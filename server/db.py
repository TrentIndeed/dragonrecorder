import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    slug        TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|ready|expired|failed
    title       TEXT,
    description TEXT,
    title_is_ai INTEGER NOT NULL DEFAULT 1,
    duration_s  REAL,
    size_bytes  INTEGER,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ready_at    TEXT,
    expires_at  TEXT,
    has_thumb   INTEGER NOT NULL DEFAULT 0,
    has_vtt     INTEGER NOT NULL DEFAULT 0,
    has_words   INTEGER NOT NULL DEFAULT 0,
    transcript  TEXT
);

CREATE TABLE IF NOT EXISTS views (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL,
    viewer_id   TEXT NOT NULL,
    label       TEXT,
    is_owner    INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen   TEXT,
    watched_s   REAL NOT NULL DEFAULT 0,
    max_pos_s   REAL NOT NULL DEFAULT 0,
    ranges      TEXT NOT NULL DEFAULT '[]',       -- merged [start,end] seconds
    UNIQUE (slug, viewer_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL,
    viewer_id   TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT 'Anonymous',
    body        TEXT NOT NULL,
    at_s        REAL,                             -- optional timestamp pin
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS reactions (
    slug        TEXT NOT NULL,
    viewer_id   TEXT NOT NULL,
    emoji       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (slug, viewer_id, emoji)
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edits (
    slug        TEXT NOT NULL,
    kind        TEXT NOT NULL,                    -- fillers|silences|captions
    count       INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 0,
    has_render  INTEGER NOT NULL DEFAULT 0,
    data        TEXT,                             -- detector output (cut list etc.)
    PRIMARY KEY (slug, kind)
);
"""


def init_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)


@contextmanager
def connect():
    db = sqlite3.connect(config.DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()
