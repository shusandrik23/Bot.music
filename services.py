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
import random
import re
import unicodedata
from dataclasses import dataclass, field

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
    "refs": "🎬 Кино, игры и отсылки",
    "random": "🎲 Случайный трек",
}

TEMPERATURES = {
    "genre": 0.8,
    "era": 0.8,
    "hits": 0.7,
    "story": 1.0,
    "refs": 0.9,
    "random": 1.2,
}

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
    "refs": (
        "Пользователь ищет музыку, связанную с темой/отсылкой: «{topic}» "
        "(фильмы, сериалы, игры, книги, атмосфера).\n"
        "intro — 1-2 предложения о теме.\n"
        "candidates — 6 реальных треков, связанных с темой (саундтреки, песни "
        "из сцен, песни-отсылки, композиции нужной атмосферы). why — 1-2 "
        "предложения: где звучал трек или как он связан с темой."
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
- refs: музыка из фильмов, сериалов, игр, книг или про атмосферу/тему (topic = тема)
- random: случайный/неожиданный трек (topic = пусто)
- playlist: пользователь просит СОЗДАТЬ/собрать плейлист (topic = описание плейлиста целиком, с количеством треков, если указано)
Если запрос не подходит ни под один режим, но связан с музыкой или настроением — выбери refs.
Запрос пользователя: «{text}»
Ответ JSON: {{"mode": "...", "topic": "..."}}"""


CLEAN_HINT = (
    "ВАЖНО: пользователь включил фильтр — предлагай только треки БЕЗ ненормативной "
    "лексики (без пометки Explicit в Spotify)."
)


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
        model: str = "gemini-2.5-flash",
    ) -> None:
        self.model = model
        # Tried in order if the configured model is retired / not found.
        self._fallback_models = [
            m for m in ("gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest") if m != model
        ]
        self.gemini = genai.Client(api_key=gemini_api_key)
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=spotify_client_id, client_secret=spotify_client_secret
            ),
            requests_timeout=15,
            retries=3,
        )
        # Free Gemini tier is ~15 RPM: keep concurrent calls low.
        self._gemini_sem = asyncio.Semaphore(3)

    # ------------------------------------------------------------------ Gemini

    async def _ask_json(self, prompt: str, temperature: float) -> dict:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=temperature,
        )
        last_exc: Exception | None = None
        attempt = 0
        while attempt < 3:
            attempt += 1
            model = self.model
            try:
                async with self._gemini_sem:
                    resp = await self.gemini.aio.models.generate_content(
                        model=model, contents=prompt, config=config
                    )
                data = json.loads(resp.text or "{}")
                if isinstance(data, list):  # model sometimes wraps the object
                    data = data[0] if data else {}
                return data
            except genai_errors.APIError as exc:
                last_exc = exc
                code = getattr(exc, "code", None)
                text = str(exc)
                log.warning("Gemini API error (model %s, attempt %s): %s", model, attempt, text)
                if code == 404 or "is not found" in text or "not supported" in text:
                    # Model retired or unavailable: switch to the next one and retry.
                    if self._next_model():
                        attempt -= 1
                        continue
                    raise ServiceError(
                        "Модель Gemini недоступна. Укажите актуальную модель в переменной "
                        "GEMINI_MODEL (например, gemini-flash-latest)."
                    ) from exc
                if "API_KEY_INVALID" in text or "API key not valid" in text or code in (401, 403):
                    raise ServiceError(
                        "Gemini не принимает ключ. Проверьте переменную GEMINI_API_KEY "
                        "(без пробелов и кавычек) или создайте новый ключ на aistudio.google.com/apikey."
                    ) from exc
                if "location is not supported" in text.lower() or "FAILED_PRECONDITION" in text:
                    raise ServiceError(
                        "Gemini недоступен из региона сервера. Пересоздайте сервис на Render "
                        "в регионе Oregon (US West)."
                    ) from exc
                if code == 429:
                    raise ServiceError(
                        "Лимит бесплатного Gemini API исчерпан. Попробуйте через минуту 🙏"
                    ) from exc
                await asyncio.sleep(2)
            except (json.JSONDecodeError, ValueError) as exc:
                last_exc = exc
                log.warning("Gemini returned invalid JSON (attempt %s): %s", attempt, exc)
        raise ServiceError("Не получилось связаться с нейросетью, попробуйте ещё раз.") from last_exc

    def _next_model(self) -> bool:
        """Move to the next fallback model; False when none are left."""
        while self._fallback_models:
            candidate = self._fallback_models.pop(0)
            if candidate != self.model:
                log.warning("Switching Gemini model %s -> %s", self.model, candidate)
                self.model = candidate
                return True
        return False

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

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:  # spotipy raises various HTTP/network errors
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

        prompt = self._build_prompt(mode, topic, avoid, clean)
        data = await self._ask_json(prompt, TEMPERATURES[mode])
        intro = str(data.get("intro", "") or "").strip()
        candidates = self._parse_candidates(data)

        if mode == "hits":
            card = await self._pick_hit(candidates, data.get("queries") or [], seen, topic, clean)
        else:
            card = await self._pick_first_found(candidates, seen, clean)

        if card is None:
            raise ServiceError(
                "Не удалось найти подходящий новый трек в Spotify 😔 "
                "Попробуйте ещё раз или выберите другую тему."
            )
        card.mode, card.topic, card.intro = mode, topic, intro
        return card

    def _build_prompt(self, mode: str, topic: str, avoid: list[str], clean: bool = False) -> str:
        topic_hint = f"Пожелание пользователя: «{topic}»." if topic else ""
        body = MODE_PROMPTS[mode].format(topic=topic or "", topic_hint=topic_hint)
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
        self, candidates: list[Candidate], queries: list, seen: set[str], topic: str, clean: bool = False
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
