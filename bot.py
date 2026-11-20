"""Spotify DJ bot — entry point.

aiogram 3.x handlers + APScheduler daily digest + tiny aiohttp health server
(GET / -> 200 OK) so the bot can live on free Render/Koyeb web services.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import timedelta, timezone

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
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

import database as db  # noqa: E402  (DB_PATH is read from env after load_dotenv)
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
    model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
)

router = Router()

# Submenu presets: (button label, topic sent to the LLM)
GENRES = ["Джаз", "Синтвейв", "Пост-панк", "Трип-хоп", "Неоклассика",
          "Олдскул Хип-хоп", "Босса-нова", "Драм-н-бэйс"]
ERAS = ["1960-е", "1970-е", "1980-е", "1990-е", "2000-е", "2010-е"]
HITS = ["Хиты всех времён", "Хиты 80-х", "Хиты 90-х", "Хиты 2000-х", "Хиты этого десятилетия"]
REFS = ["Фильмы Тарантино", "Связанные с космосом", "Саундтреки видеоигр",
        "Аниме", "Сериалы HBO", "Атмосфера ночного города"]
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

# Modes waiting for free-text input after a submenu was opened: user_id -> mode
pending_input: dict[int, str] = {}
# One request at a time per user (protects from double clicks)
user_locks: dict[int, asyncio.Lock] = {}


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
        [("⏰ Ежедневная рассылка (1-3 в день)", "sub_menu")],
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
    "<i>«что-нибудь из фильмов Тарантино»</i> или <i>«японский сити-поп»</i>."
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
            card = await service.pick(user_id, mode, topic)
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

        await db.add_seen(user_id, card.track_id, card.label)
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
        "/subscribe — ежедневная рассылка\n\n"
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


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    text = message.text.strip()[:300]
    mode = pending_input.get(user_id)
    if mode:
        await deliver(bot, message.chat.id, user_id, mode, text)
        return
    try:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        mode, topic = await service.route_intent(text)
    except ServiceError as exc:
        await message.answer(f"😕 {html.escape(str(exc))}")
        return
    await deliver(bot, message.chat.id, user_id, mode, topic)


# ------------------------------------------------------------ daily digest

async def send_digest(bot: Bot, min_per_day: int) -> None:
    subs = await db.get_active_subscriptions(min_per_day)
    log.info("Daily digest (per_day >= %s): %s subscribers", min_per_day, len(subs))
    for sub in subs:
        try:
            card = await service.pick(sub.user_id, sub.mode, sub.topic)
            for attempt in range(2):
                try:
                    await send_card(bot, sub.user_id, card, header="☀️ <b>Ваш трек дня</b>")
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
            await db.add_seen(sub.user_id, card.track_id, card.label)
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

async def start_health_server() -> web.AppRunner:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Health server listening on :%s", port)
    return runner


async def main() -> None:
    await db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    runner = await start_health_server()
    scheduler = setup_scheduler(bot)
    scheduler.start()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
