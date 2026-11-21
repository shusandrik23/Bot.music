"""Podcasts: analyse the user's podcasts and recommend new ones.

Two sources of "what the user likes":
- their saved shows in Spotify (needs /connect with the user-library-read scope);
- a list of podcasts typed in the chat (works for everyone, no Spotify login).
Recommendations come from Gemini and are looked up in Spotify search (type=show).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import quote

import spotipy

import database as db
from playlists import PlaylistService
from services import CLEAN_HINT, MusicService, ServiceError, _norm, _today_hint

log = logging.getLogger(__name__)

# Spotify needs a market (country) for show search with an app-only token. Podcasts are
# licensed per country, so several markets are tried in order.
MARKETS = [
    m.strip().upper()
    for m in (os.environ.get("SPOTIFY_MARKETS") or os.environ.get("SPOTIFY_MARKET") or "US,DE,KZ").split(",")
    if m.strip()
][:4] or ["US"]

ANALYZE_PROMPT = """{today}
Вот подкасты, которые слушает пользователь:
{shows}

Проанализируй его вкус в подкастах.
Ответ JSON:
{{"profile": "3-5 предложений: какие темы, форматы (интервью, сторителлинг, новости, юмор, научпоп…), тон и языки ему нравятся — живо, как портрет слушателя",
  "themes": ["4-6 главных тем"],
  "recommendations": [{{"title": "точное название подкаста как на Spotify", "publisher": "автор/студия", "why": "1-2 предложения, почему понравится именно ему"}}]}}
В recommendations дай 8 реальных подкастов, которые есть на Spotify и которых НЕТ в списке выше.
Учитывай языки, на которых он слушает: в основном предлагай подкасты на тех же языках.{extra}"""

TOPIC_PROMPT = """{today}
Пользователь ищет подкаст: «{topic}».
Ответ JSON:
{{"intro": "1-2 предложения о теме",
  "recommendations": [{{"title": "точное название подкаста как на Spotify", "publisher": "автор/студия", "why": "1-2 предложения, о чём он и чем хорош"}}]}}
Дай 8 реальных подкастов, которые есть на Spotify. Если язык не указан — смешай русскоязычные
и лучшие англоязычные.{extra}"""


@dataclass
class PodcastRec:
    name: str
    publisher: str
    url: str
    why: str = ""
    episodes: int | None = None
    found: bool = True  # False: not found by search, url is a Spotify search link


@dataclass
class PodcastResult:
    profile: str = ""
    themes: list[str] = field(default_factory=list)
    intro: str = ""
    recs: list[PodcastRec] = field(default_factory=list)
    source_count: int = 0


class PodcastService:
    def __init__(self, music: MusicService, playlists: PlaylistService) -> None:
        self.music = music
        self.playlists = playlists
        self._sem = asyncio.Semaphore(5)
        # user_id -> names already recommended (so "more" gives new ones)
        self._shown: dict[int, list[str]] = {}

    # ------------------------------------------------------------- sources

    async def saved_shows(self, user_id: int) -> list[dict]:
        sp = await self.playlists._client(user_id)
        shows: list[dict] = []
        offset = 0
        while len(shows) < 100:
            page = await PlaylistService._call(sp.current_user_saved_shows, limit=50, offset=offset)
            items = (page or {}).get("items", [])
            shows.extend(i["show"] for i in items if i and i.get("show"))
            if not (page or {}).get("next") or not items:
                break
            offset += 50
        return shows

    # --------------------------------------------------------------- search

    @staticmethod
    def _title_matches(want: str, got: str) -> bool:
        if not want or not got:
            return False
        if want in got or got in want:
            return True
        w, g = set(want.split()), set(got.split())
        return bool(w) and len(w & g) / len(w) >= 0.6

    async def _find_show(self, title: str, publisher: str, sp: spotipy.Spotify, markets: list[str],
                         exclude: set[str], clean: bool) -> dict | None:
        def _do(q: str, market: str) -> list[dict]:
            res = sp.search(q=q, type="show", limit=5, market=market)
            return [s for s in ((res or {}).get("shows", {}).get("items") or []) if s]

        want, want_pub = _norm(title), _norm(publisher)
        for n, market in enumerate(markets):
            # Shows are licensed per country: if not found in one market, try the next.
            queries = [f"{title} {publisher}".strip(), title] if n == 0 else [title]
            for q in dict.fromkeys(queries):
                try:
                    async with self._sem:
                        items = await asyncio.to_thread(_do, q, market)
                except spotipy.SpotifyException as exc:
                    log.warning("Spotify show search failed for %r (%s): %s", q, market, exc)
                    if exc.http_status == 429:
                        return None
                    continue
                except Exception as exc:
                    log.warning("Spotify show search failed for %r (%s): %s", q, market, exc)
                    continue
                for s in items:
                    if s.get("id") in exclude or (clean and s.get("explicit")):
                        continue
                    got = _norm(s.get("name", ""))
                    pub = _norm(s.get("publisher", ""))
                    if self._title_matches(want, got) or (
                        want_pub and pub and want_pub in pub and self._title_matches(want, got + " " + pub)
                    ):
                        return s
        log.info("Podcast not found on Spotify: %r / %r (markets %s)", title, publisher, markets)
        return None

    async def _resolve(self, user_id: int, recs: list, exclude_ids: set[str],
                       exclude_names: set[str]) -> list[PodcastRec]:
        clean = await db.get_no_explicit(user_id)
        if await self.playlists.is_connected(user_id):
            sp, markets = await self.playlists._client(user_id), ["from_token", *MARKETS]
        else:
            sp, markets = self.music.sp, MARKETS

        wanted = []
        for r in recs:
            if not isinstance(r, dict) or not r.get("title"):
                continue
            if _norm(str(r["title"])) in exclude_names:
                continue
            wanted.append(r)

        found = await asyncio.gather(*(
            self._find_show(str(r["title"]), str(r.get("publisher", "")), sp, markets, exclude_ids, clean)
            for r in wanted[:8]
        ))
        result, used = [], set()
        for r, show in zip(wanted, found):
            if not show:
                # Not found in the searched markets: still recommend it, with a Spotify search link.
                title = str(r["title"])
                result.append(PodcastRec(
                    name=title,
                    publisher=str(r.get("publisher", "") or ""),
                    url="https://open.spotify.com/search/" + quote(title) + "/podcasts",
                    why=str(r.get("why", "") or ""),
                    found=False,
                ))
                continue
            if show["id"] not in used:
                used.add(show["id"])
                result.append(PodcastRec(
                    name=show.get("name", r["title"]),
                    publisher=show.get("publisher", "") or str(r.get("publisher", "")),
                    url=(show.get("external_urls") or {}).get("spotify")
                    or f"https://open.spotify.com/show/{show['id']}",
                    why=str(r.get("why", "") or ""),
                    episodes=show.get("total_episodes"),
                ))
        shown = self._shown.setdefault(user_id, [])
        shown.extend(x.name for x in result)
        del shown[:-60]
        return result

    def _extra(self, user_id: int, clean: bool) -> str:
        extra = ""
        shown = self._shown.get(user_id) or []
        if shown:
            extra += "\nНЕ предлагай эти подкасты — они уже были: " + "; ".join(shown[-30:])
        if clean:
            extra += "\n" + CLEAN_HINT.replace("треки", "подкасты")
        return extra

    # ------------------------------------------------------------- analysis

    async def analyze(self, user_id: int, typed: str | None = None) -> PodcastResult:
        """Analyse saved Spotify shows (typed=None) or a list typed by the user."""
        clean = await db.get_no_explicit(user_id)
        exclude_ids: set[str] = set()
        if typed is None:
            shows = await self.saved_shows(user_id)
            if not shows:
                raise ServiceError(
                    "В вашей библиотеке Spotify нет подкастов. Подпишитесь на пару подкастов "
                    "в Spotify или просто перечислите любимые подкасты сообщением."
                )
            exclude_ids = {s["id"] for s in shows if s.get("id")}
            names = [s.get("name", "") for s in shows]
            listing = "\n".join(
                f"- {s.get('name', '')} ({s.get('publisher', '')}, язык: "
                f"{', '.join(s.get('languages') or []) or '?'}): "
                f"{(s.get('description') or '')[:160]}"
                for s in shows[:60]
            )
            count = len(shows)
        else:
            listing = typed.strip()[:1500]
            names = [x.strip() for x in listing.replace(";", "\n").replace(",", "\n").split("\n") if x.strip()]
            count = len(names)

        data = await self.music._ask_json(
            ANALYZE_PROMPT.format(today=_today_hint(), shows=listing, extra=self._extra(user_id, clean)),
            temperature=0.8,
        )
        recs = await self._resolve(
            user_id, data.get("recommendations") or [], exclude_ids, {_norm(n) for n in names}
        )
        if not recs:
            raise ServiceError("Нейросеть не предложила подкасты, попробуйте ещё раз.")
        return PodcastResult(
            profile=str(data.get("profile", "") or ""),
            themes=[str(t) for t in data.get("themes") or []][:6],
            recs=recs,
            source_count=count,
        )

    async def by_topic(self, user_id: int, topic: str) -> PodcastResult:
        clean = await db.get_no_explicit(user_id)
        data = await self.music._ask_json(
            TOPIC_PROMPT.format(today=_today_hint(), topic=topic[:300], extra=self._extra(user_id, clean)),
            temperature=0.9,
        )
        recs = await self._resolve(user_id, data.get("recommendations") or [], set(), set())
        if not recs:
            raise ServiceError("Нейросеть не предложила подкасты, попробуйте другую тему.")
        return PodcastResult(intro=str(data.get("intro", "") or ""), recs=recs)
