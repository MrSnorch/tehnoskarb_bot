"""
Telegram-бот для сповіщень про нові монітори на tehnoskarb.ua

Встановлення:
    pip install requests beautifulsoup4 python-telegram-bot apscheduler

Налаштування:
    1. Створіть бота через @BotFather → отримайте BOT_TOKEN
    2. Створіть канал і додайте бота як адміністратора
    3. Отримайте CHAT_ID каналу (напр. @mychannel або -1001234567890)
    4. Заповніть змінні нижче або задайте через env-змінні

Запуск:
    python tehnoskarb_bot.py
"""

import os
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime

from telegram import Bot
from telegram.constants import ParseMode
from apscheduler.schedulers.blocking import BlockingScheduler

# ─── Конфігурація ────────────────────────────────────────────────────────────

BOT_TOKEN  = os.getenv("BOT_TOKEN",  "ВАШ_ТОКЕН_ТУТ")       # від @BotFather
CHAT_ID    = os.getenv("CHAT_ID",    "@ваш_канал")            # або -1001234567890
CHECK_EVERY_MINUTES = int(os.getenv("CHECK_MINUTES", "30"))   # інтервал перевірки

# URL для моніторингу (можна додати декілька)
WATCH_URLS = [
    "https://tehnoskarb.ua/monitory/c36/filter/city=60",      # Харків
    # "https://tehnoskarb.ua/monitory/c36/filter/city=5",     # Київ
]

STATE_FILE = "seen_products.json"   # файл для збереження вже бачених товарів

# ─── Логування ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# ─── Моделі ──────────────────────────────────────────────────────────────────

@dataclass
class Product:
    name: str
    url: str
    price_min: Optional[int]
    price_max: Optional[int]
    offers_count: int

# ─── Скрапер ─────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9",
}

def is_product_url(href: str) -> bool:
    import re
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
    import re
    m = re.search(r"\((\d+)\)", text)
    return int(m.group(1)) if m else 0

def scrape_page(url: str) -> list[Product]:
    resp = requests.get(url, headers=HEADERS, timeout=15)
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

    # Дедуплікація
    seen, unique = set(), []
    for p in products:
        if p.url not in seen:
            seen.add(p.url)
            unique.append(p)
    return unique

# ─── Стан (вже бачені товари) ────────────────────────────────────────────────

def load_seen() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, encoding="utf-8") as f:
        return set(json.load(f))

def save_seen(seen: set[str]):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)

# ─── Telegram ────────────────────────────────────────────────────────────────

def format_price(p: Product) -> str:
    if p.price_min == p.price_max:
        return f"{p.price_min:,} грн".replace(",", " ")
    return f"{p.price_min:,} – {p.price_max:,} грн".replace(",", " ")

def build_message(product: Product) -> str:
    price_str = format_price(product)
    offers = f"{product.offers_count} пропозиц." if product.offers_count > 1 else "1 пропозиція"
    return (
        f"🖥 <b>{product.name}</b>\n"
        f"💰 <b>{price_str}</b>  |  {offers}\n"
        f"🔗 <a href=\"{product.url}\">Переглянути на Техноскарб</a>"
    )

async def send_notification(bot: Bot, product: Product):
    text = build_message(product)
    await bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )

# ─── Основна перевірка ───────────────────────────────────────────────────────

def check_for_new_listings():
    log.info("Перевірка нових лістингів...")
    seen = load_seen()
    bot = Bot(token=BOT_TOKEN)

    new_count = 0
    for url in WATCH_URLS:
        try:
            products = scrape_page(url)
            log.info(f"  {url} → знайдено {len(products)} товарів")

            for p in products:
                if p.url not in seen:
                    log.info(f"  🆕 Новий: {p.name} ({format_price(p)})")
                    import asyncio
                    asyncio.run(send_notification(bot, p))
                    seen.add(p.url)
                    new_count += 1
                    time.sleep(0.5)  # пауза між повідомленнями

        except Exception as e:
            log.error(f"  Помилка при скрапінгу {url}: {e}")

    save_seen(seen)

    if new_count:
        log.info(f"✅ Надіслано {new_count} нових сповіщень.")
    else:
        log.info("ℹ️  Нових товарів немає.")

# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "ВАШ_ТОКЕН_ТУТ":
        print("❌ Заповніть BOT_TOKEN і CHAT_ID у скрипті або через змінні середовища!")
        print("   export BOT_TOKEN=1234567890:ABC...")
        print("   export CHAT_ID=@mychannel")
        return

    log.info(f"🤖 Бот запущено. Перевірка кожні {CHECK_EVERY_MINUTES} хв.")
    log.info(f"   Канал: {CHAT_ID}")
    log.info(f"   URLs: {len(WATCH_URLS)} шт.")

    # Перша перевірка одразу при запуску
    check_for_new_listings()

    # Планувальник
    scheduler = BlockingScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(
        check_for_new_listings,
        trigger="interval",
        minutes=CHECK_EVERY_MINUTES,
        next_run_time=None,  # вже запустили вище
        id="check_listings",
    )

    log.info(f"⏰ Наступна перевірка через {CHECK_EVERY_MINUTES} хв. Натисніть Ctrl+C для зупинки.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Бот зупинений.")


if __name__ == "__main__":
    main()
