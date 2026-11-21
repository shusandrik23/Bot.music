"""Business logic: Gemini (ideas + texts) + Spotify search (real tracks).

Spotify restricted /recommendations in late 2024, so tracks are picked like this:
1. Gemini proposes several real songs (and, for hits, search queries) as JSON.
2. Each candidate is looked up with sp.search; tracks the user has already seen
   (SQLite) are skipped.
3. For "hits" the found tracks are sorted by `popularity` and one of the top 5
   is chosen at random.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import spotipy
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from spotipy.oauth2 import SpotifyClientCredentials

import database as db

log = logging.getLogger(__name__)

# Spotify caps `limit` for search at 10 for new (development-mode) apps.
SEARCH_LIMIT = 10

MODES: dict[str, str] = {
    "genre": "🎸 Жанры",
    "era": "🕰 Эпохи",
    "hits": "🔥 Главные хиты",
    "story": "📜 Трек с историей",
    "hist": "🏛 История в песнях",
    "film": "🎬 Кино и сериалы",
    "game": "🎮 Музыка из игр",
    "refs": "✨ Темы и атмосфера",
    "lang": "🌍 Музыка на языке",
    "random": "🎲 Случайный трек",
}

TEMPERATURES = {
    "genre": 0.8,
    "era": 0.8,
    "hits": 0.7,
    "story": 1.0,
    "refs": 0.9,
    "hist": 0.8,
    "film": 0.9,
    "game": 0.9,
    "lang": 0.9,
    "random": 1.2,
}

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря"]
MONTHS_RU_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
                 "сентябрь", "октябрь", "ноябрь", "декабрь"]

SYSTEM_PROMPT = (
    "Ты — эрудированный музыкальный журналист и диджей. Пишешь по-русски, живо и "
    "увлекательно, без воды и клише. Называешь ТОЛЬКО реально существующие песни "
    "реальных исполнителей, которые наверняка есть на Spotify. Названия треков и "
    "имена артистов пиши в оригинальном написании (как на Spotify), не переводи их. "
    "Всегда отвечай строго валидным JSON по заданной схеме."
)

CANDIDATES_SCHEMA = (
    'Формат ответа (JSON): {"intro": "...", "candidates": [{"artist": "...", '
    '"track": "...", "year": 1999, "why": "..."}]}'
)

MODE_PROMPTS: dict[str, str] = {
    "genre": (
        "Пользователь знакомится с жанром «{topic}».\n"
        "intro — краткая увлекательная справка о жанре (2-3 предложения): откуда он, "
        "чем звучит, чем цепляет.\n"
        "candidates — 6 эталонных треков этого жанра, разных исполнителей. "
        "why — 1-2 предложения, почему этот трек — эталон жанра."
    ),
    "era": (
        "Пользователь исследует музыкальную эпоху: «{topic}».\n"
        "intro — 2-3 предложения о культурном контексте эпохи (что происходило, "
        "какие настроения, технологии, субкультуры).\n"
        "candidates — 6 треков, которые являются знаковыми культурными маркерами "
        "этой эпохи, разных исполнителей и стилей. why — 1-2 предложения, почему "
        "именно эта песня стала символом времени."
    ),
    "hits": (
        "Пользователь хочет услышать главные хиты. Тема: «{topic}».\n"
        "intro — 1-2 предложения про эту тему/период и её главные хиты.\n"
        "candidates — 10 самых популярных и узнаваемых хитов по этой теме "
        "(если тема пустая — величайшие мировые хиты всех времён, разных эпох). "
        "why — 1 предложение о треке.\n"
        'Дополнительно добавь поле "queries" — список из 1-2 поисковых строк для '
        "Spotify Search API, которые найдут популярные треки по теме (можно "
        "использовать фильтры year:1985 или year:1980-1989 и genre:rock), "
        "например [\"year:1985\", \"genre:synth-pop year:1980-1989\"]."
    ),
    "story": (
        "Вспомни реально существующую песню с необычной историей создания, "
        "скандалом, мистикой или скрытым смыслом, о котором мало кто знает. "
        "{topic_hint}\n"
        "intro — пустая строка.\n"
        "candidates — 3 разные песни. why — сама история (3-5 предложений), "
        "только проверенные факты, без выдумок."
    ),
    "hist": (
        "Пользователь хочет музыку, связанную с историческим событием: {subject}.\n"
        "Подбери реальные песни, которые написаны об этом событии, стали его гимном или "
        "символом, звучали в те дни или прямо на нём, либо отражают дух того времени.\n"
        "intro — 2-3 предложения: что это было за событие и какую роль в нём сыграла музыка.\n"
        "candidates — 6 треков. why — 1-2 предложения о конкретной связи песни с событием "
        "(только проверенные факты, без выдумок)."
    ),
    "film": (
        "Пользователь хочет музыку из кино или сериалов: {subject}.\n"
        "Подбери реальные треки из саундтреков: песни из запоминающихся сцен, главные темы, "
        "композиции, которые стали культовыми благодаря фильму/сериалу.\n"
        "intro — 1-2 предложения о фильме/сериале/режиссёре и роли музыки в нём.\n"
        "candidates — 6 треков, которые есть на Spotify. why — 1-2 предложения: в каком "
        "фильме и в какой сцене звучит трек (без спойлеров концовки)."
    ),
    "game": (
        "Пользователь хочет музыку из видеоигр: {subject}.\n"
        "Подбери реальные треки: саундтреки игр, лицензированные песни из игр (радио в GTA, "
        "плейлисты гонок и т.п.), главные темы и композиции, ставшие культовыми.\n"
        "intro — 1-2 предложения об игре/серии/жанре и её музыке.\n"
        "candidates — 6 треков, которые есть на Spotify. why — 1-2 предложения: в какой "
        "игре и в какой момент звучит трек."
    ),
    "refs": (
        "Пользователь ищет музыку, связанную с темой/отсылкой: «{topic}» "
        "(фильмы, сериалы, игры, книги, атмосфера).\n"
        "intro — 1-2 предложения о теме.\n"
        "candidates — 6 реальных треков, связанных с темой (саундтреки, песни "
        "из сцен, песни-отсылки, композиции нужной атмосферы). why — 1-2 "
        "предложения: где звучал трек или как он связан с темой."
    ),
    "lang": (
        "Пользователь хочет музыку на определённом языке: «{topic}».\n"
        "Песни должны быть СПЕТЫ на этом языке (если в запросе есть жанр или настроение — "
        "учти и их). Разные исполнители и эпохи: и классика, и современные артисты.\n"
        "intro — 1-2 предложения о музыкальной сцене этого языка.\n"
        "candidates — 6 реальных треков на этом языке. why — 1-2 предложения: кто "
        "исполнитель и чем цепляет песня (можно кратко, о чём она)."
    ),
    "random": (
        "Музыкальная рулетка! Удиви пользователя неожиданной находкой со всего "
        "мира: японский сити-поп, эфиопский джаз, исландский пост-рок, "
        "турецкий психоделический рок, бразильская тропикалия, малийский "
        "блюз пустыни, советский диско, корейский инди и т.д. {topic_hint}\n"
        "intro — пустая строка.\n"
        "candidates — 5 треков из РАЗНЫХ стран и жанров. why — интригующая "
        "подводка (2-3 предложения), после которой хочется нажать play."
    ),
}

ROUTER_PROMPT = """Определи, какую музыку хочет пользователь. Режимы:
- genre: знакомство с конкретным жанром (topic = название жанра)
- era: десятилетие или эпоха (topic = например "1980-е" или "эпоха диско")
- hits: самые популярные/главные хиты (topic = тема, год или пусто)
- story: песня с интересной историей создания или скрытым смыслом (topic = уточнение или пусто)
- hist: музыка, связанная с историческим событием или периодом истории (topic = событие)
- film: музыка из фильмов или сериалов, саундтреки, режиссёры (topic = фильм/сериал/режиссёр)
- game: музыка из видеоигр (topic = игра, серия или жанр игр)
- refs: музыка про тему или атмосферу, книги и прочие отсылки (topic = тема)
- lang: музыка на определённом языке (topic = язык, можно с жанром, например "французский рэп")
- random: случайный/неожиданный трек (topic = пусто)
- playlist: пользователь просит СОЗДАТЬ/собрать плейлист (topic = описание плейлиста целиком, с количеством треков, если указано)
Если запрос не подходит ни под один режим, но связан с музыкой или настроением — выбери refs.
Запрос пользователя: «{text}»
Ответ JSON: {{"mode": "...", "topic": "..."}}"""


def _now() -> datetime:
    offset = float(os.environ.get("TIMEZONE_OFFSET", "3"))
    return datetime.now(timezone(timedelta(hours=offset)))


def _today_hint() -> str:
    d = _now()
    return f"Сегодня {d.day} {MONTHS_RU[d.month - 1]} {d.year} года."


def _hits_period(topic: str) -> str | None:
    """'month' / 'year' when the user asks for the current month's or year's hits."""
    t = (topic or "").lower()
    now = _now()
    month_names = (MONTHS_RU[now.month - 1], MONTHS_RU_NOM[now.month - 1][:-1] or MONTHS_RU_NOM[now.month - 1])
    if "месяц" in t or any(m in t for m in month_names):
        return "month"
    year = str(now.year)
    if year in t or ("год" in t and any(w in t for w in ("этот", "этого", "текущ", "нынеш"))):
        return "year"
    return None


def _fresh_hits_prompt(period: str, topic: str) -> str:
    d = _now()
    when = (
        f"за {MONTHS_RU_NOM[d.month - 1]} {d.year} года (этот месяц)"
        if period == "month"
        else f"за {d.year} год (с начала года до сегодня)"
    )
    extra = ""
    t = (topic or "").lower()
    if t and not any(w in t for w in ("хиты этого", "хиты за")):
        extra = f" Учти уточнение пользователя: «{topic}»."
    return (
        f"Пользователь хочет самые популярные треки {when}.{extra}\n"
        "Используй поиск Google: найди актуальные чарты (Spotify Top 50 Global, Billboard "
        "Hot 100, Apple Music Top 100 и т.п.) именно за этот период и опирайся на них, а не "
        "на старые знания.\n"
        "intro — 1-2 предложения: что сейчас на вершине чартов и почему.\n"
        "candidates — 10 самых популярных треков этого периода, по убыванию популярности. "
        "why — 1 предложение о треке (например, позиция в чарте или чем он выстрелил).\n"
        "Ответь ТОЛЬКО JSON-объектом, без пояснений вокруг."
    )


CLEAN_HINT = (
    "ВАЖНО: пользователь включил фильтр — предлагай только треки БЕЗ ненормативной "
    "лексики (без пометки Explicit в Spotify)."
)


GEMINI_CALL_TIMEOUT = 25.0  # seconds per request
GEMINI_TOTAL_BUDGET = 75.0  # seconds for all models/attempts of one question

_EXCLUDED_MODEL_RE = re.compile(
    r"image|tts|audio|live|embed|vision|exp|preview|thinking|computer|robotics|native|8b|001$"
)


def _parse_json(text: str | None):
    """Parse model output as JSON, tolerating ```json fences and extra text around it."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty response")
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(raw[start : end + 1])
    if isinstance(data, list):  # model sometimes wraps the object
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    return data


class ServiceError(Exception):
    """User-facing error (message is shown in the chat)."""


@dataclass
class Candidate:
    artist: str
    track: str
    year: str = ""
    why: str = ""


@dataclass
class TrackCard:
    track_id: str
    name: str
    artists: str
    year: str
    url: str
    mode: str
    topic: str
    intro: str = ""
    why: str = ""
    popularity: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.artists} — {self.name}"


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\(.*?\)|\[.*?\]|feat\..*| - .*remaster.*", "", text)
    return re.sub(r"[^\w]+", " ", text).strip()


def _clean_query_part(text: str) -> str:
    return re.sub(r"[\"':]", " ", text or "").strip()


class MusicService:
    def __init__(
        self,
        gemini_api_key: str,
        spotify_client_id: str,
        spotify_client_secret: str,
        model: str = "gemini-3.8-flash",
    ) -> None:
        self.model = model
        self._chain: list[str] | None = None  # discovered lazily from the API
        self._dead_models: set[str] = set()  # 404: retired / not available to this key
        self._cooldown: dict[str, float] = {}  # model -> monotonic time it may be retried
        self._no_thinking_cfg: set[str] = set()  # models that reject thinking_config
        self._spotify_blocked_until = 0.0  # Spotify asked us to slow down (HTTP 429)
        self.gemini = genai.Client(api_key=gemini_api_key)
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=spotify_client_id, client_secret=spotify_client_secret
            ),
            requests_timeout=10,
            retries=2,
            status_retries=0,  # don't sleep for Spotify's Retry-After (can be hours)
        )
        # Free Gemini tier is ~15 RPM: keep concurrent calls low.
        self._gemini_sem = asyncio.Semaphore(3)

    # ------------------------------------------------------------------ Gemini

    async def _model_chain(self) -> list[str]:
        """Configured model first, then flash models this API key can actually use."""
        if self._chain is None:
            chain = [self.model]
            try:
                found = []
                pager = await self.gemini.aio.models.list(config={"page_size": 100})
                async for m in pager:
                    name = (m.name or "").removeprefix("models/")
                    actions = m.supported_actions or []
                    if (
                        "flash" in name
                        and (not actions or "generateContent" in actions)
                        and not _EXCLUDED_MODEL_RE.search(name)
                    ):
                        found.append(name)

                def rank(name: str):
                    ver = re.search(r"(\d+(?:\.\d+)?)", name)
                    return ("lite" in name, -(float(ver.group(1)) if ver else 0.0), "latest" in name, name)

                for name in sorted(set(found), key=rank):
                    if name not in chain:
                        chain.append(name)
            except Exception as exc:
                log.warning("Could not list Gemini models: %r", exc)
            self._chain = chain
            log.info("Gemini model chain: %s", ", ".join(chain))
        return self._chain

    def _gen_config(
        self, model: str, temperature: float, search: bool = False
    ) -> types.GenerateContentConfig:
        thinking = None
        if model not in self._no_thinking_cfg:
            # Long "thinking" makes answers take tens of seconds; music tips don't need it.
            if "gemini-3" in model:
                thinking = types.ThinkingConfig(thinking_level="low")
            elif "gemini-2.5-flash" in model:
                thinking = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            # Google Search grounding can't be combined with JSON mode on every model:
            # with search on we ask for JSON in the prompt and parse it leniently.
            response_mime_type=None if search else "application/json",
            tools=[types.Tool(google_search=types.GoogleSearch())] if search else None,
            temperature=temperature,
            thinking_config=thinking,
            http_options=types.HttpOptions(timeout=int(GEMINI_CALL_TIMEOUT * 1000)),
        )

    async def _ask_json(self, prompt: str, temperature: float, search: bool = False) -> dict:
        """Ask Gemini for JSON. search=True lets the model use Google Search (fresh charts)."""
        deadline = time.monotonic() + GEMINI_TOTAL_BUDGET
        chain = [m for m in await self._model_chain() if m not in self._dead_models]
        now = time.monotonic()
        ready = [m for m in chain if self._cooldown.get(m, 0) <= now]
        # Models on cooldown (recently overloaded) go last, not away.
        candidates = (ready + [m for m in chain if m not in ready])[:4]
        if not candidates:
            raise ServiceError(
                "Нет доступных моделей Gemini для этого ключа. Укажите модель в переменной "
                "GEMINI_MODEL или создайте новый ключ на aistudio.google.com/apikey."
            )

        reasons: list[str] = []
        last_exc: Exception | None = None
        for model in candidates:
            for attempt in (1, 2):
                remaining = deadline - time.monotonic()
                if remaining < 5:
                    reasons.append("общее время ожидания истекло")
                    break
                started = time.monotonic()
                try:
                    async with self._gemini_sem:
                        resp = await asyncio.wait_for(
                            self.gemini.aio.models.generate_content(
                                model=model,
                                contents=prompt,
                                config=self._gen_config(model, temperature, search),
                            ),
                            timeout=min(GEMINI_CALL_TIMEOUT, remaining),
                        )
                    log.info("Gemini %s answered in %.1fs", model, time.monotonic() - started)
                    data = _parse_json(resp.text)
                    self._cooldown.pop(model, None)
                    return data
                except genai_errors.APIError as exc:
                    last_exc = exc
                    code = getattr(exc, "code", None)
                    text = str(exc)
                    message = getattr(exc, "message", "") or text
                    log.warning("Gemini API error (model %s, attempt %s): %s", model, attempt, text)
                    if "API_KEY_INVALID" in text or "API key not valid" in text or code in (401, 403):
                        raise ServiceError(
                            "Gemini не принимает ключ. Проверьте переменную GEMINI_API_KEY "
                            "(без пробелов и кавычек) или создайте новый ключ на aistudio.google.com/apikey."
                        ) from exc
                    if "location is not supported" in text.lower():
                        raise ServiceError(
                            "Gemini недоступен из региона сервера. Пересоздайте сервис на Render "
                            "в регионе Oregon (US West)."
                        ) from exc
                    if search and code in (400, 429):
                        # Grounding unsupported or its quota is used up: answer from knowledge.
                        log.info("Google Search grounding unavailable (%s), retrying without it", code)
                        search = False
                        continue
                    if code == 400 and "think" in text.lower() and model not in self._no_thinking_cfg:
                        # This model does not accept our thinking settings: retry without them.
                        self._no_thinking_cfg.add(model)
                        continue
                    if code == 404:
                        self._dead_models.add(model)  # retired / unknown: never try again
                        reasons.append(f"{model}: не найдена")
                        break
                    if code == 429:
                        # Free-tier quotas are per model: try the next one.
                        self._cooldown[model] = time.monotonic() + 60
                        reasons.append(f"{model}: лимит запросов")
                        break
                    if code == 504 or "DEADLINE_EXCEEDED" in text:
                        # The model is too slow right now: don't wait for it twice.
                        self._cooldown[model] = time.monotonic() + 120
                        reasons.append(f"{model}: не успела ответить")
                        break
                    if code and code >= 500:
                        if attempt == 1:
                            await asyncio.sleep(1.5)
                            continue
                        self._cooldown[model] = time.monotonic() + 60
                        reasons.append(f"{model}: перегружена")
                        break
                    reasons.append(f"{model}: {code} {message[:120]}")
                    break
                except asyncio.TimeoutError as exc:
                    last_exc = exc
                    self._cooldown[model] = time.monotonic() + 120
                    log.warning("Gemini timeout (model %s, attempt %s)", model, attempt)
                    reasons.append(f"{model}: не ответила вовремя")
                    break
                except (json.JSONDecodeError, ValueError) as exc:
                    last_exc = exc
                    log.warning("Gemini returned invalid JSON (model %s): %s", model, exc)
                    if attempt == 2:
                        reasons.append(f"{model}: ответ не в формате JSON")
                except Exception as exc:  # network errors etc.
                    last_exc = exc
                    log.warning("Gemini call failed (model %s): %r", model, exc)
                    reasons.append(f"{model}: {type(exc).__name__}")
                    break

        if reasons and all("лимит" in r for r in reasons if "время" not in r):
            raise ServiceError("Лимит бесплатного Gemini API исчерпан. Попробуйте через минуту 🙏") from last_exc
        raise ServiceError(
            "Не получилось связаться с нейросетью, попробуйте ещё раз через минуту."
            + ("\nПричина: " + "; ".join(reasons)[:300] if reasons else "")
        ) from last_exc

    async def route_intent(self, text: str) -> tuple[str, str]:
        data = await self._ask_json(ROUTER_PROMPT.format(text=text[:500]), temperature=0.0)
        mode = str(data.get("mode", "refs")).strip().lower()
        topic = str(data.get("topic", "") or "").strip()
        if mode == "playlist":
            return mode, topic or text
        if mode not in MODES:
            mode, topic = "refs", text
        if mode == "random":
            topic = ""
        return mode, topic

    # ----------------------------------------------------------------- Spotify

    async def _search(self, query: str, limit: int = SEARCH_LIMIT, offset: int = 0) -> list[dict]:
        def _do() -> list[dict]:
            res = self.sp.search(q=query, type="track", limit=limit, offset=offset)
            return (res or {}).get("tracks", {}).get("items", []) or []

        if time.monotonic() < self._spotify_blocked_until:
            return []
        try:
            return await asyncio.to_thread(_do)
        except spotipy.SpotifyException as exc:
            log.warning("Spotify search failed for %r: %s", query, exc)
            if exc.http_status == 429 or "429" in str(exc):
                retry_after = 60
                try:
                    retry_after = int((exc.headers or {}).get("Retry-After", 60))
                except (TypeError, ValueError):
                    pass
                self._spotify_blocked_until = time.monotonic() + min(max(retry_after, 30), 3600)
            return []
        except Exception as exc:  # network errors etc.
            log.warning("Spotify search failed for %r: %s", query, exc)
            return []

    @staticmethod
    def _artist_matches(item: dict, artist: str) -> bool:
        want = _norm(artist)
        if not want:
            return True
        for a in item.get("artists", []):
            got = _norm(a.get("name", ""))
            if got and (want in got or got in want):
                return True
        return False

    @staticmethod
    def _track_matches(item: dict, track: str) -> bool:
        want, got = _norm(track), _norm(item.get("name", ""))
        return bool(want and got and (want in got or got in want))

    async def find_track(self, cand: Candidate, seen: set[str], clean: bool = False) -> dict | None:
        """Exact field search first, then a simplified free-text fallback.

        clean=True skips tracks marked explicit by Spotify (profanity filter).
        """
        artist, track = _clean_query_part(cand.artist), _clean_query_part(cand.track)
        if not track:
            return None

        items = await self._search(f"track:{track} artist:{artist}", limit=5)
        for it in items:
            if clean and it.get("explicit"):
                continue
            if it.get("id") not in seen and self._artist_matches(it, artist):
                return it

        items = await self._search(f"{artist} {track}", limit=5)
        for it in items:
            if it.get("id") in seen or (clean and it.get("explicit")):
                continue
            if self._artist_matches(it, artist) or self._track_matches(it, track):
                return it
        return None

    # ------------------------------------------------------------------ Picks

    async def pick(self, user_id: int, mode: str, topic: str = "") -> TrackCard:
        if mode not in MODES:
            mode = "random"
        seen = await db.get_seen_ids(user_id)
        clean = await db.get_no_explicit(user_id)
        avoid = await db.recent_labels(user_id)

        period = _hits_period(topic) if mode == "hits" else None
        prompt = self._build_prompt(mode, topic, avoid, clean, period)
        data = await self._ask_json(prompt, TEMPERATURES[mode], search=period is not None)
        intro = str(data.get("intro", "") or "").strip()
        candidates = self._parse_candidates(data)

        if mode == "hits":
            queries = data.get("queries") or []
            year_only = None
            if period:
                # Fresh hits: rely on the charts Gemini found; for "this year" also take
                # this year's releases from Spotify search.
                queries = [f"year:{_now().year}"] if period == "year" else []
                year_only = _now().year if period == "year" else None
            card = await self._pick_hit(candidates, queries, seen, topic, clean, year_only)
        else:
            card = await self._pick_first_found(candidates, seen, clean)

        if card is None and time.monotonic() < self._spotify_blocked_until:
            minutes = max(1, round((self._spotify_blocked_until - time.monotonic()) / 60))
            raise ServiceError(
                f"Spotify временно ограничил запросы бота. Попробуйте через ~{minutes} мин."
            )
        if card is None:
            raise ServiceError(
                "Не удалось найти подходящий новый трек в Spotify 😔 "
                "Попробуйте ещё раз или выберите другую тему."
            )
        card.mode, card.topic, card.intro = mode, topic, intro
        return card

    def _build_prompt(
        self, mode: str, topic: str, avoid: list[str], clean: bool = False, period: str | None = None
    ) -> str:
        topic_hint = f"Пожелание пользователя: «{topic}»." if topic else ""
        if period:
            body = _fresh_hits_prompt(period, topic)
        else:
            subject = (
                f"«{topic}»" if topic
                else "на твой выбор — возьми что-нибудь интересное и не самое очевидное"
            )
            body = MODE_PROMPTS[mode].format(topic=topic or "", topic_hint=topic_hint, subject=subject)
        body = f"{_today_hint()}\n{body}"
        if avoid:
            body += (
                "\nНЕ предлагай эти треки — пользователь их уже слышал: "
                + "; ".join(avoid)
            )
        if clean:
            body += f"\n{CLEAN_HINT}"
        return f"{body}\n\n{CANDIDATES_SCHEMA}"

    @staticmethod
    def _parse_candidates(data: dict) -> list[Candidate]:
        result = []
        for c in data.get("candidates") or []:
            if not isinstance(c, dict):
                continue
            artist = str(c.get("artist", "") or "").strip()
            track = str(c.get("track", "") or "").strip()
            if artist and track:
                result.append(
                    Candidate(
                        artist=artist,
                        track=track,
                        year=str(c.get("year", "") or ""),
                        why=str(c.get("why", "") or "").strip(),
                    )
                )
        return result

    async def _pick_first_found(
        self, candidates: list[Candidate], seen: set[str], clean: bool = False
    ) -> TrackCard | None:
        # Look up all candidates concurrently, keep the LLM's order of preference.
        found = await asyncio.gather(*(self.find_track(c, seen, clean) for c in candidates))
        for cand, item in zip(candidates, found):
            if item:
                return self._to_card(item, why=cand.why)
        return None

    async def _pick_hit(
        self,
        candidates: list[Candidate],
        queries: list,
        seen: set[str],
        topic: str,
        clean: bool = False,
        year_only: int | None = None,
    ) -> TrackCard | None:
        tasks = [self.find_track(c, seen, clean) for c in candidates]
        query_list = [str(q) for q in queries if isinstance(q, str) and q.strip()][:2]
        if not query_list and not candidates:
            query_list = [topic or "year:2020-2026"]
        for q in query_list:
            tasks.append(self._search(q, offset=0))
            tasks.append(self._search(q, offset=SEARCH_LIMIT))

        results = await asyncio.gather(*tasks)
        why_by_id: dict[str, str] = {}
        pool: dict[str, dict] = {}
        for idx, res in enumerate(results):
            items = [res] if isinstance(res, dict) else (res or [])
            for it in items:
                if not it or not it.get("id") or it["id"] in seen or (clean and it.get("explicit")):
                    continue
                if (
                    year_only
                    and idx >= len(candidates)  # search-query results (not chart candidates)
                    and not str((it.get("album") or {}).get("release_date", "")).startswith(str(year_only))
                ):
                    continue
                pool.setdefault(it["id"], it)
                if idx < len(candidates):
                    why_by_id[it["id"]] = candidates[idx].why
        if not pool:
            return None

        tracks = list(pool.values())
        if any(isinstance(t.get("popularity"), int) for t in tracks):
            tracks.sort(key=lambda t: t.get("popularity") or 0, reverse=True)
        else:
            # Popularity is not returned for some apps: trust the LLM's ranking.
            ranked = [pool[i] for i in why_by_id if i in pool]
            tracks = ranked + [t for t in tracks if t["id"] not in why_by_id]
        chosen = random.choice(tracks[:5])
        return self._to_card(chosen, why=why_by_id.get(chosen["id"], ""))

    @staticmethod
    def _to_card(item: dict, why: str = "") -> TrackCard:
        album = item.get("album") or {}
        release = str(album.get("release_date", "") or "")
        return TrackCard(
            track_id=item["id"],
            name=item.get("name", "?"),
            artists=", ".join(a.get("name", "") for a in item.get("artists", [])) or "?",
            year=release[:4],
            url=(item.get("external_urls") or {}).get("spotify")
            or f"https://open.spotify.com/track/{item['id']}",
            mode="",
            topic="",
            why=why,
            popularity=item.get("popularity") if isinstance(item.get("popularity"), int) else None,
        )
