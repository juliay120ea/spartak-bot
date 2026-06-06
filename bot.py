import asyncio
import json
import logging
import os
import hashlib
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 3600))

SECTIONS = [
    {
        "name": "📰 Новости Спартак",
        "type": "news",
        "list_url": "https://spartak.com/media/news",
        "api_url": "https://spartak.com/api/v2/news?page=1&limit=20",
        "base_url": "https://spartak.com",
        "slug_prefix": "/media/news/",
    },
    {
        "name": "🎟 Билеты Спартак (футбол)",
        "type": "tickets",
        "list_url": "https://tickets.spartak.com/matches",
        "base_url": "https://tickets.spartak.com",
        "slug_prefix": "/matches/",
    },
    {
        "name": "🏒 Билеты ЦСКА (хоккей)",
        "type": "tickets",
        "list_url": "https://tickets.cska-hockey.ru/",
        "base_url": "https://tickets.cska-hockey.ru",
        "slug_prefix": "/leagues/",
    },
]

SEEN_FILE = Path("seen_urls.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def load_seen():
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        return {k: set(v) for k, v in data.items()}
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(
        json.dumps({k: list(v) for k, v in seen.items()}, ensure_ascii=False, indent=2)
    )


def make_id(text):
    return hashlib.md5(text.encode()).hexdigest()[:12]


async def fetch_spartak_news(section):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            r = await client.get(section["api_url"], headers=HEADERS)
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                data = r.json()
                items = []
                candidates = data if isinstance(data, list) else data.get("items", data.get("data", data.get("news", [])))
                for item in candidates[:30]:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url") or item.get("slug") or item.get("link") or ""
                    uid = item.get("id") or item.get("uuid") or ""
                    if not url and uid:
                        url = f"{section['slug_prefix']}{uid}"
                    if url and not url.startswith("http"):
                        url = section["base_url"] + url
                    if not url:
                        continue
                    title = item.get("title") or item.get("name") or "Новость"
                    items.append({"id": make_id(url), "url": url, "title": str(title)})
                if items:
                    return items
        except Exception as e:
            logger.warning(f"API error: {e}")

        # fallback: HTML
        r = await client.get(section["list_url"], headers=HEADERS)
        soup = BeautifulSoup(r.text, "html.parser")
        items = []
        seen_urls = set()
        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if not href.startswith("http"):
                href = section["base_url"] + href
            if section["slug_prefix"].rstrip("/") not in href or href in seen_urls:
                continue
            seen_urls.add(href)
            title = tag.get_text(strip=True) or "Новость"
            items.append({"id": make_id(href), "url": href, "title": title[:100]})
        return items


async def fetch_spartak_tickets(section):
    """Парсит список матчей с tickets.spartak.com"""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(section["list_url"], headers=HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []

    # Ищем блоки матчей — у них есть название команды и дата
    # Структура: ищем любые контейнеры с текстом о матчах
    for tag in soup.find_all(["h2", "h3", "h4", "p", "div", "span"]):
        text = tag.get_text(strip=True)
        # Ищем строки типа "Спартак — Локомотив" или с датой
        if ("Спартак" in text or "Spartak" in text) and len(text) > 5 and len(text) < 150:
            uid = make_id(text)
            # Ищем ссылку рядом
            link = tag.find_parent("a") or tag.find("a")
            url = section["list_url"]
            if link and link.get("href"):
                href = link["href"]
                url = href if href.startswith("http") else section["base_url"] + href
            items.append({"id": uid, "url": url, "title": text})

    # Дополнительно ищем все ссылки на матчи
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if "/matches/" in href or "/match/" in href:
            full = href if href.startswith("http") else section["base_url"] + href
            title = tag.get_text(strip=True) or full
            items.append({"id": make_id(full), "url": full, "title": title[:100]})

    # Убираем дубли по id
    seen = set()
    unique = []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    return unique


async def fetch_cska_tickets(section):
    """Парсит список матчей с tickets.cska-hockey.ru"""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(section["list_url"], headers=HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []

    # Ищем ссылки на лиги/матчи
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if any(x in href for x in ["/leagues/", "/match", "/event", "/game"]):
            full = href if href.startswith("http") else section["base_url"] + href
            title = tag.get_text(strip=True) or full
            if title and len(title) > 1:
                items.append({"id": make_id(full), "url": full, "title": title[:100]})

    # Ищем блоки с матчами по тексту
    for tag in soup.find_all(["div", "li", "article"]):
        text = tag.get_text(strip=True)
        if ("ЦСКА" in text or "CSKA" in text) and 5 < len(text) < 200:
            link = tag.find("a")
            url = section["list_url"]
            if link and link.get("href"):
                href = link["href"]
                url = href if href.startswith("http") else section["base_url"] + href
            uid = make_id(text)
            items.append({"id": uid, "url": url, "title": text[:100]})

    seen = set()
    unique = []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    return unique


async def fetch_items(section):
    try:
        if section["type"] == "news":
            return await fetch_spartak_news(section)
        elif section["type"] == "tickets" and "spartak" in section["list_url"]:
            return await fetch_spartak_tickets(section)
        elif section["type"] == "tickets" and "cska" in section["list_url"]:
            return await fetch_cska_tickets(section)
    except Exception as e:
        logger.error(f"[{section['name']}] Ошибка получения данных: {e}", exc_info=True)
    return []


async def check_section(section, seen, bot):
    key = section["list_url"]
    if key not in seen:
        seen[key] = set()

    items = await fetch_items(section)
    if not items:
        logger.warning(f"[{section['name']}] Нет данных")
        return 0

    new_items = [i for i in items if i["id"] not in seen[key]]

    if not seen[key]:
        logger.info(f"[{section['name']}] Первый запуск, запоминаем {len(items)} элементов")
        seen[key] = {i["id"] for i in items}
        return 0

    for item in reversed(new_items):
        seen[key].add(item["id"])
        emoji = "🎟" if section["type"] == "tickets" else "📰"
        msg = (
            f"{emoji} *{section['name']}* — новое!\n\n"
            f"📌 {item['title']}\n"
            f"🔗 {item['url']}"
        )
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Отправлено: {item['title'][:50]}")
        await asyncio.sleep(0.5)

    return len(new_items)


async def main():
    bot = Bot(token=BOT_TOKEN)
    seen = load_seen()

    me = await bot.get_me()
    logger.info(f"Бот запущен: @{me.username}")

    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "🤖 *Spartak & ЦСКА Monitor запущен*\n\n"
            "Отслеживаю:\n"
            "📰 Новости Спартак\n"
            "🎟 Билеты Спартак (футбол)\n"
            "🏒 Билеты ЦСКА (хоккей)\n\n"
            f"Проверка каждые {CHECK_INTERVAL // 60} мин."
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
                logger.error(f"[{section['name']}] Критическая ошибка: {e}", exc_info=True)
        save_seen(seen)
        logger.info(f"Новых: {total_new}. Следующая проверка через {CHECK_INTERVAL}с.")
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
