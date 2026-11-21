# 🎧 Spotify DJ Bot

Telegram-бот для знакомства с музыкой: Google Gemini (`gemini-3.8-flash`) придумывает
и рассказывает, Spotify Web API находит реальные треки.

Режимы: 🎸 Жанры · 🕰 Эпохи · 🔥 Главные хиты · 📜 Трек с историей · 🎬 Кино, игры и отсылки ·
🎲 Случайный трек. Можно просто писать запрос текстом — LLM-роутер сам выберет режим.
Есть ежедневная рассылка (1–3 трека в день) и защита от повторов (SQLite).

🕘 **История** (`/history`, слово «история» или кнопка в меню): все треки, которые прислал бот
(включая рассылку) — со ссылками, режимом/темой и датой, по 10 на страницу. Историю можно
сохранить одним плейлистом в Spotify или очистить (после очистки треки могут повторяться).

⚙️ **Настройки** (`/settings` или кнопка в меню): галка «🚫 Без нецензурной лексики» —
бот пропускает треки с пометкой Explicit в Spotify во всех режимах, рассылке, создаваемых
плейлистах и рекомендациях; в анализе плейлиста показывается, сколько в нём таких треков.

🎶 **Плейлисты Spotify** (после входа через `/connect`):
- собрать плейлист по любому описанию («бодрый инди 2010-х для пробежки, 25 треков») прямо в ваш Spotify;
- проанализировать свой плейлист или любой по ссылке: топ артистов, эпохи, жанры, настроение,
  «портрет слушателя» и 8 рекомендаций, которые можно одной кнопкой добавить в плейлист
  или сохранить отдельным плейлистом;
- сохранить все треки, которые показал бот, в один плейлист.

## Файлы

| Файл | Что внутри |
|---|---|
| `bot.py` | точка входа: хэндлеры aiogram 3, клавиатуры, APScheduler, health-сервер aiohttp |
| `services.py` | Gemini (JSON-ответы) + Spotify search, подбор и фильтрация треков |
| `playlists.py` | вход в Spotify (OAuth), создание и анализ плейлистов |
| `database.py` | SQLite: `seen_tracks`, `subscriptions`, `last_requests`, `user_settings`, `spotify_tokens`, `oauth_states` |
| `.env.example` | шаблон переменных окружения |

## Ключи (все бесплатные)

1. **BOT_TOKEN** — в Telegram: @BotFather → `/newbot`.
2. **SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET** — [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) →
   Create App, Redirect URI `http://localhost:8888/callback`, галочка *Web API*.
   Для плейлистов добавьте в настройках приложения ещё один Redirect URI — адрес бота
   `https://ваш-бот.onrender.com/spotify/callback` (локально: `http://127.0.0.1:8080/spotify/callback`)
   и укажите его же в `SPOTIFY_REDIRECT_URI`. Пока приложение в режиме Development, войти смогут
   только пользователи, добавленные в разделе **User Management** приложения (имя + email их аккаунта Spotify).
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
     `GEMINI_API_KEY`, `SPOTIFY_REDIRECT_URI`, `TIMEZONE_OFFSET` (по умолчанию `3`), при желании `PYTHON_VERSION=3.12.8`.
3. После деплоя скопируйте адрес вида `https://ваш-бот.onrender.com` — по `GET /` бот отвечает `200 OK`.
4. Засыпать бот не будет: на Render он сам каждые 10 минут открывает свой адрес
   (`RENDER_EXTERNAL_URL` Render подставляет автоматически), и Render видит активность.
   На другом хостинге укажите адрес в `KEEPALIVE_URL` или используйте внешний пингер:
   [uptimerobot.com](https://uptimerobot.com) → **Add New Monitor → HTTP(s)** → этот адрес, интервал 5 минут,
   чтобы Render не усыплял сервис.

> ⚠️ Диск бесплатного Render-инстанса не постоянный: `bot_data.db` (история и подписки)
> обнуляется при каждом редеплое/перезапуске (вместе с ней — вход в Spotify: нужно будет снова `/connect`). Для постоянного хранения подключите
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
- Анализ чужих плейлистов: Spotify может не отдавать содержимое плейлистов, которые вам не
  принадлежат, приложениям в режиме Development (а также свои редакционные/алгоритмические
  плейлисты Spotify) — в этом случае бот так и скажет. Свои плейлисты анализируются всегда.
- Токены Spotify хранятся в SQLite и обновляются автоматически; `/disconnect` удаляет их.
