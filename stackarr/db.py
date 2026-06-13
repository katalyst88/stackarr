"""SQLite storage (WAL, per-call connections). One file in DATA_DIR, with a
schema covering every Stackarr feature: multi-user, per-user suggestions,
requests, taste signals (positive + negative), 5-star ratings, a library
snapshot for deletion-detection, and import-list items."""
import os
import secrets
import sqlite3
from contextlib import contextmanager

from . import config

DB_PATH = os.path.join(config.DATA_DIR, "stackarr.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  abs_user_id TEXT UNIQUE,
  username TEXT UNIQUE NOT NULL,
  abs_token TEXT,                              -- this user's ABS token (for their history)
  role TEXT NOT NULL DEFAULT 'user',           -- user | admin
  created_at TEXT DEFAULT (datetime('now','localtime')),
  last_login TEXT
);
CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  asin TEXT,
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  narrator TEXT DEFAULT '',
  series TEXT DEFAULT '',
  cover TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  lane TEXT DEFAULT 'foryou',                  -- foryou | series | narrator | discover | importlist
  score REAL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | rejected
  extra TEXT DEFAULT '',                        -- e.g. release date for upcoming titles
  created_at TEXT DEFAULT (datetime('now','localtime')),
  decided_at TEXT,
  UNIQUE(user_id, asin)
);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  asin TEXT,
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  cover TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',       -- queued | handed | available | failed
  detail TEXT DEFAULT '',
  chaptarr_ref TEXT DEFAULT '',                -- chaptarr author/book id
  source TEXT DEFAULT 'suggestion',            -- suggestion | manual | importlist
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                          -- asin | author | series | narrator
  value TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0,              -- >0 positive (liked/read), <0 negative (pass/DNF/deleted)
  why TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, kind, value)
);
CREATE TABLE IF NOT EXISTS ratings (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  asin TEXT NOT NULL,
  title TEXT DEFAULT '',
  author TEXT DEFAULT '',
  stars INTEGER NOT NULL,                      -- 1..5
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, asin)
);
CREATE TABLE IF NOT EXISTS library (
  item_id TEXT PRIMARY KEY,
  library_id TEXT,
  title TEXT,
  author TEXT,
  asin TEXT,
  first_seen TEXT DEFAULT (datetime('now','localtime')),
  last_seen TEXT,
  gone_at TEXT
);
CREATE TABLE IF NOT EXISTS importlist_items (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  source TEXT NOT NULL,                        -- goodreads | hardcover
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  asin TEXT DEFAULT '',
  added_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, source, title, author)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_sugg_user_status ON suggestions(user_id, status);
CREATE INDEX IF NOT EXISTS ix_req_user_status ON requests(user_id, status);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        # lightweight migrations for existing DBs
        for stmt in ("ALTER TABLE suggestions ADD COLUMN extra TEXT DEFAULT ''",):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass


def get_meta(k: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default


def set_meta(k: str, v: str):
    with conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def setting(key: str, fallback: str = "") -> str:
    """Runtime setting: in-app value (DB) wins, else env/config fallback.
    Lets service connections be configured in the UI instead of only env."""
    v = get_meta(key, "")
    return v if v else fallback


def secret_key() -> str:
    s = get_meta("secret_key")
    if not s:
        s = secrets.token_hex(32)
        set_meta("secret_key", s)
    return s


def upsert_user(abs_user_id: str, username: str, abs_token: str, role: str) -> dict:
    with conn() as c:
        c.execute(
            "INSERT INTO users (abs_user_id, username, abs_token, role, last_login) "
            "VALUES (?,?,?,?, datetime('now','localtime')) "
            "ON CONFLICT(username) DO UPDATE SET abs_token=excluded.abs_token, "
            "abs_user_id=excluded.abs_user_id, role=excluded.role, "
            "last_login=datetime('now','localtime')",
            (abs_user_id, username, abs_token, role))
        return dict(c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())


def get_user(user_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None
