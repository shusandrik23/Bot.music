"""SQLite storage: seen tracks, daily subscriptions and the user's last request.

sqlite3 is synchronous, so every public coroutine runs the query in a worker
thread via asyncio.to_thread and never blocks the bot's event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


@dataclass
class Subscription:
    user_id: int
    mode: str
    topic: str
    per_day: int
    active: bool


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def _execute(sql: str, params: tuple = (), fetch: str | None = None):
    with _lock:
        conn = _connect()
        cur = conn.execute(sql, params)
        if fetch == "one":
            result = cur.fetchone()
        elif fetch == "all":
            result = cur.fetchall()
        else:
            result = None
        conn.commit()
        return result


def _init_sync() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_tracks (
                user_id  INTEGER,
                track_id TEXT,
                label    TEXT DEFAULT '',
                seen_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, track_id)
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                mode    TEXT,
                topic   TEXT,
                per_day INTEGER,
                active  INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS last_requests (
                user_id INTEGER PRIMARY KEY,
                mode    TEXT,
                topic   TEXT
            );
            CREATE TABLE IF NOT EXISTS spotify_tokens (
                user_id      INTEGER PRIMARY KEY,
                token_json   TEXT,
                spotify_id   TEXT DEFAULT '',
                display_name TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state      TEXT PRIMARY KEY,
                user_id    INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


async def init_db() -> None:
    await asyncio.to_thread(_init_sync)


# ---------- seen tracks ----------

async def get_seen_ids(user_id: int) -> set[str]:
    rows = await asyncio.to_thread(
        _execute, "SELECT track_id FROM seen_tracks WHERE user_id = ?", (user_id,), "all"
    )
    return {r[0] for r in rows}


async def recent_labels(user_id: int, limit: int = 25) -> list[str]:
    """Human-readable 'Artist — Track' of recently shown tracks (a hint for the LLM)."""
    rows = await asyncio.to_thread(
        _execute,
        "SELECT label FROM seen_tracks WHERE user_id = ? AND label != '' "
        "ORDER BY seen_at DESC LIMIT ?",
        (user_id, limit),
        "all",
    )
    return [r[0] for r in rows]


async def add_seen(user_id: int, track_id: str, label: str = "") -> None:
    await asyncio.to_thread(
        _execute,
        "INSERT OR IGNORE INTO seen_tracks (user_id, track_id, label) VALUES (?, ?, ?)",
        (user_id, track_id, label),
    )


# ---------- last request ("🔄 Ещё так же") ----------

async def set_last_request(user_id: int, mode: str, topic: str) -> None:
    await asyncio.to_thread(
        _execute,
        "INSERT INTO last_requests (user_id, mode, topic) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode, topic = excluded.topic",
        (user_id, mode, topic),
    )


async def get_last_request(user_id: int) -> tuple[str, str] | None:
    row = await asyncio.to_thread(
        _execute, "SELECT mode, topic FROM last_requests WHERE user_id = ?", (user_id,), "one"
    )
    return (row[0], row[1] or "") if row else None


# ---------- subscriptions ----------

async def get_subscription(user_id: int) -> Subscription | None:
    row = await asyncio.to_thread(
        _execute,
        "SELECT user_id, mode, topic, per_day, active FROM subscriptions WHERE user_id = ?",
        (user_id,),
        "one",
    )
    if not row:
        return None
    return Subscription(row[0], row[1], row[2] or "", row[3], bool(row[4]))


async def upsert_subscription(user_id: int, mode: str, topic: str, per_day: int) -> None:
    await asyncio.to_thread(
        _execute,
        "INSERT INTO subscriptions (user_id, mode, topic, per_day, active) VALUES (?, ?, ?, ?, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode, topic = excluded.topic, "
        "per_day = excluded.per_day, active = 1",
        (user_id, mode, topic, per_day),
    )


async def set_subscription_active(user_id: int, active: bool) -> None:
    await asyncio.to_thread(
        _execute,
        "UPDATE subscriptions SET active = ? WHERE user_id = ?",
        (1 if active else 0, user_id),
    )


async def get_active_subscriptions(min_per_day: int) -> list[Subscription]:
    rows = await asyncio.to_thread(
        _execute,
        "SELECT user_id, mode, topic, per_day, active FROM subscriptions "
        "WHERE active = 1 AND per_day >= ?",
        (min_per_day,),
        "all",
    )
    return [Subscription(r[0], r[1], r[2] or "", r[3], bool(r[4])) for r in rows]


async def seen_track_ids_ordered(user_id: int, limit: int = 100) -> list[str]:
    rows = await asyncio.to_thread(
        _execute,
        "SELECT track_id FROM seen_tracks WHERE user_id = ? ORDER BY seen_at DESC LIMIT ?",
        (user_id, limit),
        "all",
    )
    return [r[0] for r in rows]


# ---------- Spotify user OAuth ----------
# Sync variants are used by spotipy's cache handler (always called from a worker thread).

def get_token_sync(user_id: int) -> dict | None:
    row = _execute("SELECT token_json FROM spotify_tokens WHERE user_id = ?", (user_id,), "one")
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def save_token_sync(user_id: int, token: dict) -> None:
    _execute(
        "INSERT INTO spotify_tokens (user_id, token_json) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET token_json = excluded.token_json",
        (user_id, json.dumps(token)),
    )


async def set_spotify_profile(user_id: int, spotify_id: str, display_name: str) -> None:
    await asyncio.to_thread(
        _execute,
        "UPDATE spotify_tokens SET spotify_id = ?, display_name = ? WHERE user_id = ?",
        (spotify_id, display_name, user_id),
    )


async def get_spotify_profile(user_id: int) -> tuple[str, str] | None:
    """(spotify_id, display_name) if the user connected Spotify."""
    row = await asyncio.to_thread(
        _execute,
        "SELECT spotify_id, display_name FROM spotify_tokens "
        "WHERE user_id = ? AND token_json IS NOT NULL AND spotify_id != ''",
        (user_id,),
        "one",
    )
    return (row[0], row[1] or row[0]) if row else None


async def delete_spotify_token(user_id: int) -> None:
    await asyncio.to_thread(_execute, "DELETE FROM spotify_tokens WHERE user_id = ?", (user_id,))


async def create_oauth_state(user_id: int) -> str:
    state = secrets.token_urlsafe(24)
    await asyncio.to_thread(
        _execute, "DELETE FROM oauth_states WHERE created_at < datetime('now', '-1 day')"
    )
    await asyncio.to_thread(
        _execute, "INSERT INTO oauth_states (state, user_id) VALUES (?, ?)", (state, user_id)
    )
    return state


async def pop_oauth_state(state: str) -> int | None:
    row = await asyncio.to_thread(
        _execute,
        "SELECT user_id FROM oauth_states WHERE state = ? "
        "AND created_at >= datetime('now', '-1 hour')",
        (state,),
        "one",
    )
    await asyncio.to_thread(_execute, "DELETE FROM oauth_states WHERE state = ?", (state,))
    return row[0] if row else None
