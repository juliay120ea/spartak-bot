# Spartak Monitor Bot 🤖

Телеграм-бот, который следит за новыми публикациями на spartak.com и мгновенно присылает уведомления.

## Как запустить локально (для теста)

### 1. Установи зависимости
```bash
pip install -r requirements.txt
```

### 2. Создай бота в Telegram
1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot`
2. Скопируй **токен** (выглядит как `123456:ABC-DEF...`)

### 3. Узнай свой Chat ID
1. Напиши боту любое сообщение
2. Открой в браузере: `https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates`
3. Найди поле `"chat": {"id": XXXXXXX}` — это твой `CHAT_ID`

### 4. Запусти бота
```bash
export BOT_TOKEN="123456:ABC-DEF..."
export CHAT_ID="123456789"
export CHECK_INTERVAL=3600   # интервал в секундах (3600 = 1 час)

python bot.py
```

---

## Деплой на Railway (24/7 бесплатно)

### 1. Создай репозиторий на GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/ВАШ_USERNAME/spartak-bot.git
git push -u origin main
```

### 2. Деплой на Railway
1. Зайди на [railway.app](https://railway.app) → Sign in with GitHub
2. **New Project** → **Deploy from GitHub repo** → выбери `spartak-bot`
3. Railway автоматически найдёт `Dockerfile` и задеплоит

### 3. Добавь переменные окружения
В Railway: **Variables** → добавь:
| Имя | Значение |
|-----|---------|
| `BOT_TOKEN` | токен от BotFather |
| `CHAT_ID` | твой chat id |
| `CHECK_INTERVAL` | `3600` (или меньше для теста) |

### 4. Готово!
Railway запустит бота и он будет работать 24/7 ✅

---

## Добавить раздел билетов

В `bot.py` найди `SECTIONS` и раскомментируй блок с билетами:
```python
{
    "name": "🎟 Билеты",
    "list_url": "https://spartak.com/tickets",
    ...
}
```

---

## Как это работает

Бот пробует три способа получить список страниц (от простого к сложному):
1. **JSON API** — если сайт отдаёт данные через API
2. **HTML парсинг** — если страница рендерится на сервере
3. **Sitemap** — `sitemap.xml` часто содержит все URL сайта

При первом запуске бот запоминает все существующие страницы и **не спамит** — только новые.
