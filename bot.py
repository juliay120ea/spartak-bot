import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]           # ваш chat_id (или group id)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 3600))  # секунды (по умолч. 1 час)

SECTIONS = [
    {
        "name": "📰 Новости",
        "list_url": "https://spartak.com/media/news",
        "api_url": "https://spartak.com/api/v2/news?page=1&limit=20",
        "base_url": "https://spartak.com",
        "slug_prefix": "/media/news/",
    },
    # Раскомментируйте когда будете добавлять билеты:
    # {
    #     "name": "🎟 Билеты",
    #     "list_url": "https://spartak.com/tickets",
    #     "api_url": "https://spartak.com/api/v2/tickets?page=1&limit=20",
    #     "base_url": "https://spartak.com",
    #     "slug_prefix": "/tickets/",
    # },
]

SEEN_FILE = Path("seen_urls.json")
# ───────────────────────────────────────────────────────────────────────────────


def load_seen() -> dict[str, set]:
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        return {k: set(v) for k, v in data.items()}
    return {}


def save_seen(seen: dict[str, set]):
    SEEN_FILE.write_text(
        json.dumps({k: list(v) for k, v in seen.items()}, ensure_ascii=False, indent=2)
    )


async def fetch_news_urls(section: dict) -> list[dict]:
    """
    Пытается получить ссылки тремя способами (от простого к сложному):
    1. JSON API endpoint
    2. HTML парсинг (если вдруг SSR)
    3. Sitemap
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SpartakBot/1.0)",
        "Accept": "application/json, text/html",
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:

        # ── Попытка 1: JSON API ──────────────────────────────────────────────
        if section.get("api_url"):
            try:
                r = await client.get(section["api_url"], headers=headers)
                if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    data = r.json()
                    items = _extract_from_json(data, section)
                    if items:
                        logger.info(f"[{section['name']}] Получено {len(items)} статей через API")
                        return items
            except Exception as e:
                logger.warning(f"[{section['name']}] API недоступен: {e}")

        # ── Попытка 2: HTML парсинг ──────────────────────────────────────────
        try:
            r = await client.get(section["list_url"], headers={**headers, "Accept": "text/html"})
            if r.status_code == 200:
                items = _extract_from_html(r.text, section)
                if items:
                    logger.info(f"[{section['name']}] Получено {len(items)} ссылок из HTML")
                    return items
        except Exception as e:
            logger.warning(f"[{section['name']}] HTML парсинг не удался: {e}")

        # ── Попытка 3: Sitemap ───────────────────────────────────────────────
        try:
            sitemap_url = section["base_url"] + "/sitemap.xml"
            r = await client.get(sitemap_url, headers=headers)
            if r.status_code == 200:
                items = _extract_from_sitemap(r.text, section)
                if items:
                    logger.info(f"[{section['name']}] Получено {len(items)} ссылок из sitemap")
                    return items
        except Exception as e:
            logger.warning(f"[{section['name']}] Sitemap недоступен: {e}")

    logger.error(f"[{section['name']}] Не удалось получить данные ни одним способом")
    return []


def _extract_from_json(data, section) -> list[dict]:
    """Пытается достать items из типичных JSON-структур CMS."""
    items = []
    prefix = section["slug_prefix"]
    base = section["base_url"]

    # Ищем массив на разных уровнях вложенности
    candidates = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("items", "data", "news", "articles", "results", "content"):
            if key in data and isinstance(data[key], list):
                candidates = data[key]
                break

    for item in candidates[:50]:
        if not isinstance(item, dict):
            continue
        # URL
        url = (
            item.get("url")
            or item.get("slug")
            or item.get("link")
            or item.get("href")
            or item.get("path")
        )
        if not url:
            # Попробуем из id
            uid = item.get("id") or item.get("uuid")
            if uid:
                url = f"{prefix}{uid}"

        if url and not url.startswith("http"):
            url = base + url

        if not url or prefix.rstrip("/") not in url:
            continue

        title = (
            item.get("title")
            or item.get("name")
            or item.get("headline")
            or "Без названия"
        )
        published = item.get("publishedAt") or item.get("published_at") or item.get("date") or ""

        items.append({"url": url, "title": str(title), "published": str(published)})

    return items


def _extract_from_html(html: str, section) -> list[dict]:
    """Парсит HTML страницу — ищет ссылки с нужным префиксом."""
    soup = BeautifulSoup(html, "html.parser")
    prefix = section["slug_prefix"]
    base = section["base_url"]
    seen_urls = set()
    items = []

    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if not href.startswith("http"):
            href = base + href
        if prefix.rstrip("/") not in href:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        title = tag.get_text(strip=True) or tag.get("title") or "Без названия"
        items.append({"url": href, "title": title, "published": ""})

    return items


def _extract_from_sitemap(xml: str, section) -> list[dict]:
    """Ищет нужные URL в sitemap.xml."""
    prefix = section["slug_prefix"]
    items = []
    urls = re.findall(r"<loc>(.*?)</loc>", xml)
    for url in urls:
        if prefix.rstrip("/") in url:
            items.append({"url": url, "title": url.split("/")[-1], "published": ""})
    return items


async def check_section(section: dict, seen: dict[str, set], bot: Bot) -> int:
    """Проверяет раздел, отправляет уведомления о новых страницах. Возвращает кол-во новых."""
    key = section["list_url"]
    if key not in seen:
        seen[key] = set()

    items = await fetch_news_urls(section)
    if not items:
        return 0

    new_items = [i for i in items if i["url"] not in seen[key]]

    if not seen[key]:
        # Первый запуск — просто запоминаем, не спамим
        logger.info(f"[{section['name']}] Первый запуск, запоминаем {len(items)} существующих URL")
        seen[key] = {i["url"] for i in items}
        return 0

    for item in reversed(new_items):  # старые сначала
        seen[key].add(item["url"])
        msg = (
            f"{section['name']} — *новая публикация!*\n\n"
            f"📌 {item['title']}\n"
            f"🔗 {item['url']}"
        )
        if item["published"]:
            msg += f"\n🕐 {item['published']}"
        await bot.send_message(
            chat_id=CHAT_ID,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
        )
        logger.info(f"Отправлено уведомление: {item['url']}")
        await asyncio.sleep(0.5)  # не спамить Telegram

    return len(new_items)


async def main():
    bot = Bot(token=BOT_TOKEN)
    seen = load_seen()

    me = await bot.get_me()
    logger.info(f"Бот запущен: @{me.username}")

    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "🤖 *Spartak Monitor запущен*\n"
            f"Отслеживаю {len(SECTIONS)} раздел(а).\n"
            f"Интервал проверки: {CHECK_INTERVAL // 60} мин."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    while True:
        logger.info("── Проверка разделов ──")
        total_new = 0
        for section in SECTIONS:
            try:
                n = await check_section(section, seen, bot)
                total_new += n
            except Exception as e:
                logger.error(f"[{section['name']}] Ошибка: {e}", exc_info=True)

        save_seen(seen)
        logger.info(f"Новых публикаций: {total_new}. Следующая проверка через {CHECK_INTERVAL}с.")
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
