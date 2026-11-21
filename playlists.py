"""Spotify user features: OAuth login, playlist creation and playlist analysis.

Creating playlists and reading private ones needs the user's own token
(Authorization Code flow), unlike track search, which works with app-only
Client Credentials. Tokens are stored per Telegram user in SQLite and refreshed
by spotipy automatically.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

import spotipy
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyOAuth

import database as db
from services import CLEAN_HINT, Candidate, MusicService, ServiceError

log = logging.getLogger(__name__)

SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public "
    "user-library-read"  # saved podcasts (shows)
)

PLAYLIST_URL_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-\w+/)?playlist/|spotify:playlist:)([A-Za-z0-9]{22})")

CREATE_PROMPT = """Собери плейлист по запросу пользователя: «{request}».
Если пользователь указал количество треков — используй его (максимум 50), иначе 20.
Подбирай разнообразно: разные исполнители, без повторов, хорошая драматургия (начало, развитие, финал).
name — короткое цепляющее название плейлиста (до 60 символов), можно с эмодзи.
description — описание в 1-2 предложения (до 250 символов, без переносов строк).
candidates — список треков по порядку.
Формат ответа (JSON): {{"name": "...", "description": "...", "candidates": [{{"artist": "...", "track": "..."}}]}}"""

ANALYZE_PROMPT = """Проанализируй музыкальный плейлист «{name}».
Статистика: {stats}
Треки (артист — название (год)):
{tracks}

Ответ JSON:
{{"verdict": "одна яркая фраза-вердикт",
  "mood": "настроение в 3-6 словах",
  "genres": ["3-6 основных жанров"],
  "analysis": "4-6 предложений: характер плейлиста, звучание, эпохи, что объединяет треки, портрет слушателя",
  "recommendations": [{{"artist": "...", "track": "...", "why": "коротко, почему подойдёт"}}]}}
В recommendations дай 8 реальных треков, которых НЕТ в плейлисте, но которые идеально его дополнят."""


class DBCacheHandler(CacheHandler):
    """spotipy token cache backed by the bot's SQLite (one row per Telegram user)."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    def get_cached_token(self):
        return db.get_token_sync(self.user_id)

    def save_token_to_cache(self, token_info) -> None:
        db.save_token_sync(self.user_id, token_info)


@dataclass
class PlaylistResult:
    name: str
    url: str
    added: int
    requested: int
    missing: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    playlist_id: str
    name: str
    url: str
    owned: bool
    total: int
    duration_ms: int
    top_artists: list[tuple[str, int]]
    decades: list[tuple[str, int]]
    avg_popularity: int | None
    explicit: int = 0
    verdict: str = ""
    mood: str = ""
    genres: list[str] = field(default_factory=list)
    text: str = ""
    recs: list[dict] = field(default_factory=list)  # found Spotify tracks with "why"


def extract_playlist_id(text: str) -> str | None:
    m = PLAYLIST_URL_RE.search(text or "")
    return m.group(1) if m else None


def _entry_track(entry: dict) -> dict | None:
    """Playlist items carry the track under "item" (new API) or "track" (old)."""
    t = (entry or {}).get("item") or (entry or {}).get("track")
    if not t or t.get("type", "track") != "track" or not t.get("id"):
        return None
    return t


class PlaylistService:
    def __init__(
        self,
        music: MusicService,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self.music = music
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._search_sem = asyncio.Semaphore(5)

    @property
    def enabled(self) -> bool:
        return bool(self.redirect_uri)

    # --------------------------------------------------------------- OAuth

    def _oauth(self, user_id: int) -> SpotifyOAuth:
        return SpotifyOAuth(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            scope=SCOPES,
            cache_handler=DBCacheHandler(user_id),
            open_browser=False,
            show_dialog=True,
        )

    async def auth_url(self, user_id: int) -> str:
        state = await db.create_oauth_state(user_id)
        return self._oauth(user_id).get_authorize_url(state=state)

    async def finish_auth(self, state: str, code: str) -> tuple[int, str]:
        """Exchange the code from the redirect; returns (telegram_user_id, display_name)."""
        user_id = await db.pop_oauth_state(state)
        if user_id is None:
            raise ServiceError("Ссылка для входа устарела. Отправьте /connect ещё раз.")

        def _exchange() -> dict:
            oauth = self._oauth(user_id)
            oauth.get_access_token(code, as_dict=False, check_cache=False)
            return spotipy.Spotify(auth_manager=oauth).current_user()

        me = await asyncio.to_thread(_exchange)
        name = me.get("display_name") or me.get("id", "")
        await db.set_spotify_profile(user_id, me.get("id", ""), name)
        return user_id, name

    async def is_connected(self, user_id: int) -> bool:
        return await db.get_spotify_profile(user_id) is not None

    async def _client(self, user_id: int) -> spotipy.Spotify:
        if not await self.is_connected(user_id):
            raise ServiceError("Сначала подключите Spotify: /connect")
        return spotipy.Spotify(auth_manager=self._oauth(user_id), requests_timeout=15, retries=2, status_retries=0)

    @staticmethod
    async def _call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except spotipy.SpotifyException as exc:
            log.warning("Spotify API error: %s", exc)
            if exc.http_status in (401, 403):
                raise ServiceError(
                    "Spotify не дал доступ. Если плейлист не ваш — Spotify может не "
                    "отдавать его содержимое приложениям в режиме разработки. "
                    "Для своих плейлистов попробуйте переподключиться: /connect"
                ) from exc
            if exc.http_status == 404:
                raise ServiceError("Плейлист не найден или он приватный.") from exc
            raise ServiceError("Ошибка Spotify API, попробуйте позже.") from exc

    # ----------------------------------------------------------- playlists

    async def list_playlists(self, user_id: int, limit: int = 20) -> list[dict]:
        sp = await self._client(user_id)
        data = await self._call(sp.current_user_playlists, limit=limit)
        return [p for p in (data or {}).get("items", []) if p]

    async def _find_many(
        self, candidates: list[Candidate], exclude: set[str], clean: bool = False
    ) -> list[tuple[Candidate, dict | None]]:
        async def one(c: Candidate):
            async with self._search_sem:
                return c, await self.music.find_track(c, exclude, clean)

        return await asyncio.gather(*(one(c) for c in candidates))

    async def _create(self, sp: spotipy.Spotify, name: str, description: str, track_ids: list[str]) -> str:
        description = re.sub(r"[\r\n<>]+", " ", description)[:290]
        pl = await self._call(
            sp.current_user_playlist_create, name[:100], public=False, description=description
        )
        await self._add(sp, pl["id"], track_ids)
        return (pl.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/playlist/{pl['id']}"

    async def _add(self, sp: spotipy.Spotify, playlist_id: str, track_ids: list[str]) -> None:
        uris = [f"spotify:track:{t}" for t in track_ids]
        for i in range(0, len(uris), 100):
            await self._call(sp.playlist_add_items, playlist_id, uris[i : i + 100])

    async def create_from_prompt(self, user_id: int, request: str) -> PlaylistResult:
        sp = await self._client(user_id)
        clean = await db.get_no_explicit(user_id)
        prompt = CREATE_PROMPT.format(request=request[:400]) + (f"\n{CLEAN_HINT}" if clean else "")
        data = await self.music._ask_json(prompt, temperature=0.9)
        candidates = MusicService._parse_candidates(data)[:50]
        if not candidates:
            raise ServiceError("Нейросеть не предложила треки, попробуйте переформулировать запрос.")

        found = await self._find_many(candidates, set(), clean)
        ids, missing, used = [], [], set()
        for cand, item in found:
            if item and item["id"] not in used:
                used.add(item["id"])
                ids.append(item["id"])
            elif not item:
                missing.append(f"{cand.artist} — {cand.track}")
        if not ids:
            raise ServiceError("Не нашёл в Spotify ни одного трека для этого плейлиста 😔")

        name = str(data.get("name") or "DJ-бот: новый плейлист").strip()
        url = await self._create(sp, name, str(data.get("description") or request), ids)
        return PlaylistResult(name=name, url=url, added=len(ids), requested=len(candidates), missing=missing)

    async def create_from_seen(self, user_id: int) -> PlaylistResult:
        sp = await self._client(user_id)
        ids = list(reversed(await db.seen_track_ids_ordered(user_id, 100)))
        if not ids:
            raise ServiceError("Я ещё не показывал вам треков — сначала послушайте что-нибудь 🙂")
        name = "🎧 Находки от DJ-бота"
        url = await self._create(sp, name, "Треки, которые мне подобрал музыкальный DJ-бот в Telegram", ids)
        return PlaylistResult(name=name, url=url, added=len(ids), requested=len(ids))

    async def create_from_tracks(self, user_id: int, name: str, description: str, track_ids: list[str]) -> PlaylistResult:
        sp = await self._client(user_id)
        url = await self._create(sp, name, description, track_ids)
        return PlaylistResult(name=name, url=url, added=len(track_ids), requested=len(track_ids))

    async def add_tracks(self, user_id: int, playlist_id: str, track_ids: list[str]) -> None:
        sp = await self._client(user_id)
        await self._add(sp, playlist_id, track_ids)

    # ------------------------------------------------------------ analysis

    async def analyze(self, user_id: int, playlist_id: str) -> Analysis:
        profile = await db.get_spotify_profile(user_id)
        # Own token if connected (private playlists), otherwise app-only token.
        sp = await self._client(user_id) if profile else self.music.sp

        meta = await self._call(sp.playlist, playlist_id, fields="id,name,owner(id),external_urls")
        tracks: list[dict] = []
        offset = 0
        while len(tracks) < 300:
            page = await self._call(sp.playlist_items, playlist_id, limit=50, offset=offset)
            entries = (page or {}).get("items", [])
            tracks.extend(t for t in map(_entry_track, entries) if t)
            if not (page or {}).get("next") or not entries:
                break
            offset += 50
        if not tracks:
            raise ServiceError("В плейлисте нет треков (или Spotify не отдал его содержимое).")

        artists = Counter(a.get("name", "?") for t in tracks for a in t.get("artists", [])[:1])
        decades: Counter = Counter()
        for t in tracks:
            year = str((t.get("album") or {}).get("release_date", ""))[:4]
            if year.isdigit():
                decades[f"{year[:3]}0-е"] += 1
        pops = [t["popularity"] for t in tracks if isinstance(t.get("popularity"), int)]
        clean = await db.get_no_explicit(user_id)
        duration = sum(t.get("duration_ms") or 0 for t in tracks)

        an = Analysis(
            playlist_id=playlist_id,
            name=meta.get("name", "Плейлист"),
            url=(meta.get("external_urls") or {}).get("spotify")
            or f"https://open.spotify.com/playlist/{playlist_id}",
            owned=bool(profile and (meta.get("owner") or {}).get("id") == profile[0]),
            total=len(tracks),
            duration_ms=duration,
            top_artists=artists.most_common(5),
            decades=sorted(decades.items(), key=lambda kv: -kv[1])[:5],
            avg_popularity=round(sum(pops) / len(pops)) if pops else None,
            explicit=sum(1 for t in tracks if t.get("explicit")),
        )

        stats = (
            f"{an.total} треков; топ артистов: "
            + ", ".join(f"{a} ({n})" for a, n in an.top_artists)
            + "; десятилетия: "
            + ", ".join(f"{d}: {n}" for d, n in an.decades)
        )
        lines = []
        for t in tracks[:120]:
            artist = ", ".join(a.get("name", "") for a in t.get("artists", [])[:2])
            year = str((t.get("album") or {}).get("release_date", ""))[:4]
            lines.append(f"{artist} — {t.get('name', '')} ({year})")
        data = await self.music._ask_json(
            ANALYZE_PROMPT.format(name=an.name, stats=stats, tracks="\n".join(lines))
            + (f"\n{CLEAN_HINT}" if clean else ""),
            temperature=0.7,
        )
        an.verdict = str(data.get("verdict", "") or "")
        an.mood = str(data.get("mood", "") or "")
        an.genres = [str(g) for g in data.get("genres") or []][:6]
        an.text = str(data.get("analysis", "") or "")

        rec_cands = []
        for r in data.get("recommendations") or []:
            if isinstance(r, dict) and r.get("artist") and r.get("track"):
                rec_cands.append(Candidate(str(r["artist"]), str(r["track"]), why=str(r.get("why", ""))))
        existing = {t["id"] for t in tracks}
        for cand, item in await self._find_many(rec_cands[:8], existing, clean):
            if item and item["id"] not in existing:
                existing.add(item["id"])
                an.recs.append(
                    {
                        "id": item["id"],
                        "label": f"{', '.join(a['name'] for a in item.get('artists', []))} — {item.get('name', '')}",
                        "url": (item.get("external_urls") or {}).get("spotify")
                        or f"https://open.spotify.com/track/{item['id']}",
                        "why": cand.why,
                    }
                )
        return an
