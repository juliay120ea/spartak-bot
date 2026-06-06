import asyncio
import json
import logging
import os
import hashlib
import re
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
        "type": "spartak_tickets",
        "list_url": "https://tickets.spartak.com/matches",
        "base_url": "https://tickets.spartak.com",
    },
    {
        "name": "🏒 Билеты ЦСКА (хоккей)",
        "type": "cska_tickets",
        "list_url": "https://tickets.cska-hockey.ru/",
        "base_url": "https://tickets.cska-hockey.ru",
    },
]

SEEN_FILE = Path("seen_urls.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
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


# ── Новости Спартак ────────────────────────────────────────────────────────────

async def fetch_spartak_news(section):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Попытка 1: JSON API
        try:
            r = await client.get(section["api_url"], headers=HEADERS)
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                data = r.json()
                candidates = data if isinstance(data, list) else data.get("items", data.get("data", data.get("news", [])))
                items = []
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
                    title = str(item.get("title") or item.get("name") or "Новость")
                    # Анонс/описание
                    preview = str(item.get("preview") or item.get("description") or item.get("subtitle") or item.get("lead") or "")
                    items.append({"id": make_id(url), "url": url, "title": title, "preview": preview[:200]})
                if items:
                    return items
        except Exception as e:
            logger.warning(f"API error: {e}")

        # Попытка 2: HTML
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
            items.append({"id": make_id(href), "url": href, "title": title[:150], "preview": ""})
        return items


# ── Билеты Спартак (футбол) ────────────────────────────────────────────────────

async def fetch_spartak_tickets(section):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(section["list_url"], headers=HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    seen_ids = set()

    # Ищем все ссылки на конкретные матчи
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = href if href.startswith("http") else section["base_url"] + href
        # Ссылки на матчи обычно содержат UUID или числовой ID
        if not ("/match" in href or re.search(r'/[0-9a-f-]{8,}', href)):
            continue

        uid = make_id(full_url)
        if uid in seen_ids:
            continue
        seen_ids.add(uid)

        # Пытаемся найти текст матча в родительском блоке
        parent = a.find_parent(["div", "article", "li", "section"])
        text = parent.get_text(separator=" ", strip=True) if parent else a.get_text(strip=True)

        # Вычленяем название матча (команды)
        title = _extract_match_title(text) or a.get_text(strip=True) or "Матч"
        date = _extract_date(text)
        seats = _extract_seats(text)

        items.append({
            "id": uid,
            "url": full_url,
            "title": title,
            "date": date,
            "seats": seats,
        })

    # Если ссылок на матчи нет — парсим сами блоки с текстом
    if not items:
        for tag in soup.find_all(["div", "article", "li"], class_=re.compile(r'match|event|game|card', re.I)):
            text = tag.get_text(separator=" ", strip=True)
            if len(text) < 5:
                continue
            a = tag.find("a", href=True)
            url = section["list_url"]
            if a:
                href = a["href"]
                url = href if href.startswith("http") else section["base_url"] + href
            uid = make_id(text[:80])
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            title = _extract_match_title(text) or text[:80]
            date = _extract_date(text)
            seats = _extract_seats(text)
            items.append({"id": uid, "url": url, "title": title, "date": date, "seats": seats})

    return items


# ── Билеты ЦСКА (хоккей) ──────────────────────────────────────────────────────

async def fetch_cska_tickets(section):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(section["list_url"], headers=HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    seen_ids = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = href if href.startswith("http") else section["base_url"] + href
        if not any(x in href for x in ["/event/", "/match", "/game", "/ticket", "/leagues/"]):
            continue

        uid = make_id(full_url)
        if uid in seen_ids:
            continue
        seen_ids.add(uid)

        parent = a.find_parent(["div", "article", "li", "tr"])
        text = parent.get_text(separator=" ", strip=True) if parent else a.get_text(strip=True)
        title = _extract_match_title(text) or a.get_text(strip=True) or "Матч ЦСКА"
        date = _extract_date(text)
        seats = _extract_seats(text)

        items.append({"id": uid, "url": full_url, "title": title, "date": date, "seats": seats})

    return items


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _extract_match_title(text):
    """Ищет 'Команда — Команда' или 'Команда vs Команда'"""
    m = re.search(r'([А-ЯA-Z][а-яёa-z]+(?:\s[А-ЯA-Zа-яёa-z]+)*)\s*[—\-–vs]+\s*([А-ЯA-Z][а-яёa-z]+(?:\s[А-ЯA-Zа-яёa-z]+)*)', text)
    if m:
        return f"{m.group(1).strip()} — {m.group(2).strip()}"
    return None


def _extract_date(text):
    """Ищет дату в тексте"""
    m = re.search(r'\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s*,\s*\d{2}:\d{2})?', text, re.I)
    if m:
        return m.group(0).strip()
    m = re.search(r'\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?(?:\s+\d{2}:\d{2})?', text)
    if m:
        return m.group(0).strip()
    return ""


def _extract_seats(text):
    """Ищет количество доступных мест"""
    m = re.search(r'[Дд]оступно\s+([\d\s]+)\s*мест', text)
    if m:
        return f"Доступно {m.group(1).strip()} мест"
    m = re.search(r'([\d\s]+)\s*мест', text)
    if m:
        return f"{m.group(1).strip()} мест"
    return ""


# ── Форматирование уведомлений ────────────────────────────────────────────────

def format_news_message(section, item):
    lines = [f"📰 *Новая новость — Спартак*\n"]
    lines.append(f"*{item['title']}*")
    if item.get("preview"):
        lines.append(f"\n_{item['preview']}_")
    lines.append(f"\n🔗 [Читать]({item['url']})")
    return "\n".join(lines)


def format_ticket_message(section, item):
    emoji = "🎟" if "spartak" in section["list_url"] else "🏒"
    club = "Спартак (футбол)" if "spartak" in section["list_url"] else "ЦСКА (хоккей)"
    lines = [f"{emoji} *Новый матч в продаже — {club}*\n"]
    lines.append(f"⚽ *{item['title']}*" if "spartak" in section["list_url"] else f"🏒 *{item['title']}*")
    if item.get("date"):
        lines.append(f"📅 {item['date']}")
    if item.get("seats"):
        lines.append(f"💺 {item['seats']}")
    lines.append(f"\n🎫 [Купить билет]({item['url']})")
    return "\n".join(lines)


# ── Основная логика ───────────────────────────────────────────────────────────

async def fetch_items(section):
    try:
        t = section["type"]
        if t == "news":
            return await fetch_spartak_news(section)
        elif t == "spartak_tickets":
            return await fetch_spartak_tickets(section)
        elif t == "cska_tickets":
            return await fetch_cska_tickets(section)
    except Exception as e:
        logger.error(f"[{section['name']}] Ошибка: {e}", exc_info=True)
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
        if section["type"] == "news":
            msg = format_news_message(section, item)
        else:
            msg = format_ticket_message(section, item)

        await bot.send_message(
            chat_id=CHAT_ID,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )
        logger.info(f"Отправлено: {item['title'][:60]}")
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
            "📰 Новости Спартак — с анонсом и ссылкой\n"
            "🎟 Билеты Спартак (футбол) — с датой и ссылкой на покупку\n"
            "🏒 Билеты ЦСКА (хоккей) — с датой и ссылкой на покупку\n\n"
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
