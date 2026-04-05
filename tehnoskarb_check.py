"""
Перевірка нових моніторів на tehnoskarb.ua — всі міста
Запускається через GitHub Actions за розкладом.

Зберігає:
  - seen_products.json  — стан (пам'ять бота)
  - docs/data.json      — дані для GitHub Pages дашборду
"""

import os
import re
import json
import time
import asyncio
import logging
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from dataclasses import dataclass, field, asdict
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

# ─── Конфігурація ────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID   = os.environ["CHAT_ID"]

# Всі міста — скрапимо одну сторінку без фільтра по місту
WATCH_URL = "https://tehnoskarb.ua/monitory/c36"

STATE_FILE    = "seen_products.json"
DASHBOARD_DIR = "docs"
DATA_FILE     = os.path.join(DASHBOARD_DIR, "data.json")

MAX_TG_LEN    = 4096
MAX_PAGES     = 10   # максимум сторінок пагінації

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
    city: str = ""
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
    return bool(re.search(r"/m\d{5,}", href))

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

def scrape_page(url: str) -> list[Product]:
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
        # Прибираємо filter/city=XX з URL для уніфікації ключів
        product_url = re.sub(r"/filter/city=\d+", "", product_url)

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

    return products

def scrape_all_pages() -> list[Product]:
    """Збирає товари з усіх сторінок пагінації."""
    all_products: list[Product] = []
    seen_urls: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = WATCH_URL if page == 1 else f"{WATCH_URL}?page={page}"
        log.info(f"  Сторінка {page}: {url}")

        try:
            items = scrape_page(url)
        except Exception as e:
            log.error(f"  Помилка на сторінці {page}: {e}")
            break

        if not items:
            log.info("  Порожня сторінка — кінець.")
            break

        new_items = [p for p in items if p.url not in seen_urls]
        if not new_items:
            log.info("  Дублікати — кінець пагінації.")
            break

        for p in new_items:
            seen_urls.add(p.url)
            all_products.append(p)

        time.sleep(0.5)

    return all_products

# ─── Скрапер: адреса + місто ─────────────────────────────────────────────────

ADDRESS_RE = re.compile(r"^м\.\s*\S+,\s*.{3,60}$", re.UNICODE)
CITY_RE    = re.compile(r"м\.\s*(\S+)")

def scrape_details(product_url: str) -> tuple[str, list[str]]:
    """Повертає (місто, [адреси])."""
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

        addresses = addresses[:5]

        # Витягуємо назву міста з першої адреси
        city = ""
        if addresses:
            m = CITY_RE.search(addresses[0])
            if m:
                city = m.group(1).rstrip(",")

        return city, addresses

    except Exception as e:
        log.warning(f"Не вдалося отримати деталі для {product_url}: {e}")
        return "", []

# ─── Стан ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        log.info("Міграція seen_products.json...")
        return {url: {"name": "", "price_min": None, "price_max": None, "city": ""} for url in data}
    return data

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ─── Dashboard data.json ─────────────────────────────────────────────────────

def save_dashboard(state: dict, events: list[dict]):
    """Генерує docs/data.json для GitHub Pages дашборду."""
    os.makedirs(DASHBOARD_DIR, exist_ok=True)

    # Поточні товари (є в state і не продані)
    products = []
    for url, d in state.items():
        products.append({
            "name":       d.get("name", ""),
            "url":        url,
            "price_min":  d.get("price_min"),
            "price_max":  d.get("price_max"),
            "city":       d.get("city", ""),
            "addresses":  d.get("addresses", []),
            "offers":     d.get("offers_count", 1),
            "first_seen": d.get("first_seen", ""),
            "price_history": d.get("price_history", []),
        })

    # Статистика
    prices = [p["price_min"] for p in products if p["price_min"]]
    stats = {
        "total":     len(products),
        "avg_price": round(sum(prices) / len(prices)) if prices else 0,
        "min_price": min(prices) if prices else 0,
        "max_price": max(prices) if prices else 0,
        "cities":    sorted(set(p["city"] for p in products if p["city"])),
    }

    data = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats":    stats,
        "products": sorted(products, key=lambda p: p["price_min"] or 0),
        "events":   events[-100:],  # останні 100 подій
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info(f"Дашборд оновлено: {len(products)} товарів → {DATA_FILE}")

# ─── Telegram ────────────────────────────────────────────────────────────────

def fmt_price(price_min, price_max) -> str:
    if not price_min:
        return "—"
    if price_min == price_max:
        return f"{price_min:,} грн".replace(",", " ")
    return f"{price_min:,} – {price_max:,} грн".replace(",", " ")

def pct(old, new) -> str:
    p = round(abs(old - new) / old * 100)
    return f" (−{p}%)" if new < old else f" (+{p}%)"

def addr_block(addresses) -> str:
    return "\n".join(f"📍 {a}" for a in addresses) if addresses else "📍 Адреса не вказана"

def safe(text) -> str:
    return text[:MAX_TG_LEN - 10] + "…" if len(text) > MAX_TG_LEN else text

def msg_new(p: Product) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"
    city = f"🏙 {p.city}\n" if p.city else ""
    return safe(
        f"🆕 <b>Новий товар!</b>\n"
        f"🖥 <b>{p.name}</b>\n"
        f"💰 <b>{fmt_price(p.price_min, p.price_max)}</b>  |  {offers}\n"
        f"{city}{addr_block(p.addresses)}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )

def msg_drop(p: Product, old_min, old_max) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"
    city = f"🏙 {p.city}\n" if p.city else ""
    return safe(
        f"📉 <b>Ціна знизилась{pct(old_min, p.price_min)}!</b>\n"
        f"🖥 <b>{p.name}</b>\n"
        f"💰 <s>{fmt_price(old_min, old_max)}</s> → <b>{fmt_price(p.price_min, p.price_max)}</b>  |  {offers}\n"
        f"{city}{addr_block(p.addresses)}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )

def msg_rise(p: Product, old_min, old_max) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"
    city = f"🏙 {p.city}\n" if p.city else ""
    return safe(
        f"📈 <b>Ціна підвищилась{pct(old_min, p.price_min)}!</b>\n"
        f"🖥 <b>{p.name}</b>\n"
        f"💰 <s>{fmt_price(old_min, old_max)}</s> → <b>{fmt_price(p.price_min, p.price_max)}</b>  |  {offers}\n"
        f"{city}{addr_block(p.addresses)}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )

def msg_sold(name, url, price_min, price_max, city) -> str:
    city_str = f"🏙 {city}\n" if city else ""
    return safe(
        f"✅ <b>Товар продано!</b>\n"
        f"🖥 <b>{name}</b>\n"
        f"💰 Була ціна: {fmt_price(price_min, price_max)}\n"
        f"{city_str}"
        f"🔗 <a href=\"{url}\">Посилання на Техноскарб</a>"
    )

async def send_messages(messages: list[str]):
    bot = Bot(token=BOT_TOKEN)
    for text in messages:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        await asyncio.sleep(0.5)

# ─── Головна логіка ──────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("Запуск перевірки (всі міста)...")

    state   = load_state()
    tg_msgs = []
    events  = []

    # Завантажуємо існуючі події з data.json щоб не втратити
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                old_data = json.load(f)
            events = old_data.get("events", [])
        except Exception:
            pass

    # Скрапимо всі сторінки
    products = scrape_all_pages()
    log.info(f"Всього знайдено товарів: {len(products)}")
    current_urls = {p.url for p in products}

    # ── Перевірка зниклих товарів (продані) ──────────────────────────────────
    for known_url in list(state.keys()):
        if known_url not in current_urls:
            d = state.pop(known_url)
            name = d.get("name") or known_url
            log.info(f"  ✅ Продано: {name}")
            tg_msgs.append(msg_sold(name, known_url, d.get("price_min"), d.get("price_max"), d.get("city", "")))
            events.append({"type": "sold", "name": name, "url": known_url,
                           "price": d.get("price_min"), "city": d.get("city", ""), "at": now})

    # ── Перевірка нових та змін ціни ─────────────────────────────────────────
    for p in products:
        known = state.get(p.url)

        if known is None:
            # Новий товар — йдемо за деталями
            log.info(f"  🆕 Новий: {p.name}")
            p.city, p.addresses = scrape_details(p.url)
            time.sleep(0.5)
            tg_msgs.append(msg_new(p))
            events.append({"type": "new", "name": p.name, "url": p.url,
                           "price": p.price_min, "city": p.city, "at": now})
            state[p.url] = {
                "name": p.name, "price_min": p.price_min, "price_max": p.price_max,
                "city": p.city, "addresses": p.addresses, "offers_count": p.offers_count,
                "first_seen": now,
                "price_history": [{"price": p.price_min, "at": now}],
            }

        else:
            old_min = known.get("price_min")
            old_max = known.get("price_max")

            if old_min is not None and p.price_min is not None and p.price_min != old_min:
                p.city      = known.get("city", "")
                p.addresses = known.get("addresses", [])

                if p.price_min < old_min:
                    log.info(f"  📉 Ціна впала: {p.name} | {old_min} → {p.price_min} грн")
                    tg_msgs.append(msg_drop(p, old_min, old_max))
                    events.append({"type": "drop", "name": p.name, "url": p.url,
                                   "price_old": old_min, "price_new": p.price_min,
                                   "city": p.city, "at": now})
                else:
                    log.info(f"  📈 Ціна зросла: {p.name} | {old_min} → {p.price_min} грн")
                    tg_msgs.append(msg_rise(p, old_min, old_max))
                    events.append({"type": "rise", "name": p.name, "url": p.url,
                                   "price_old": old_min, "price_new": p.price_min,
                                   "city": p.city, "at": now})

                # Додаємо в історію цін
                history = known.get("price_history", [])
                history.append({"price": p.price_min, "at": now})
                known["price_history"] = history
            else:
                log.info(f"  ✓ Без змін: {p.name} ({p.price_min} грн)")

            # Оновлюємо стан
            known.update({
                "name": p.name,
                "price_min": p.price_min,
                "price_max": p.price_max,
                "offers_count": p.offers_count,
            })
            state[p.url] = known

    # ── Надсилаємо в Telegram ─────────────────────────────────────────────────
    if tg_msgs:
        log.info(f"Надсилаємо {len(tg_msgs)} сповіщень...")
        asyncio.run(send_messages(tg_msgs))
    else:
        log.info("Нічого нового.")

    # ── Зберігаємо стан і дашборд ────────────────────────────────────────────
    save_state(state)
    save_dashboard(state, events)
    log.info("Готово.")

if __name__ == "__main__":
    main()
