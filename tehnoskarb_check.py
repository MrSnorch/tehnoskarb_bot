"""
Одноразова перевірка нових моніторів на tehnoskarb.ua
Запускається через GitHub Actions за розкладом.
Стан зберігається у seen_products.json (комітиться в репозиторій).

Відстежує:
  - нові товари
  - зниження ціни на вже відомі товари
"""

import os
import re
import json
import time
import asyncio
import logging
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

# ─── Конфігурація ────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID   = os.environ["CHAT_ID"]

WATCH_URLS = [
    "https://tehnoskarb.ua/monitory/c36/filter/city=60",
    # "https://tehnoskarb.ua/monitory/c36/filter/city=5",
]

STATE_FILE = "seen_products.json"
MAX_TG_LEN = 4096

# ─── Логування ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Модель ──────────────────────────────────────────────────────────────────

@dataclass
class Product:
    name: str
    url: str
    price_min: Optional[int]
    price_max: Optional[int]
    offers_count: int
    addresses: list[str] = field(default_factory=list)

# ─── HTTP ─────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ─── Скрапер: список товарів ─────────────────────────────────────────────────

def is_product_url(href: str) -> bool:
    return bool(re.search(r"/m\d{5,}/", href))

def parse_price(text: str) -> tuple[Optional[int], Optional[int]]:
    text = text.replace("\xa0", " ").replace("грн", "").strip()
    if "-" in text:
        parts = text.split("-")
        try:
            return int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            return None, None
    try:
        val = int(text.strip())
        return val, val
    except ValueError:
        return None, None

def parse_offers(text: str) -> int:
    m = re.search(r"\((\d+)\)", text)
    return int(m.group(1)) if m else 0

def scrape_listing(url: str) -> list[Product]:
    resp = SESSION.get(url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    products = []

    for card in soup.find_all("a", href=True):
        href = card["href"]
        if not is_product_url(href):
            continue
        name = card.get_text(strip=True)
        if not name or len(name) < 3:
            continue

        product_url = "https://tehnoskarb.ua" + href if href.startswith("/") else href
        container = card.parent
        for _ in range(3):
            if container and container.find(string=lambda t: t and "грн" in t):
                break
            container = container.parent if container else None

        price_text = offers_text = ""
        if container:
            for el in container.find_all(string=True):
                t = el.strip()
                if "грн" in t and not price_text:
                    price_text = t
                if "Пропозицій" in t and not offers_text:
                    offers_text = t

        price_min, price_max = parse_price(price_text) if price_text else (None, None)
        if price_min is None:
            continue

        products.append(Product(
            name=name,
            url=product_url,
            price_min=price_min,
            price_max=price_max,
            offers_count=parse_offers(offers_text) if offers_text else 1,
        ))

    seen_urls, unique = set(), []
    for p in products:
        if p.url not in seen_urls:
            seen_urls.add(p.url)
            unique.append(p)
    return unique

# ─── Скрапер: адреса магазину ────────────────────────────────────────────────

ADDRESS_RE = re.compile(r"^м\.\s*\S+,\s*.{3,60}$", re.UNICODE)

def scrape_addresses(product_url: str) -> list[str]:
    try:
        resp = SESSION.get(product_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        addresses = []
        for el in soup.find_all(string=True):
            addr = el.strip()
            if ADDRESS_RE.match(addr) and addr not in addresses:
                addresses.append(addr)

        if not addresses:
            for div in soup.find_all("div", class_=lambda c: c and "font-semibold" in c):
                text = div.get_text(strip=True)
                if re.match(r"^м\.", text) and len(text) < 80 and text not in addresses:
                    addresses.append(text)

        return addresses[:5]
    except Exception as e:
        log.warning(f"Не вдалося отримати адресу для {product_url}: {e}")
        return []

# ─── Стан ────────────────────────────────────────────────────────────────────
#
# Формат seen_products.json:
# {
#   "https://tehnoskarb.ua/...": {
#     "name": "Монітор Samsung ...",
#     "price_min": 580,
#     "price_max": 580
#   },
#   ...
# }

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    # Міграція старого формату (список URL) → новий (словник)
    if isinstance(data, list):
        log.info("Міграція seen_products.json зі старого формату...")
        return {url: {"name": "", "price_min": None, "price_max": None} for url in data}
    return data

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ─── Telegram ────────────────────────────────────────────────────────────────

def format_price(price_min: Optional[int], price_max: Optional[int]) -> str:
    if price_min == price_max:
        return f"{price_min:,} грн".replace(",", " ")
    return f"{price_min:,} – {price_max:,} грн".replace(",", " ")

def build_new_message(p: Product) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"
    addr_lines = "\n".join(f"📍 {a}" for a in p.addresses) if p.addresses else "📍 Адреса не вказана"

    msg = (
        f"🆕 <b>Новий товар!</b>\n"
        f"🖥 <b>{p.name}</b>\n"
        f"💰 <b>{format_price(p.price_min, p.price_max)}</b>  |  {offers}\n"
        f"{addr_lines}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )
    return msg[:MAX_TG_LEN - 10] + "…" if len(msg) > MAX_TG_LEN else msg

def build_price_drop_message(p: Product, old_price_min: int, old_price_max: int) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"
    addr_lines = "\n".join(f"📍 {a}" for a in p.addresses) if p.addresses else "📍 Адреса не вказана"

    # Рахуємо відсоток знижки
    if old_price_min and p.price_min:
        discount = round((old_price_min - p.price_min) / old_price_min * 100)
        discount_str = f"  (−{discount}%)"
    else:
        discount_str = ""

    msg = (
        f"📉 <b>Ціна знизилась{discount_str}!</b>\n"
        f"🖥 <b>{p.name}</b>\n"
        f"💰 <s>{format_price(old_price_min, old_price_max)}</s> → "
        f"<b>{format_price(p.price_min, p.price_max)}</b>  |  {offers}\n"
        f"{addr_lines}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )
    return msg[:MAX_TG_LEN - 10] + "…" if len(msg) > MAX_TG_LEN else msg

async def send_messages(messages: list[str]):
    bot = Bot(token=BOT_TOKEN)
    for text in messages:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        await asyncio.sleep(0.5)

# ─── Головна логіка ──────────────────────────────────────────────────────────

def main():
    log.info("Запуск перевірки...")
    state = load_state()
    messages = []

    for url in WATCH_URLS:
        try:
            products = scrape_listing(url)
            log.info(f"Знайдено {len(products)} товарів: {url}")

            for p in products:
                known = state.get(p.url)

                if known is None:
                    # Новий товар
                    log.info(f"  🆕 Новий: {p.name} ({p.price_min} грн)")
                    p.addresses = scrape_addresses(p.url)
                    time.sleep(0.5)
                    messages.append(build_new_message(p))

                else:
                    # Товар вже відомий — перевіряємо чи впала ціна
                    old_min = known.get("price_min")
                    old_max = known.get("price_max")

                    if old_min is not None and p.price_min is not None and p.price_min < old_min:
                        log.info(f"  📉 Ціна впала: {p.name} | {old_min} → {p.price_min} грн")
                        p.addresses = scrape_addresses(p.url)
                        time.sleep(0.5)
                        messages.append(build_price_drop_message(p, old_min, old_max))
                    else:
                        log.info(f"  ✓ Без змін: {p.name} ({p.price_min} грн)")

                # Оновлюємо стан
                state[p.url] = {
                    "name": p.name,
                    "price_min": p.price_min,
                    "price_max": p.price_max,
                }

        except Exception as e:
            log.error(f"Помилка: {e}")

    if messages:
        log.info(f"Надсилаємо {len(messages)} сповіщень...")
        asyncio.run(send_messages(messages))
    else:
        log.info("Нічого нового.")

    save_state(state)
    log.info("Готово.")

if __name__ == "__main__":
    main()
