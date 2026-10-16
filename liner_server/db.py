import json
import sqlite3
import threading
from pathlib import Path

from .config import DATA_DIR

DB_PATH = DATA_DIR / "liner.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    username TEXT UNIQUE,
    display_name TEXT,
    avatar_url TEXT,
    bio TEXT,
    password_hash TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    user_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS likes (
    user_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    liked_at TEXT NOT NULL,
    PRIMARY KEY (user_id, track_id)
);
CREATE TABLE IF NOT EXISTS saved (
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, entity_id)
);
CREATE TABLE IF NOT EXISTS playlists (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    is_public INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_items (
    id TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    position REAL NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    playback_session_id TEXT,
    listening_session_id TEXT,
    played_ms INTEGER NOT NULL,
    completion REAL NOT NULL,
    completed INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    loop_count INTEGER NOT NULL,
    seek_back INTEGER NOT NULL,
    context TEXT,
    listened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lyrics (
    track_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS covers (
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    file TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, target_id)
);
CREATE INDEX IF NOT EXISTS history_user ON history (user_id, listened_at);
CREATE INDEX IF NOT EXISTS items_playlist ON playlist_items (playlist_id, position);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        _local.conn = c
    return c


def init() -> None:
    conn().executescript(SCHEMA)


def one(sql: str, *args):
    return conn().execute(sql, args).fetchone()


def all_(sql: str, *args):
    return conn().execute(sql, args).fetchall()


def run(sql: str, *args) -> None:
    conn().execute(sql, args)


def put_track(track: dict) -> None:
    if track and track.get("id"):
        run(
            "INSERT INTO tracks (id, data) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            track["id"],
            json.dumps(track),
        )


def get_track(track_id: str) -> dict | None:
    row = one("SELECT data FROM tracks WHERE id = ?", track_id)
    return json.loads(row["data"]) if row else None


def put_entity(kind: str, entity: dict) -> None:
    if entity and entity.get("id"):
        run(
            "INSERT INTO entities (kind, id, data) VALUES (?, ?, ?) ON CONFLICT(kind, id) DO UPDATE SET data = excluded.data",
            kind,
            entity["id"],
            json.dumps(entity),
        )


def get_entity(kind: str, entity_id: str) -> dict | None:
    row = one("SELECT data FROM entities WHERE kind = ? AND id = ?", kind, entity_id)
    return json.loads(row["data"]) if row else None
