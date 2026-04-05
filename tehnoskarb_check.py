"""
Одноразова перевірка нових моніторів на tehnoskarb.ua
Запускається через GitHub Actions за розкладом.
Стан зберігається у seen_products.json (комітиться в репозиторій).
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
MAX_TG_LEN = 4096  # ліміт Telegram

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

# Адреса виду "м. Харків, вул. Дудинської, 1-А" — не довша за 80 символів
ADDRESS_RE = re.compile(r"^м\.\s*\S+,\s*.{3,60}$", re.UNICODE)

def scrape_addresses(product_url: str) -> list[str]:
    """Заходить на сторінку товару і витягує адреси магазинів."""
    try:
        resp = SESSION.get(product_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        addresses = []

        for el in soup.find_all(string=True):
            addr = el.strip()
            if ADDRESS_RE.match(addr) and addr not in addresses:
                addresses.append(addr)

        # Резервний варіант — через div.font-semibold
        if not addresses:
            for div in soup.find_all("div", class_=lambda c: c and "font-semibold" in c):
                text = div.get_text(strip=True)
                if re.match(r"^м\.", text) and len(text) < 80 and text not in addresses:
                    addresses.append(text)

        return addresses[:5]  # максимум 5 адрес на товар

    except Exception as e:
        log.warning(f"Не вдалося отримати адресу для {product_url}: {e}")
        return []

# ─── Стан ────────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, encoding="utf-8") as f:
        return set(json.load(f))

def save_seen(seen: set[str]):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)

# ─── Telegram ────────────────────────────────────────────────────────────────

def format_price(p: Product) -> str:
    if p.price_min == p.price_max:
        return f"{p.price_min:,} грн".replace(",", " ")
    return f"{p.price_min:,} – {p.price_max:,} грн".replace(",", " ")

def build_message(p: Product) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"

    if p.addresses:
        addr_lines = "\n".join(f"📍 {a}" for a in p.addresses)
    else:
        addr_lines = "📍 Адреса не вказана"

    msg = (
        f"🖥 <b>{p.name}</b>\n"
        f"💰 <b>{format_price(p)}</b>  |  {offers}\n"
        f"{addr_lines}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )

    # Захист від перевищення ліміту Telegram
    if len(msg) > MAX_TG_LEN:
        msg = msg[:MAX_TG_LEN - 10] + "…"

    return msg

async def send_notifications(new_products: list[Product]):
    bot = Bot(token=BOT_TOKEN)
    for p in new_products:
        text = build_message(p)
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        log.info(f"  📨 Надіслано: {p.name} | {p.addresses}")
        await asyncio.sleep(0.5)

# ─── Головна логіка ──────────────────────────────────────────────────────────

def main():
    log.info("Запуск перевірки...")
    seen = load_seen()
    new_products = []

    for url in WATCH_URLS:
        try:
            products = scrape_listing(url)
            log.info(f"Знайдено {len(products)} товарів: {url}")

            for p in products:
                if p.url not in seen:
                    log.info(f"  🆕 Новий: {p.name} — отримуємо адресу...")
                    p.addresses = scrape_addresses(p.url)
                    time.sleep(0.5)
                    new_products.append(p)
                    seen.add(p.url)

        except Exception as e:
            log.error(f"Помилка: {e}")

    if new_products:
        log.info(f"Нових товарів: {len(new_products)} — надсилаємо сповіщення...")
        asyncio.run(send_notifications(new_products))
    else:
        log.info("Нових товарів немає.")

    save_seen(seen)
    log.info("Готово.")

if __name__ == "__main__":
    main()
