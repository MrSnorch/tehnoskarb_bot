"""
Перевірка нових товарів на tehnoskarb.ua — декілька категорій
Запускається через GitHub Actions за розкладом.

Зберігає:
  - seen_products.json  — стан (пам'ять бота)
  - docs/data.json      — дані для GitHub Pages дашборду

Налаштування читаються з config.json (змінюються через адмін-панель).
"""

import os
import re
import json
import time
import asyncio
import logging
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter

# ─── Змінні середовища ───────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALL_PRODUCTS_CHAT_ID = os.environ.get("ALL_PRODUCTS_CHAT_ID", "").strip()

# ─── ID групи та тем (вшиті прямо в код) ─────────────────────────────────────
CHAT_ID        = os.environ.get("CHAT_ID",        "-1003980198992").strip()
THREAD_ALL     = os.environ.get("THREAD_ALL",     "1").strip()
THREAD_NEW     = os.environ.get("THREAD_NEW",     "2").strip()
THREAD_PRICE   = os.environ.get("THREAD_PRICE",   "3").strip()
THREAD_SOLD    = os.environ.get("THREAD_SOLD",    "4").strip()
THREAD_KHARKIV = os.environ.get("THREAD_KHARKIV", "8").strip()
KHARKIV_CITY   = "харків"          # назва міста для порівняння (малими літерами)

CONFIG_FILE   = "config.json"
STATE_FILE    = "seen_products.json"
DASHBOARD_DIR = "docs"
DATA_FILE     = os.path.join(DASHBOARD_DIR, "data.json")

MAX_TG_LEN            = 4096
MAX_PAGES             = 10
DETAILS_FETCH_RETRIES = 4
DETAILS_REQUEST_DELAY = 2.0
DETAILS_RETRY_BASE    = 3.0
DETAILS_RETRY_MAX     = 15.0
DETAILS_RETRY_COOLDOWN_MIN = 30
DETAILS_RETRY_COOLDOWN_MAX = 360

# Категорії за замовчуванням якщо config відсутній
DEFAULT_CATEGORIES = [
    {"name": "Монітори", "url": "https://tehnoskarb.ua/monitory/c36", "emoji": "🖥", "enabled": True},
]

# ─── Логування ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Конфіг ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    defaults = {
        "categories":          DEFAULT_CATEGORIES,
        "notify_cities":       [],
        "price_min":           None,
        "price_max":           None,
        "keywords":            [],
        "notify_new":          True,
        "notify_price_drop":   True,
        "notify_price_rise":   True,
        "notify_sold":         True,
        "max_individual_msgs": 5,
    }
    if not os.path.exists(CONFIG_FILE):
        log.warning(f"{CONFIG_FILE} не знайдено — defaults")
        return defaults
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        defaults.update(cfg)
        enabled = [c["name"] for c in defaults["categories"] if c.get("enabled")]
        log.info(f"Конфіг: категорії={enabled}, міста={defaults['notify_cities']}, "
                 f"ціна={defaults['price_min']}–{defaults['price_max']}, кл.слова={defaults['keywords']}")
        return defaults
    except Exception as e:
        log.error(f"Помилка читання {CONFIG_FILE}: {e}")
        return defaults

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
    category: str = ""
    category_emoji: str = ""

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

# ─── Фільтри ─────────────────────────────────────────────────────────────────

def passes_filters(p: Product, cfg: dict) -> bool:
    notify_cities = [c.strip().casefold() for c in cfg.get("notify_cities", []) if c.strip()]
    if notify_cities and p.city.strip().casefold() not in notify_cities:
        return False
    price_min_cfg = cfg.get("price_min")
    if price_min_cfg is not None and p.price_min is not None and p.price_min < price_min_cfg:
        return False
    price_max_cfg = cfg.get("price_max")
    if price_max_cfg is not None and p.price_min is not None and p.price_min > price_max_cfg:
        return False
    keywords = [k.strip().casefold() for k in cfg.get("keywords", []) if k.strip()]
    if keywords and not any(kw in p.name.casefold() for kw in keywords):
        return False
    return True

def is_kharkiv(city: str) -> bool:
    return city.strip().casefold() == KHARKIV_CITY



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
        return int(text.strip()), int(text.strip())
    except ValueError:
        return None, None

def parse_offers(text: str) -> int:
    m = re.search(r"\((\d+)\)", text)
    return int(m.group(1)) if m else 0

def scrape_page(url: str, category: str = "", emoji: str = "") -> list[Product]:
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
            name=name, url=product_url,
            price_min=price_min, price_max=price_max,
            offers_count=parse_offers(offers_text) if offers_text else 1,
            category=category, category_emoji=emoji,
        ))

    return products

def scrape_category(base_url: str, category: str, emoji: str) -> list[Product]:
    """Збирає всі сторінки однієї категорії."""
    all_products: list[Product] = []
    seen_urls: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        log.info(f"    [{category}] Сторінка {page}: {url}")
        try:
            items = scrape_page(url, category, emoji)
        except Exception as e:
            log.error(f"    Помилка: {e}")
            break

        if not items:
            break

        new_items = [p for p in items if p.url not in seen_urls]
        if not new_items:
            log.info(f"    [{category}] Дублікати — кінець.")
            break

        for p in new_items:
            seen_urls.add(p.url)
            all_products.append(p)

        time.sleep(0.5)

    log.info(f"    [{category}] Всього: {len(all_products)} товарів")
    return all_products

# ─── Деталі (адреса + місто) ─────────────────────────────────────────────────

ADDRESS_RE = re.compile(r"^(?:м|г)\.\s*[^,]+,\s*.{3,60}$", re.UNICODE | re.IGNORECASE)
CITY_RE    = re.compile(r"^(?:м|г)\.\s*([^,]+)", re.UNICODE | re.IGNORECASE)

def extract_city(address: str) -> str:
    m = CITY_RE.search(address.strip())
    return m.group(1).strip() if m else ""

def build_detail_fetch_urls(product_url: str) -> list[str]:
    candidates = [product_url]
    parsed = urlsplit(product_url)

    if not parsed.scheme or not parsed.netloc:
        return candidates

    path = parsed.path or "/"
    candidate_paths = [path]

    if path.startswith("/ru/"):
        candidate_paths.append(path[3:] or "/")
    elif path != "/ru":
        candidate_paths.append("/ru" + path if path.startswith("/") else f"/ru/{path}")

    item_match = re.search(r"/(m\d{5,})(?:/)?$", path, re.IGNORECASE)
    if item_match:
        item_path = f"/{item_match.group(1)}"
        candidate_paths.append(item_path)
        candidate_paths.append(f"/ru{item_path}")

    for candidate_path in candidate_paths:
        candidate_url = urlunsplit((parsed.scheme, parsed.netloc, candidate_path, parsed.query, parsed.fragment))
        if candidate_url not in candidates:
            candidates.append(candidate_url)

    return candidates

def get_details_retry_delay(attempt: int) -> float:
    return min(DETAILS_RETRY_BASE * (2 ** (attempt - 1)), DETAILS_RETRY_MAX)

def should_retry(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return False

def scrape_details(product_url: str) -> tuple[str, list[str]]:
    detail_urls = build_detail_fetch_urls(product_url)

    for attempt in range(1, DETAILS_FETCH_RETRIES + 1):
        last_exc: Optional[Exception] = None
        retryable_exc: Optional[Exception] = None

        for detail_url in detail_urls:
            try:
                resp = SESSION.get(detail_url, timeout=15)
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
                        if re.match(r"^(?:м|г)\.", text, re.IGNORECASE) and len(text) < 80 and text not in addresses:
                            addresses.append(text)

                addresses = addresses[:5]
                city = extract_city(addresses[0]) if addresses else ""
                return city, addresses

            except Exception as e:
                last_exc = e
                if should_retry(e):
                    retryable_exc = e

        if attempt < DETAILS_FETCH_RETRIES and retryable_exc is not None:
            delay = get_details_retry_delay(attempt)
            log.warning(f"Деталі retry {attempt}/{DETAILS_FETCH_RETRIES - 1} через {delay:.1f}с: {retryable_exc}")
            time.sleep(delay)
            continue

        if last_exc is not None:
            log.warning(f"Деталі недоступні для {product_url}: {last_exc}")
        return "", []

# ─── Стан ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {url: {"name": "", "price_min": None, "price_max": None, "city": ""} for url in data}
    return data

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def details_retry_due(record: dict, now_dt: datetime) -> bool:
    retry_at = parse_utc(record.get("details_retry_at", ""))
    return retry_at is None or now_dt >= retry_at

def mark_details_success(record: dict, city: str, addresses: list[str], now_dt: datetime):
    record["details_failures"] = 0
    record["details_last_attempt"] = format_utc(now_dt)
    if city:
        record["city"] = city
    if addresses:
        record["addresses"] = addresses
    record.pop("details_retry_at", None)

def mark_details_failure(record: dict, now_dt: datetime):
    failures = int(record.get("details_failures", 0)) + 1
    delay_minutes = min(DETAILS_RETRY_COOLDOWN_MIN * (2 ** (failures - 1)), DETAILS_RETRY_COOLDOWN_MAX)
    record["details_failures"] = failures
    record["details_last_attempt"] = format_utc(now_dt)
    record["details_retry_at"] = format_utc(now_dt + timedelta(minutes=delay_minutes))

# ─── Dashboard ───────────────────────────────────────────────────────────────

def save_dashboard(state: dict, events: list[dict], cfg: dict):
    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    products = []
    for url, d in state.items():
        products.append({
            "name":          d.get("name", ""),
            "url":           url,
            "price_min":     d.get("price_min"),
            "price_max":     d.get("price_max"),
            "city":          d.get("city", ""),
            "addresses":     d.get("addresses", []),
            "offers":        d.get("offers_count", 1),
            "first_seen":    d.get("first_seen", ""),
            "price_history": d.get("price_history", []),
            "category":      d.get("category", ""),
            "category_emoji":d.get("category_emoji", ""),
        })

    prices = [p["price_min"] for p in products if p["price_min"]]
    cats   = sorted(set(p["category"] for p in products if p["category"]))
    cities = sorted(set(p["city"] for p in products if p["city"]))

    stats = {
        "total":      len(products),
        "avg_price":  round(sum(prices) / len(prices)) if prices else 0,
        "min_price":  min(prices) if prices else 0,
        "max_price":  max(prices) if prices else 0,
        "cities":     cities,
        "categories": cats,
    }

    data = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats":      stats,
        "products":   sorted(products, key=lambda p: p["price_min"] or 0),
        "events":     events[-200:],
        "config":     cfg,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info(f"Дашборд: {len(products)} товарів, {len(cats)} категорій")

# ─── Telegram ────────────────────────────────────────────────────────────────

def fmt_price(mn, mx) -> str:
    if not mn: return "—"
    f = lambda n: f"{n:,}".replace(",", " ") + " грн"
    return f(mn) if mn == mx else f"{f(mn)} – {f(mx)}"

def pct(old, new) -> str:
    p = round(abs(old - new) / old * 100)
    return f" (−{p}%)" if new < old else f" (+{p}%)"

def addr_block(addresses) -> str:
    return "\n".join(f"📍 {a}" for a in addresses) if addresses else "📍 Адреса не вказана"

def safe(text) -> str:
    return text[:MAX_TG_LEN - 10] + "…" if len(text) > MAX_TG_LEN else text

def cat_line(p: Product) -> str:
    return f"{p.category_emoji} {p.category}\n" if p.category else ""

def msg_new(p: Product) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"
    city = f"🏙 {p.city}\n" if p.city else ""
    return safe(
        f"🆕 <b>Новий товар!</b>\n"
        f"{cat_line(p)}"
        f"<b>{p.name}</b>\n"
        f"💰 <b>{fmt_price(p.price_min, p.price_max)}</b>  |  {offers}\n"
        f"{city}{addr_block(p.addresses)}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )

def msg_drop(p: Product, old_min, old_max) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"
    city = f"🏙 {p.city}\n" if p.city else ""
    return safe(
        f"📉 <b>Ціна знизилась{pct(old_min, p.price_min)}!</b>\n"
        f"{cat_line(p)}"
        f"<b>{p.name}</b>\n"
        f"💰 <s>{fmt_price(old_min, old_max)}</s> → <b>{fmt_price(p.price_min, p.price_max)}</b>  |  {offers}\n"
        f"{city}{addr_block(p.addresses)}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )

def msg_rise(p: Product, old_min, old_max) -> str:
    offers = f"{p.offers_count} пропозиц." if p.offers_count > 1 else "1 пропозиція"
    city = f"🏙 {p.city}\n" if p.city else ""
    return safe(
        f"📈 <b>Ціна підвищилась{pct(old_min, p.price_min)}!</b>\n"
        f"{cat_line(p)}"
        f"<b>{p.name}</b>\n"
        f"💰 <s>{fmt_price(old_min, old_max)}</s> → <b>{fmt_price(p.price_min, p.price_max)}</b>  |  {offers}\n"
        f"{city}{addr_block(p.addresses)}\n"
        f"🔗 <a href=\"{p.url}\">Переглянути на Техноскарб</a>"
    )

def msg_sold(name, url, price_min, price_max, city, category, emoji) -> str:
    city_str = f"🏙 {city}\n" if city else ""
    cat_str  = f"{emoji} {category}\n" if category else ""
    return safe(
        f"✅ <b>Товар продано!</b>\n"
        f"{cat_str}"
        f"<b>{name}</b>\n"
        f"💰 Була ціна: {fmt_price(price_min, price_max)}\n"
        f"{city_str}"
        f"🔗 <a href=\"{url}\">Посилання на Техноскарб</a>"
    )

def msg_summary(new_products: list[Product], total: int) -> str:
    lines = [f"📦 <b>Завантажено {total} товарів</b>\n", "Ось кілька прикладів:\n"]
    for p in new_products[:10]:
        city = f" · {p.city}" if p.city else ""
        cat  = f" [{p.category}]" if p.category else ""
        lines.append(f"• <a href=\"{p.url}\">{p.name}</a> — <b>{fmt_price(p.price_min, p.price_max)}</b>{city}{cat}")
    if total > 10:
        lines.append(f"\n…і ще {total - 10} товарів на дашборді")
    return safe("\n".join(lines))

def compact_messages(messages: list, new_products: list[Product], max_individual: int) -> list:
    """messages is a list of (msg_type, text) tuples."""
    if len(messages) <= max_individual:
        return messages

    # Зберігаємо всі не-нові (ціна/продажі) + зводне для нових
    sold_price = [(t, m) for t, m in messages if t != "new"]
    if new_products:
        sold_price.append(("new", msg_summary(new_products, len(new_products))))
    return sold_price

def get_notification_targets(filtered_messages: list[str], all_messages: list[str]) -> list[tuple[str, str, list[str]]]:
    targets = [("основний канал", CHAT_ID, filtered_messages)]
    if not ALL_PRODUCTS_CHAT_ID:
        return targets

    if ALL_PRODUCTS_CHAT_ID == CHAT_ID:
        log.warning("ALL_PRODUCTS_CHAT_ID збігається з CHAT_ID — дубльоване надсилання пропускаємо")
        return targets

    targets.append(("канал всіх товарів", ALL_PRODUCTS_CHAT_ID, all_messages))
    return targets

async def send_one(bot: Bot, chat_id: str, text: str, retries: int = 5, thread_id: Optional[int] = None):
    for attempt in range(retries):
        try:
            kwargs = dict(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            await bot.send_message(**kwargs)
            return
        except RetryAfter as e:
            wait = e.retry_after + 1
            log.warning(f"Flood control -- чекаємо {wait}с (спроба {attempt+1}/{retries})")
            await asyncio.sleep(wait)
        except Exception as e:
            log.error(f"Помилка надсилання: {e}")
            return
    log.error("Не вдалося надіслати після всіх спроб.")

async def send_messages(chat_id: str, messages: list[str], thread_id: Optional[int] = None):
    bot = Bot(token=BOT_TOKEN)
    for i, text in enumerate(messages):
        await send_one(bot, chat_id, text, thread_id=thread_id)
        if i < len(messages) - 1:
            await asyncio.sleep(1.5)

async def send_messages_by_type(chat_id: str, typed_messages: list):
    """Надсилає повідомлення у відповідні теми групи залежно від типу (new/price/sold)."""
    bot = Bot(token=BOT_TOKEN)
    thread_map = {
        "new":   int(THREAD_NEW)   if THREAD_NEW   else None,
        "price": int(THREAD_PRICE) if THREAD_PRICE else None,
        "sold":  int(THREAD_SOLD)  if THREAD_SOLD  else None,
    }
    for i, (msg_type, text) in enumerate(typed_messages):
        thread_id = thread_map.get(msg_type)
        await send_one(bot, chat_id, text, thread_id=thread_id)
        if i < len(typed_messages) - 1:
            await asyncio.sleep(1.5)

# ─── Головна логіка ──────────────────────────────────────────────────────────

def main():
    now_dt = datetime.now(timezone.utc)
    now = format_utc(now_dt)
    log.info("═" * 50)
    log.info("Запуск перевірки")

    cfg   = load_config()
    state = load_state()
    is_first_run = len(state) == 0
    max_individual = cfg.get("max_individual_msgs", 5)

    # Активні категорії
    active_cats = [c for c in cfg.get("categories", DEFAULT_CATEGORIES) if c.get("enabled")]
    if not active_cats:
        log.warning("Жодна категорія не активована — виходимо")
        return

    log.info(f"Активних категорій: {len(active_cats)} — {[c['name'] for c in active_cats]}")

    tg_msgs       = []  # list of (msg_type, text): msg_type in "new","price","sold"
    all_tg_msgs   = []
    kharkiv_msgs  = []  # всі події по місту Харків
    events        = []
    new_list      = []

    # Завантажуємо попередні події
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                events = json.load(f).get("events", [])
        except Exception:
            pass

    # Збираємо всі товари з усіх активних категорій
    all_products: list[Product] = []
    for cat in active_cats:
        log.info(f"  Категорія: {cat['emoji']} {cat['name']}")
        items = scrape_category(cat["url"], cat["name"], cat.get("emoji", ""))
        all_products.extend(items)

    log.info(f"Всього товарів з усіх категорій: {len(all_products)}")
    current_urls = {p.url for p in all_products}

    # ── Продані товари ────────────────────────────────────────────────────────
    if not is_first_run:
        for known_url in list(state.keys()):
            if known_url not in current_urls:
                d = state.pop(known_url)
                name      = d.get("name") or known_url
                sold_city = d.get("city", "")
                sold_addrs= d.get("addresses", [])
                if not sold_city and sold_addrs:
                    sold_city = extract_city(sold_addrs[0])
                cat_name  = d.get("category", "")
                cat_emoji = d.get("category_emoji", "")

                log.info(f"  ✅ Продано [{cat_name}]: {name}")
                events.append({"type": "sold", "name": name, "url": known_url,
                               "price": d.get("price_min"), "city": sold_city,
                               "category": cat_name, "at": now})

                sold_msg = msg_sold(name, known_url, d.get("price_min"),
                                    d.get("price_max"), sold_city, cat_name, cat_emoji)
                all_tg_msgs.append(sold_msg)
                if cfg.get("notify_sold", True):
                    fake = Product(name=name, url=known_url,
                                   price_min=d.get("price_min"), price_max=d.get("price_max"),
                                   offers_count=1, city=sold_city, addresses=sold_addrs,
                                   category=cat_name, category_emoji=cat_emoji)
                    if passes_filters(fake, cfg):
                        tg_msgs.append(("sold", sold_msg))
                if cfg.get("notify_sold", True) and is_kharkiv(sold_city):
                    kharkiv_msgs.append(sold_msg)

    # ── Нові товари та зміни ціни ─────────────────────────────────────────────
    for p in all_products:
        known = state.get(p.url)

        if known is None:
            log.info(f"  🆕 [{p.category}] {p.name}")
            details_loaded = False
            if not is_first_run:
                p.city, p.addresses = scrape_details(p.url)
                time.sleep(DETAILS_REQUEST_DELAY)
                details_loaded = bool(p.city or p.addresses)
                new_msg = msg_new(p)
                all_tg_msgs.append(new_msg)
                if cfg.get("notify_new", True) and passes_filters(p, cfg):
                    tg_msgs.append(("new", new_msg))
                    new_list.append(p)
                if cfg.get("notify_new", True) and is_kharkiv(p.city):
                    kharkiv_msgs.append(new_msg)

            events.append({"type": "new", "name": p.name, "url": p.url,
                           "price": p.price_min, "city": p.city,
                           "category": p.category, "at": now})
            state[p.url] = {
                "name": p.name, "price_min": p.price_min, "price_max": p.price_max,
                "city": p.city, "addresses": p.addresses, "offers_count": p.offers_count,
                "category": p.category, "category_emoji": p.category_emoji,
                "first_seen": now, "price_history": [{"price": p.price_min, "at": now}],
            }
            if not is_first_run:
                if details_loaded:
                    mark_details_success(state[p.url], p.city, p.addresses, now_dt)
                else:
                    mark_details_failure(state[p.url], now_dt)

        else:
            # Підтягуємо деталі якщо ще немає
            stored_city  = known.get("city", "")
            stored_addrs = known.get("addresses", [])
            if not stored_city and stored_addrs:
                stored_city = extract_city(stored_addrs[0])
                known["city"] = stored_city
            if not stored_city or not stored_addrs:
                if details_retry_due(known, now_dt):
                    fc, fa = scrape_details(p.url)
                    time.sleep(DETAILS_REQUEST_DELAY)
                    if fa:
                        stored_addrs = fa
                        known["addresses"] = fa
                    if fc:
                        stored_city = fc
                        known["city"] = fc

                    if fc or fa:
                        mark_details_success(known, stored_city, stored_addrs, now_dt)
                    else:
                        mark_details_failure(known, now_dt)

            p.city      = stored_city
            p.addresses = stored_addrs
            # Зберігаємо категорію якщо ще немає
            if not known.get("category"):
                known["category"]       = p.category
                known["category_emoji"] = p.category_emoji

            old_min = known.get("price_min")
            old_max = known.get("price_max")

            if old_min is not None and p.price_min is not None and p.price_min != old_min:
                if p.price_min < old_min:
                    log.info(f"  📉 [{p.category}] {p.name} | {old_min} → {p.price_min} грн")
                    drop_msg = msg_drop(p, old_min, old_max)
                    all_tg_msgs.append(drop_msg)
                    if cfg.get("notify_price_drop", True) and passes_filters(p, cfg):
                        tg_msgs.append(("price", drop_msg))
                    if cfg.get("notify_price_drop", True) and is_kharkiv(p.city):
                        kharkiv_msgs.append(drop_msg)
                    events.append({"type": "drop", "name": p.name, "url": p.url,
                                   "price_old": old_min, "price_new": p.price_min,
                                   "city": p.city, "category": p.category, "at": now})
                else:
                    log.info(f"  📈 [{p.category}] {p.name} | {old_min} → {p.price_min} грн")
                    rise_msg = msg_rise(p, old_min, old_max)
                    all_tg_msgs.append(rise_msg)
                    if cfg.get("notify_price_rise", True) and passes_filters(p, cfg):
                        tg_msgs.append(("price", rise_msg))
                    if cfg.get("notify_price_rise", True) and is_kharkiv(p.city):
                        kharkiv_msgs.append(rise_msg)
                    events.append({"type": "rise", "name": p.name, "url": p.url,
                                   "price_old": old_min, "price_new": p.price_min,
                                   "city": p.city, "category": p.category, "at": now})

                known.setdefault("price_history", []).append({"price": p.price_min, "at": now})
            else:
                log.info(f"  ✓ [{p.category}] {p.name} ({p.price_min} грн)")

            known.update({"name": p.name, "price_min": p.price_min,
                          "price_max": p.price_max, "offers_count": p.offers_count})
            state[p.url] = known

    # ── Telegram ──────────────────────────────────────────────────────────────
    if is_first_run:
        log.info(f"Перший запуск — зберігаємо базу ({len(state)} товарів), без сповіщень")
        tg_msgs = []
        all_tg_msgs = []
        kharkiv_msgs = []
    else:
        tg_msgs = compact_messages(tg_msgs, new_list, max_individual)

    sent_any = False

    # Якщо задані теми (threads) — надсилаємо в них по типу повідомлення
    use_threads = any([THREAD_NEW, THREAD_PRICE, THREAD_SOLD])

    if tg_msgs:
        sent_any = True
        if use_threads:
            log.info(f"Надсилаємо {len(tg_msgs)} повідомлень у теми групи {CHAT_ID}...")
            asyncio.run(send_messages_by_type(CHAT_ID, tg_msgs))
        else:
            plain = [text for _, text in tg_msgs]
            log.info(f"Надсилаємо {len(plain)} повідомлень у основний канал...")
            asyncio.run(send_messages(CHAT_ID, plain))

    if ALL_PRODUCTS_CHAT_ID and ALL_PRODUCTS_CHAT_ID != CHAT_ID and all_tg_msgs:
        sent_any = True
        log.info(f"Надсилаємо {len(all_tg_msgs)} повідомлень у канал всіх товарів...")
        asyncio.run(send_messages(ALL_PRODUCTS_CHAT_ID, all_tg_msgs))

    if all_tg_msgs:
        sent_any = True
        log.info(f"Надсилаємо {len(all_tg_msgs)} повідомлень у тему 'Всі події'...")
        asyncio.run(send_messages(CHAT_ID, all_tg_msgs, thread_id=int(THREAD_ALL)))

    if kharkiv_msgs:
        sent_any = True
        log.info(f"Надсилаємо {len(kharkiv_msgs)} повідомлень у тему Харків...")
        asyncio.run(send_messages(CHAT_ID, kharkiv_msgs, thread_id=int(THREAD_KHARKIV)))

    if not sent_any:
        log.info("Нічого нового.")

    save_state(state)
    save_dashboard(state, events, cfg)
    log.info("Готово.")
    log.info("═" * 50)

if __name__ == "__main__":
    main()
