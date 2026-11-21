"""Spotify DJ bot — entry point.

aiogram 3.x handlers + APScheduler daily digest + tiny aiohttp health server
(GET / -> 200 OK) so the bot can live on free Render/Koyeb web services.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiohttp import ClientSession, ClientTimeout, web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

import database as db  # noqa: E402  (DB_PATH is read from env after load_dotenv)
from playlists import Analysis, PlaylistService, extract_playlist_id  # noqa: E402
from services import MODES, MusicService, ServiceError, TrackCard  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("bot")


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Environment variable {name} is not set (see .env.example)")
    return value


BOT_TOKEN = _require("BOT_TOKEN")
TZ = timezone(timedelta(hours=float(os.environ.get("TIMEZONE_OFFSET", "3"))))

service = MusicService(
    gemini_api_key=_require("GEMINI_API_KEY"),
    spotify_client_id=_require("SPOTIFY_CLIENT_ID"),
    spotify_client_secret=_require("SPOTIFY_CLIENT_SECRET"),
    model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
)
playlists = PlaylistService(
    music=service,
    client_id=_require("SPOTIFY_CLIENT_ID"),
    client_secret=_require("SPOTIFY_CLIENT_SECRET"),
    # e.g. https://your-bot.onrender.com/spotify/callback (must also be added in the Spotify dashboard)
    redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", "").strip(),
)

router = Router()

# Submenu presets: (button label, topic sent to the LLM)
def _env_list(name: str, default: list[str]) -> list[str]:
    """Comma-separated list from an environment variable (to edit menus on Render without code)."""
    raw = os.environ.get(name, "")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items[:40] if items else default


GENRES = _env_list("GENRES", [
    "Рок", "Поп", "Хип-хоп", "Электроника", "Инди", "Метал", "Синтвейв",
    "Пост-панк", "Трип-хоп", "Неоклассика", "Олдскул Хип-хоп", "Босса-нова",
    "Драм-н-бэйс", "K-pop",
])
ERAS = _env_list("ERAS", ["1960-е", "1970-е", "1980-е", "1990-е", "2000-е", "2010-е"])
HITS = _env_list("HITS", ["Хиты всех времён", "Хиты 80-х", "Хиты 90-х", "Хиты 2000-х",
                          "Хиты этого десятилетия"])
REFS = _env_list("REFS", ["Фильмы Тарантино", "Связанные с космосом", "Саундтреки видеоигр",
                          "Аниме", "Сериалы HBO", "Атмосфера ночного города"])
PRESETS = {"genre": GENRES, "era": ERAS, "hits": HITS, "refs": REFS}

INPUT_HINTS = {
    "genre": "Выберите жанр или напишите любой другой текстом 👇",
    "era": "Выберите десятилетие или напишите свою эпоху (например, «эпоха диско») 👇",
    "hits": "Выберите вариант или напишите тему/год (например, «рок 1985») 👇",
    "refs": "Выберите тему или напишите свою: фильм, игру, книгу, атмосферу 👇",
}

SCHEDULE_TEXT = {
    1: "1 трек в день (10:00)",
    2: "2 трека в день (10:00 и 19:30)",
    3: "3 трека в день (10:00, 15:00 и 19:30)",
}

# Hard limit for one track pick (Gemini + Spotify), seconds
PICK_TIMEOUT = 110

# Modes waiting for free-text input after a submenu was opened: user_id -> mode
pending_input: dict[int, str] = {}
# One request at a time per user (protects from double clicks)
user_locks: dict[int, asyncio.Lock] = {}
# Last playlist analysis per user (for "add recommendations" buttons)
last_analysis: dict[int, Analysis] = {}


# ----------------------------------------------------------------- keyboards

def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def main_menu_kb() -> InlineKeyboardMarkup:
    return kb([
        [(MODES["genre"], "mode:genre"), (MODES["era"], "mode:era")],
        [(MODES["hits"], "mode:hits"), (MODES["story"], "mode:story")],
        [(MODES["refs"], "mode:refs")],
        [(MODES["random"], "mode:random")],
        [("🎶 Плейлисты Spotify", "pl_menu"), ("🕘 История", "hist:0")],
        [("⏰ Ежедневная рассылка (1-3 в день)", "sub_menu")],
        [("⚙️ Настройки", "settings")],
    ])


def submenu_kb(mode: str) -> InlineKeyboardMarkup:
    items = PRESETS[mode]
    rows, row = [], []
    for i, label in enumerate(items):
        row.append((label, f"p:{mode}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("🔙 В главное меню", "menu")])
    return kb(rows)


def track_kb() -> InlineKeyboardMarkup:
    return kb([
        [("🔄 Ещё так же", "more"), ("🎲 Случайный", "mode:random")],
        [("📌 Присылать такое ежедневно", "sub_here")],
        [("🔙 В главное меню", "menu")],
    ])


def schedule_kb() -> InlineKeyboardMarkup:
    return kb([
        [("1 трек в день", "sub:1")],
        [("2 трека в день", "sub:2")],
        [("3 трека в день", "sub:3")],
        [("🚫 Отключить рассылку", "sub:0")],
        [("🔙 В главное меню", "menu")],
    ])


# --------------------------------------------------------------- formatting

def topic_title(mode: str, topic: str) -> str:
    title = MODES.get(mode, "🎵")
    return f"{title} · {topic}" if topic else title


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_card(card: TrackCard, header: str = "") -> str:
    e = html.escape
    parts = []
    if header:
        parts.append(header)
    parts.append(f"<b>{e(topic_title(card.mode, card.topic))}</b>")
    if card.intro:
        parts.append(e(_cut(card.intro, 1200)))
    if card.why:
        parts.append(e(_cut(card.why, 1500)))
    info = [f"🎵 <b>{e(card.name)}</b>", f"👤 {e(card.artists)}"]
    if card.year:
        info.append(f"📅 {e(card.year)}")
    if card.mode == "hits" and card.popularity is not None:
        info.append(f"🔥 Индекс популярности Spotify: <b>{card.popularity}</b>/100")
    info.append(f'🔗 <a href="{e(card.url)}">Слушать в Spotify</a>')
    parts.append("\n".join(info))
    return "\n\n".join(parts)


WELCOME = (
    "🎧 <b>Привет! Я — музыкальный DJ-бот.</b>\n\n"
    "Помогу открыть новую музыку: жанры, эпохи, главные хиты, песни с тайной "
    "историей, саундтреки и неожиданные находки со всего мира.\n\n"
    "Выберите режим ниже или просто напишите, что хочется послушать — например, "
    "<i>«что-нибудь из фильмов Тарантино»</i> или <i>«японский сити-поп»</i>.\n\n"
    "🎶 А ещё я умею собирать плейлисты прямо в ваш Spotify и анализировать "
    "существующие — пришлите ссылку на плейлист."
)


# ----------------------------------------------------------------- delivery

async def deliver(bot: Bot, chat_id: int, user_id: int, mode: str, topic: str) -> None:
    lock = user_locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        await bot.send_message(chat_id, "⏳ Уже подбираю трек, секунду…")
        return
    async with lock:
        pending_input.pop(user_id, None)
        status = await bot.send_message(chat_id, "🎧 Подбираю трек…")
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            card = await asyncio.wait_for(service.pick(user_id, mode, topic), timeout=PICK_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("pick() timed out for user %s (%s/%s)", user_id, mode, topic)
            await _safe_delete(status)
            await bot.send_message(
                chat_id,
                "😕 Слишком долго: нейросеть или Spotify сейчас не отвечают. Попробуйте через минуту.",
                reply_markup=track_kb(),
            )
            await db.set_last_request(user_id, mode, topic)
            return
        except ServiceError as exc:
            await _safe_delete(status)
            await bot.send_message(chat_id, f"😕 {html.escape(str(exc))}", reply_markup=track_kb())
            await db.set_last_request(user_id, mode, topic)
            return
        except Exception:
            log.exception("Unexpected error while picking a track")
            await _safe_delete(status)
            await bot.send_message(
                chat_id, "😕 Что-то пошло не так. Попробуйте ещё раз.", reply_markup=main_menu_kb()
            )
            return

        await db.add_seen(user_id, card.track_id, card.label, mode, topic)
        await db.set_last_request(user_id, mode, topic)
        await _safe_delete(status)
        await send_card(bot, chat_id, card)


async def send_card(bot: Bot, chat_id: int, card: TrackCard, header: str = "") -> None:
    await bot.send_message(
        chat_id,
        format_card(card, header),
        reply_markup=track_kb(),
        link_preview_options=LinkPreviewOptions(
            is_disabled=False, url=card.url, prefer_large_media=True
        ),
    )


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


# ----------------------------------------------------------------- handlers

@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message) -> None:
    pending_input.pop(message.from_user.id, None)
    await message.answer(WELCOME, reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Команды:\n/start — главное меню\n/random — случайный трек\n"
        "/subscribe — ежедневная рассылка\n/playlists — плейлисты Spotify\n"
        "/settings — настройки (фильтр нецензурной лексики)\n"
        "/history — история треков (или просто напишите «история»)\n"
        "/connect — подключить Spotify\n/disconnect — отключить Spotify\n\n"
        "Пришлите ссылку на плейлист Spotify — я его проанализирую.\n"
        "Или просто напишите запрос текстом: «главные хиты 1985», «песня с "
        "мрачной историей», «музыка из Ведьмака»…"
    )


@router.message(Command("random"))
async def cmd_random(message: Message, bot: Bot) -> None:
    await deliver(bot, message.chat.id, message.from_user.id, "random", "")


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    await message.answer(await subscription_status(message.from_user.id), reply_markup=schedule_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery) -> None:
    pending_input.pop(cq.from_user.id, None)
    await cq.answer()
    await cq.message.answer(WELCOME, reply_markup=main_menu_kb())


@router.callback_query(F.data.startswith("mode:"))
async def cb_mode(cq: CallbackQuery, bot: Bot) -> None:
    mode = cq.data.split(":", 1)[1]
    await cq.answer()
    if mode in PRESETS:
        pending_input[cq.from_user.id] = mode
        await cq.message.answer(
            f"<b>{MODES[mode]}</b>\n{INPUT_HINTS[mode]}", reply_markup=submenu_kb(mode)
        )
    elif mode in MODES:
        await deliver(bot, cq.message.chat.id, cq.from_user.id, mode, "")


@router.callback_query(F.data.startswith("p:"))
async def cb_preset(cq: CallbackQuery, bot: Bot) -> None:
    _, mode, idx = cq.data.split(":")
    await cq.answer()
    try:
        topic = PRESETS[mode][int(idx)]
    except (KeyError, ValueError, IndexError):
        return
    if mode == "hits" and topic == "Хиты всех времён":
        topic = ""
    await deliver(bot, cq.message.chat.id, cq.from_user.id, mode, topic)


@router.callback_query(F.data == "more")
async def cb_more(cq: CallbackQuery, bot: Bot) -> None:
    await cq.answer()
    last = await db.get_last_request(cq.from_user.id)
    mode, topic = last if last else ("random", "")
    await deliver(bot, cq.message.chat.id, cq.from_user.id, mode, topic)


async def subscription_status(user_id: int) -> str:
    sub = await db.get_subscription(user_id)
    if sub and sub.active:
        return (
            "⏰ <b>Ежедневная рассылка включена</b>\n"
            f"Тема: {html.escape(topic_title(sub.mode, sub.topic))}\n"
            f"Расписание: {SCHEDULE_TEXT.get(sub.per_day, '')}\n\n"
            "Изменить частоту или отключить 👇\n"
            "<i>Чтобы сменить тему, нажмите «📌 Присылать такое ежедневно» под любым треком.</i>"
        )
    last = await db.get_last_request(user_id)
    theme = topic_title(*last) if last else MODES["random"]
    return (
        "⏰ <b>Ежедневная рассылка</b>\n"
        f"Буду присылать треки по теме: {html.escape(theme)}\n"
        "Сколько треков в день? 👇"
    )


@router.callback_query(F.data == "sub_menu")
async def cb_sub_menu(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(await subscription_status(cq.from_user.id), reply_markup=schedule_kb())


@router.callback_query(F.data == "sub_here")
async def cb_sub_here(cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    last = await db.get_last_request(user_id)
    mode, topic = last if last else ("random", "")
    sub = await db.get_subscription(user_id)
    per_day = sub.per_day if sub and sub.per_day in SCHEDULE_TEXT else 1
    await db.upsert_subscription(user_id, mode, topic, per_day)
    await cq.answer("📌 Подписка оформлена!")
    await cq.message.answer(
        "📌 <b>Готово!</b> Буду присылать такое ежедневно.\n"
        f"Тема: {html.escape(topic_title(mode, topic))}\n"
        f"Расписание: {SCHEDULE_TEXT[per_day]}\n\n"
        "Можно изменить частоту 👇",
        reply_markup=schedule_kb(),
    )


@router.callback_query(F.data.startswith("sub:"))
async def cb_sub_set(cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    per_day = int(cq.data.split(":", 1)[1])
    if per_day == 0:
        await db.set_subscription_active(user_id, False)
        await cq.answer("Рассылка отключена")
        await cq.message.answer("🚫 Ежедневная рассылка отключена.", reply_markup=main_menu_kb())
        return
    sub = await db.get_subscription(user_id)
    if sub:
        mode, topic = sub.mode, sub.topic
    else:
        last = await db.get_last_request(user_id)
        mode, topic = last if last else ("random", "")
    await db.upsert_subscription(user_id, mode, topic, per_day)
    await cq.answer("Сохранено ✅")
    await cq.message.answer(
        "✅ <b>Рассылка включена</b>\n"
        f"Тема: {html.escape(topic_title(mode, topic))}\n"
        f"Расписание: {SCHEDULE_TEXT[per_day]}",
        reply_markup=main_menu_kb(),
    )


# ------------------------------------------------------ Spotify playlists

def _plural_tracks(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} трек"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} трека"
    return f"{n} треков"


def _fmt_duration(ms: int) -> str:
    minutes = ms // 60000
    return f"{minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"{minutes} мин"


def playlists_menu_kb() -> InlineKeyboardMarkup:
    return kb([
        [("📂 Мои плейлисты (анализ)", "pl_list")],
        [("✨ Создать плейлист по описанию", "pl_new")],
        [("💾 Плейлист из показанных мне треков", "pl_seen")],
        [("🔌 Отключить Spotify", "pl_logout")],
        [("🔙 В главное меню", "menu")],
    ])


async def show_playlists_menu(bot: Bot, chat_id: int, user_id: int) -> None:
    if not playlists.enabled:
        await bot.send_message(
            chat_id,
            "🎶 Работа с плейлистами не настроена: владелец бота должен задать "
            "переменную <code>SPOTIFY_REDIRECT_URI</code> (см. README).\n\n"
            "Анализ публичного плейлиста всё равно можно попробовать — просто пришлите ссылку.",
        )
        return
    profile = await db.get_spotify_profile(user_id)
    if not profile:
        url = await playlists.auth_url(user_id)
        await bot.send_message(
            chat_id,
            "🎶 <b>Плейлисты Spotify</b>\n\n"
            "Подключите свой аккаунт Spotify, и я смогу:\n"
            "• собирать плейлисты по любому описанию прямо в ваш Spotify;\n"
            "• анализировать ваши плейлисты и предлагать, что в них добавить;\n"
            "• сохранить все показанные мной треки в один плейлист.\n\n"
            "Анализ публичного плейлиста по ссылке работает и без подключения.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Подключить Spotify", url=url)],
                [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu")],
            ]),
        )
        return
    await bot.send_message(
        chat_id,
        f"🎶 <b>Плейлисты Spotify</b> · аккаунт: {html.escape(profile[1])}\n\n"
        "Выберите действие или просто пришлите ссылку на любой плейлист для анализа 👇",
        reply_markup=playlists_menu_kb(),
    )


@router.message(Command("playlists"))
@router.message(Command("connect"))
async def cmd_playlists(message: Message, bot: Bot) -> None:
    await show_playlists_menu(bot, message.chat.id, message.from_user.id)


@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message) -> None:
    await db.delete_spotify_token(message.from_user.id)
    await message.answer("🔌 Spotify отключён. Подключить снова: /connect", reply_markup=main_menu_kb())


@router.callback_query(F.data == "pl_menu")
async def cb_pl_menu(cq: CallbackQuery, bot: Bot) -> None:
    await cq.answer()
    await show_playlists_menu(bot, cq.message.chat.id, cq.from_user.id)


@router.callback_query(F.data == "pl_logout")
async def cb_pl_logout(cq: CallbackQuery) -> None:
    await db.delete_spotify_token(cq.from_user.id)
    await cq.answer("Spotify отключён")
    await cq.message.answer("🔌 Spotify отключён. Подключить снова: /connect", reply_markup=main_menu_kb())


async def run_playlist_task(bot: Bot, chat_id: int, user_id: int, status_text: str, coro_factory):
    """Common wrapper: lock, status message, error reporting."""
    lock = user_locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        await bot.send_message(chat_id, "⏳ Я ещё работаю над прошлым запросом, секунду…")
        return None
    async with lock:
        status = await bot.send_message(chat_id, status_text)
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            return await coro_factory()
        except ServiceError as exc:
            await bot.send_message(chat_id, f"😕 {html.escape(str(exc))}")
        except Exception:
            log.exception("Playlist task failed")
            await bot.send_message(chat_id, "😕 Что-то пошло не так. Попробуйте ещё раз.")
        finally:
            await _safe_delete(status)
    return None


async def send_playlist_result(bot: Bot, chat_id: int, res) -> None:
    text = (
        f"✅ <b>Плейлист готов: {html.escape(res.name)}</b>\n"
        f"Добавлено треков: {res.added}"
        + (f" из {res.requested}" if res.requested != res.added else "")
        + f'\n🔗 <a href="{html.escape(res.url)}">Открыть в Spotify</a>'
    )
    if res.missing:
        text += "\n\n<i>Не нашлись в Spotify: " + html.escape("; ".join(res.missing[:8])) + "</i>"
    await bot.send_message(
        chat_id, text,
        reply_markup=kb([[("🎶 Плейлисты", "pl_menu"), ("🔙 В главное меню", "menu")]]),
        link_preview_options=LinkPreviewOptions(is_disabled=False, url=res.url),
    )


async def create_playlist(bot: Bot, chat_id: int, user_id: int, request: str) -> None:
    pending_input.pop(user_id, None)
    if not await playlists.is_connected(user_id):
        await show_playlists_menu(bot, chat_id, user_id)
        return
    res = await run_playlist_task(
        bot, chat_id, user_id, "🎛 Собираю плейлист… это может занять до минуты",
        lambda: playlists.create_from_prompt(user_id, request),
    )
    if res:
        await send_playlist_result(bot, chat_id, res)


@router.callback_query(F.data == "pl_new")
async def cb_pl_new(cq: CallbackQuery) -> None:
    await cq.answer()
    pending_input[cq.from_user.id] = "playlist"
    await cq.message.answer(
        "✨ Опишите плейлист, который нужно собрать. Например:\n"
        "• <i>бодрый инди-рок 2010-х для пробежки, 25 треков</i>\n"
        "• <i>уютный джаз для дождливого вечера</i>\n"
        "• <i>саундтрек к поездке по ночному Токио</i>",
        reply_markup=kb([[("🔙 Назад", "pl_menu")]]),
    )


@router.callback_query(F.data == "pl_seen")
async def cb_pl_seen(cq: CallbackQuery, bot: Bot) -> None:
    await cq.answer()
    user_id = cq.from_user.id
    res = await run_playlist_task(
        bot, cq.message.chat.id, user_id, "💾 Сохраняю ваши находки…",
        lambda: playlists.create_from_seen(user_id),
    )
    if res:
        await send_playlist_result(bot, cq.message.chat.id, res)


@router.callback_query(F.data == "pl_list")
async def cb_pl_list(cq: CallbackQuery, bot: Bot) -> None:
    await cq.answer()
    user_id = cq.from_user.id
    try:
        items = await playlists.list_playlists(user_id)
    except ServiceError as exc:
        await cq.message.answer(f"😕 {html.escape(str(exc))}")
        return
    if not items:
        await cq.message.answer("У вас пока нет плейлистов. Создадим? 👇", reply_markup=playlists_menu_kb())
        return
    rows = [[(f"🎵 {_cut(p.get('name') or 'Без названия', 40)}", f"pa:{p['id']}")] for p in items]
    rows.append([("🔙 Назад", "pl_menu")])
    await cq.message.answer("📂 Выберите плейлист для анализа:", reply_markup=kb(rows))


def format_analysis(an: Analysis) -> str:
    e = html.escape
    lines = [f'🔍 <b>Анализ плейлиста <a href="{e(an.url)}">{e(an.name)}</a></b>']
    if an.verdict:
        lines.append(f"<i>{e(an.verdict)}</i>")
    stats = [f"📊 {_plural_tracks(an.total)} · {_fmt_duration(an.duration_ms)}"]
    if an.top_artists:
        stats.append("👤 Топ артистов: " + ", ".join(f"{e(a)} ({n})" for a, n in an.top_artists))
    if an.decades:
        stats.append("📅 Эпохи: " + " · ".join(
            f"{e(d)} {round(n * 100 / an.total)}%" for d, n in an.decades))
    if an.explicit:
        stats.append(f"🔞 С ненормативной лексикой: {an.explicit} из {an.total}")
    if an.avg_popularity is not None:
        stats.append(f"🔥 Средняя популярность: {an.avg_popularity}/100")
    if an.mood:
        stats.append(f"🎭 Настроение: {e(an.mood)}")
    if an.genres:
        stats.append("🏷 Жанры: " + e(", ".join(an.genres)))
    lines.append("\n".join(stats))
    if an.text:
        lines.append(e(_cut(an.text, 1300)))
    if an.recs:
        recs = ["💡 <b>Что добавить:</b>"]
        for i, r in enumerate(an.recs, 1):
            why = f" — {e(_cut(r['why'], 140))}" if r["why"] else ""
            recs.append(f'{i}. <a href="{e(r["url"])}">{e(r["label"])}</a>{why}')
        lines.append("\n".join(recs))
    return _cut("\n\n".join(lines), 4000)


async def analyze_playlist(bot: Bot, chat_id: int, user_id: int, playlist_id: str) -> None:
    an = await run_playlist_task(
        bot, chat_id, user_id, "🔍 Слушаю ваш плейлист и анализирую…",
        lambda: playlists.analyze(user_id, playlist_id),
    )
    if not an:
        return
    last_analysis[user_id] = an
    rows = []
    if an.recs and an.owned:
        rows.append([("➕ Добавить рекомендации в этот плейлист", "pr_add")])
    if an.recs:
        rows.append([("💾 Сохранить рекомендации новым плейлистом", "pr_new")])
    rows.append([("🎶 Плейлисты", "pl_menu"), ("🔙 В главное меню", "menu")])
    await bot.send_message(
        chat_id, format_analysis(an), reply_markup=kb(rows),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data.startswith("pa:"))
async def cb_pl_analyze(cq: CallbackQuery, bot: Bot) -> None:
    await cq.answer()
    await analyze_playlist(bot, cq.message.chat.id, cq.from_user.id, cq.data.split(":", 1)[1])


@router.callback_query(F.data.in_({"pr_add", "pr_new"}))
async def cb_pl_recs(cq: CallbackQuery, bot: Bot) -> None:
    user_id = cq.from_user.id
    an = last_analysis.get(user_id)
    if not an or not an.recs:
        await cq.answer("Сначала проанализируйте плейлист", show_alert=True)
        return
    await cq.answer()
    if not await playlists.is_connected(user_id):
        await show_playlists_menu(bot, cq.message.chat.id, user_id)
        return
    ids = [r["id"] for r in an.recs]
    if cq.data == "pr_add":
        async def _add() -> bool:
            await playlists.add_tracks(user_id, an.playlist_id, ids)
            return True

        if not await run_playlist_task(bot, cq.message.chat.id, user_id, "➕ Добавляю треки…", _add):
            return
        await bot.send_message(
            cq.message.chat.id,
            f'✅ Добавил {len(ids)} треков в <a href="{html.escape(an.url)}">{html.escape(an.name)}</a>',
            reply_markup=kb([[("🎶 Плейлисты", "pl_menu"), ("🔙 В главное меню", "menu")]]),
        )
        last_analysis.pop(user_id, None)
    else:
        res = await run_playlist_task(
            bot, cq.message.chat.id, user_id, "💾 Создаю плейлист…",
            lambda: playlists.create_from_tracks(
                user_id, f"Дополнение к «{an.name}»"[:100],
                f"Рекомендации DJ-бота к плейлисту {an.name}", ids),
        )
        if res:
            await send_playlist_result(bot, cq.message.chat.id, res)


async def spotify_callback(request: web.Request) -> web.Response:
    """OAuth redirect target: /spotify/callback?code=...&state=..."""
    bot: Bot = request.app["bot"]
    page = (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Spotify DJ Bot</title></head><body style='font-family:sans-serif;"
        "text-align:center;padding:40px'><h2>{}</h2><p>{}</p></body></html>"
    )
    code, state = request.query.get("code"), request.query.get("state", "")
    if not code:
        return web.Response(
            text=page.format("Вход отменён", "Можно закрыть эту страницу и вернуться в Telegram."),
            content_type="text/html",
        )
    try:
        user_id, name = await playlists.finish_auth(state, code)
    except ServiceError as exc:
        return web.Response(text=page.format("Ошибка", html.escape(str(exc))), content_type="text/html", status=400)
    except Exception:
        log.exception("Spotify OAuth failed")
        return web.Response(
            text=page.format("Ошибка", "Не удалось подключить Spotify. Попробуйте /connect ещё раз."),
            content_type="text/html", status=500,
        )
    try:
        await bot.send_message(
            user_id, f"✅ Spotify подключён: <b>{html.escape(name)}</b>", reply_markup=playlists_menu_kb()
        )
    except Exception:
        log.warning("Could not notify user %s about Spotify login", user_id)
    return web.Response(
        text=page.format("✅ Spotify подключён", "Возвращайтесь в Telegram — бот уже ждёт."),
        content_type="text/html",
    )


# ------------------------------------------------------------------ history

HISTORY_PAGE = 10
HISTORY_WORDS = {"история", "/история", "моя история", "история треков", "🕘 история"}


def _fmt_seen_at(raw: str) -> str:
    """SQLite UTC timestamp -> local 'DD.MM HH:MM' (TIMEZONE_OFFSET)."""
    try:
        dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return dt.astimezone(TZ).strftime("%d.%m.%y %H:%M")


async def history_view(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_history(user_id)
    if total == 0:
        return (
            "🕘 <b>История пуста</b>\n\nЗдесь будут все треки, которые я вам присылал. "
            "Выберите режим в меню, чтобы начать 🎧",
            kb([[("🔙 В главное меню", "menu")]]),
        )
    pages = (total + HISTORY_PAGE - 1) // HISTORY_PAGE
    page = max(0, min(page, pages - 1))
    items = await db.get_history(user_id, HISTORY_PAGE, page * HISTORY_PAGE)

    e = html.escape
    lines = [f"🕘 <b>История треков</b> · {_plural_tracks(total)} · стр. {page + 1}/{pages}"]
    for n, it in enumerate(items, page * HISTORY_PAGE + 1):
        url = f"https://open.spotify.com/track/{it.track_id}"
        label = it.label or "Трек"
        meta = [topic_title(it.mode, it.topic)] if it.mode else []
        when = _fmt_seen_at(it.seen_at)
        if when:
            meta.append(when)
        line = f'<b>{n}.</b> <a href="{e(url)}">{e(_cut(label, 80))}</a>'
        if meta:
            line += f"\n      <i>{e(' · '.join(meta))}</i>"
        lines.append(line)

    nav = []
    if page > 0:
        nav.append(("◀️ Новее", f"hist:{page - 1}"))
    if page < pages - 1:
        nav.append(("Старше ▶️", f"hist:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([("💾 Сохранить в плейлист Spotify", "pl_seen")])
    rows.append([("🗑 Очистить историю", "hist_clear"), ("🔙 В меню", "menu")])
    return "\n\n".join(lines), kb(rows)


@router.message(Command("history"))
@router.message(F.text.func(lambda t: t.strip().lower() in HISTORY_WORDS))
async def cmd_history(message: Message) -> None:
    pending_input.pop(message.from_user.id, None)
    text, markup = await history_view(message.from_user.id, 0)
    await message.answer(text, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))


@router.callback_query(F.data.startswith("hist:"))
async def cb_history(cq: CallbackQuery) -> None:
    await cq.answer()
    try:
        page = int(cq.data.split(":", 1)[1])
    except ValueError:
        page = 0
    text, markup = await history_view(cq.from_user.id, page)
    no_preview = LinkPreviewOptions(is_disabled=True)
    if not (cq.message.text or "").startswith("🕘 История треков"):
        # Opened from a menu/track card: send a new message instead of replacing it.
        await cq.message.answer(text, reply_markup=markup, link_preview_options=no_preview)
        return
    try:
        await cq.message.edit_text(text, reply_markup=markup, link_preview_options=no_preview)
    except TelegramBadRequest:
        pass  # message not modified


@router.callback_query(F.data == "hist_clear")
async def cb_history_clear(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(
        "🗑 Удалить всю историю?\n\n<i>После очистки я забуду, какие треки уже присылал, "
        "и они могут попадаться снова.</i>",
        reply_markup=kb([[("Да, удалить", "hist_clear_yes"), ("Отмена", "hist:0")]]),
    )


@router.callback_query(F.data == "hist_clear_yes")
async def cb_history_clear_yes(cq: CallbackQuery) -> None:
    await db.clear_history(cq.from_user.id)
    await cq.answer("История очищена")
    try:
        await cq.message.edit_text("🗑 История очищена.", reply_markup=main_menu_kb())
    except TelegramBadRequest:
        await cq.message.answer("🗑 История очищена.", reply_markup=main_menu_kb())


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    raw = message.text.strip()
    playlist_id = extract_playlist_id(raw)
    if playlist_id:
        pending_input.pop(user_id, None)
        await analyze_playlist(bot, message.chat.id, user_id, playlist_id)
        return
    text = raw[:300]
    mode = pending_input.get(user_id)
    if mode == "playlist":
        await create_playlist(bot, message.chat.id, user_id, text)
        return
    if mode:
        await deliver(bot, message.chat.id, user_id, mode, text)
        return
    try:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        mode, topic = await service.route_intent(text)
    except ServiceError as exc:
        await message.answer(f"😕 {html.escape(str(exc))}")
        return
    if mode == "playlist":
        await create_playlist(bot, message.chat.id, user_id, topic)
        return
    await deliver(bot, message.chat.id, user_id, mode, topic)


# ----------------------------------------------------------------- settings

async def settings_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    on = await db.get_no_explicit(user_id)
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🚫 Без нецензурной лексики: <b>{'включено ✅' if on else 'выключено'}</b>\n"
        "<i>Когда включено, я не присылаю треки с пометкой Explicit — ни в подборках, "
        "ни в рассылке, ни в создаваемых плейлистах и рекомендациях.</i>"
    )
    markup = kb([
        [(f"{'☑️' if on else '⬜️'} Без нецензурной лексики", "toggle_explicit")],
        [("🔙 В главное меню", "menu")],
    ])
    return text, markup


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    text, markup = await settings_view(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "settings")
async def cb_settings(cq: CallbackQuery) -> None:
    await cq.answer()
    text, markup = await settings_view(cq.from_user.id)
    await cq.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "toggle_explicit")
async def cb_toggle_explicit(cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    new_value = not await db.get_no_explicit(user_id)
    await db.set_no_explicit(user_id, new_value)
    await cq.answer("Фильтр включён ✅" if new_value else "Фильтр выключен")
    text, markup = await settings_view(user_id)
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await cq.message.answer(text, reply_markup=markup)


# ------------------------------------------------------------ daily digest

async def send_digest(bot: Bot, min_per_day: int) -> None:
    subs = await db.get_active_subscriptions(min_per_day)
    log.info("Daily digest (per_day >= %s): %s subscribers", min_per_day, len(subs))
    for sub in subs:
        try:
            card = await asyncio.wait_for(
                service.pick(sub.user_id, sub.mode, sub.topic), timeout=PICK_TIMEOUT
            )
            for attempt in range(2):
                try:
                    await send_card(bot, sub.user_id, card, header="☀️ <b>Ваш трек дня</b>")
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
            await db.add_seen(sub.user_id, card.track_id, card.label, sub.mode, sub.topic)
        except TelegramForbiddenError:
            log.info("User %s blocked the bot — deactivating subscription", sub.user_id)
            await db.set_subscription_active(sub.user_id, False)
        except TelegramBadRequest as exc:
            if "chat not found" in str(exc).lower():
                await db.set_subscription_active(sub.user_id, False)
            log.warning("Digest to %s failed: %s", sub.user_id, exc)
        except ServiceError as exc:
            log.warning("Digest pick for %s failed: %s", sub.user_id, exc)
        except Exception:
            log.exception("Digest to %s failed", sub.user_id)
        # Free Gemini tier: ~15 requests per minute.
        await asyncio.sleep(1.5)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)
    opts = {"misfire_grace_time": 600, "coalesce": True, "max_instances": 1}
    scheduler.add_job(send_digest, "cron", hour=10, minute=0, args=[bot, 1], **opts)
    scheduler.add_job(send_digest, "cron", hour=15, minute=0, args=[bot, 3], **opts)
    scheduler.add_job(send_digest, "cron", hour=19, minute=30, args=[bot, 2], **opts)
    return scheduler


# ------------------------------------------------------------ health server

async def start_health_server(bot: Bot) -> web.AppRunner:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/spotify/callback", spotify_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Health server listening on :%s", port)
    return runner


# ------------------------------------------------------------- keep-alive

# Free Render services sleep after ~15 min without incoming HTTP traffic, and a
# sleeping bot does not receive Telegram messages. Pinging our own public URL
# every 10 minutes counts as traffic and keeps the bot awake (no UptimeRobot needed).
KEEPALIVE_URL = (
    os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
).strip().rstrip("/")


async def keep_alive() -> None:
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(f"{KEEPALIVE_URL}/health") as resp:
                log.debug("Keep-alive ping: %s", resp.status)
    except Exception as exc:
        log.warning("Keep-alive ping failed: %r", exc)


async def main() -> None:
    await db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    runner = await start_health_server(bot)
    scheduler = setup_scheduler(bot)
    if KEEPALIVE_URL:
        scheduler.add_job(keep_alive, "interval", minutes=10, max_instances=1, coalesce=True)
        log.info("Keep-alive enabled: %s/health every 10 min", KEEPALIVE_URL)
    scheduler.start()
    try:
        # Keep messages sent while the bot was restarting/asleep and answer them.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
