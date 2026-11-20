# 🎧 Spotify DJ Bot

Telegram-бот для знакомства с музыкой: Google Gemini (`gemini-2.5-flash`) придумывает
и рассказывает, Spotify Web API находит реальные треки.

Режимы: 🎸 Жанры · 🕰 Эпохи · 🔥 Главные хиты · 📜 Трек с историей · 🎬 Кино, игры и отсылки ·
🎲 Случайный трек. Можно просто писать запрос текстом — LLM-роутер сам выберет режим.
Есть ежедневная рассылка (1–3 трека в день) и защита от повторов (SQLite).

## Файлы

| Файл | Что внутри |
|---|---|
| `bot.py` | точка входа: хэндлеры aiogram 3, клавиатуры, APScheduler, health-сервер aiohttp |
| `services.py` | Gemini (JSON-ответы) + Spotify search, подбор и фильтрация треков |
| `database.py` | SQLite: `seen_tracks`, `subscriptions`, `last_requests` |
| `.env.example` | шаблон переменных окружения |

## Ключи (все бесплатные)

1. **BOT_TOKEN** — в Telegram: @BotFather → `/newbot`.
2. **SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET** — [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) →
   Create App, Redirect URI `http://localhost:8888/callback`, галочка *Web API*.
3. **GEMINI_API_KEY** — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → Create API key.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # и впишите ключи
python bot.py
```

Нужен Python 3.11+.

## Бесплатный деплой: Render + UptimeRobot

1. Залейте репозиторий на GitHub (`.env` в `.gitignore` и в репозиторий не попадает).
2. [render.com](https://render.com) → **New + → Web Service** → подключите репозиторий.
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot.py`
   - Instance Type: **Free**
   - Environment Variables: `BOT_TOKEN`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
     `GEMINI_API_KEY`, `TIMEZONE_OFFSET` (по умолчанию `3`), при желании `PYTHON_VERSION=3.12.8`.
3. После деплоя скопируйте адрес вида `https://ваш-бот.onrender.com` — по `GET /` бот отвечает `200 OK`.
4. [uptimerobot.com](https://uptimerobot.com) → **Add New Monitor → HTTP(s)** → этот адрес, интервал 5 минут,
   чтобы Render не усыплял сервис.

> ⚠️ Диск бесплатного Render-инстанса не постоянный: `bot_data.db` (история и подписки)
> обнуляется при каждом редеплое/перезапуске. Для постоянного хранения подключите
> платный Persistent Disk и укажите путь в `DB_PATH`, либо используйте внешнюю БД.

## Заметки

- Эндпоинт Spotify `/recommendations` закрыт с конца 2024 года, поэтому подбор работает так:
  Gemini предлагает несколько реальных треков → `sp.search` находит их → уже показанные
  пользователю отфильтровываются по SQLite. Если точный поиск `track:… artist:…` пуст,
  повторяется упрощённый поиск `"{artist} {track}"`.
- Для режима «Главные хиты» результаты сортируются по `popularity` и выбирается случайный
  трек из топ-5. Если Spotify не отдаёт `popularity` для вашего приложения, используется
  порядок, предложенный нейросетью.
- Рассылка: 10:00, 15:00 и 19:30 по `TIMEZONE_OFFSET`, пауза 1.5 с между пользователями
  (лимит бесплатного Gemini — ~15 запросов в минуту). Если пользователь заблокировал бота,
  подписка деактивируется.
