"""SQLite storage (WAL, per-call connections). One file in DATA_DIR, with a
schema covering every Stackarr feature: multi-user, per-user suggestions,
requests, taste signals (positive + negative), 5-star ratings, a library
snapshot for deletion-detection, and import-list items."""
import os
import re
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
  format TEXT DEFAULT 'audiobook',             -- audiobook | ebook
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
  format TEXT DEFAULT 'audiobook',             -- audiobook | ebook
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
  review TEXT DEFAULT '',                       -- optional free-text review (shared)
  format TEXT DEFAULT 'audiobook',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT,
  UNIQUE(user_id, asin)
);
CREATE TABLE IF NOT EXISTS library (
  item_id TEXT PRIMARY KEY,
  library_id TEXT,
  title TEXT,
  author TEXT,
  asin TEXT,
  series TEXT DEFAULT '',
  series_seq REAL,
  narrator TEXT DEFAULT '',
  format TEXT DEFAULT 'audiobook',             -- audiobook | ebook
  source TEXT DEFAULT 'abs',                    -- backend id the book came from
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
CREATE TABLE IF NOT EXISTS shelf (
  user_id INTEGER NOT NULL,
  rkey TEXT NOT NULL,                          -- rating_key (asin / t-slug / gb:/ol:)
  state TEXT NOT NULL,                         -- want | reading | read
  title TEXT DEFAULT '', author TEXT DEFAULT '', cover TEXT DEFAULT '',
  format TEXT DEFAULT 'audiobook',
  added_at TEXT DEFAULT (datetime('now','localtime')),
  finished_at TEXT,                            -- set when state -> read (heatmap/goal)
  PRIMARY KEY (user_id, rkey)
);
CREATE TABLE IF NOT EXISTS book_tags (
  rkey TEXT NOT NULL,
  tag TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'genre',          -- genre | mood | pace | warning
  source TEXT NOT NULL DEFAULT 'auto',         -- auto | user
  PRIMARY KEY (rkey, tag)
);
CREATE TABLE IF NOT EXISTS review_votes (
  user_id INTEGER NOT NULL,
  rating_id INTEGER NOT NULL,
  PRIMARY KEY (user_id, rating_id)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_shelf_user_state ON shelf(user_id, state);
CREATE INDEX IF NOT EXISTS ix_tags_kind ON book_tags(kind);
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
        for stmt in ("ALTER TABLE suggestions ADD COLUMN extra TEXT DEFAULT ''",
                     "ALTER TABLE library ADD COLUMN series TEXT DEFAULT ''",
                     "ALTER TABLE library ADD COLUMN series_seq REAL",
                     "ALTER TABLE library ADD COLUMN narrator TEXT DEFAULT ''",
                     "ALTER TABLE library ADD COLUMN format TEXT DEFAULT 'audiobook'",
                     "ALTER TABLE library ADD COLUMN source TEXT DEFAULT 'abs'",
                     "ALTER TABLE suggestions ADD COLUMN format TEXT DEFAULT 'audiobook'",
                     "ALTER TABLE requests ADD COLUMN format TEXT DEFAULT 'audiobook'",
                     "ALTER TABLE ratings ADD COLUMN review TEXT DEFAULT ''",
                     "ALTER TABLE ratings ADD COLUMN format TEXT DEFAULT 'audiobook'",
                     "ALTER TABLE ratings ADD COLUMN updated_at TEXT",
                     "ALTER TABLE ratings ADD COLUMN spoiler INTEGER DEFAULT 0",
                     "ALTER TABLE signals ADD COLUMN format TEXT DEFAULT ''"):
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


def rating_key(asin: str, title: str, author: str) -> str:
    """Stable identity for a book in History & ratings: the real ASIN when we
    have one, else a slug of title+author. Most ABS library books have no ASIN,
    so the slug lets them be rated/removed; it's reproducible across page loads
    and shared by the recommender so 'removed' books stop seeding suggestions.
    Alphanumeric+dashes, so it's safe in HTML attributes and JS strings."""
    if asin:
        return asin
    base = f"{(title or '').strip().lower()} {(author or '').split(',')[0].strip().lower()}"
    return "t-" + re.sub(r"[^a-z0-9]+", "-", base).strip("-")


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


# --- shared ratings & reviews (community signal across all Stackarr users) ---
def community_rating(key: str) -> dict:
    """Aggregate star rating for a book across every user. {avg, count}."""
    with conn() as c:
        r = c.execute("SELECT ROUND(AVG(stars),1) a, COUNT(*) n FROM ratings WHERE asin=?",
                      (key,)).fetchone()
    return {"avg": r["a"] or 0, "count": r["n"] or 0}


def reviews_for(key: str) -> list[dict]:
    """Text reviews other users have left for a book, most-helpful first then
    newest. Carries id (for voting), spoiler flag, and helpful-vote count."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT r.id, r.stars, r.review, r.spoiler, r.created_at, u.username, "
            "(SELECT COUNT(*) FROM review_votes v WHERE v.rating_id=r.id) AS votes "
            "FROM ratings r JOIN users u ON u.id=r.user_id "
            "WHERE r.asin=? AND r.review<>'' ORDER BY votes DESC, COALESCE(r.updated_at,r.created_at) DESC",
            (key,))]


# --- personal shelves (want / reading / read) -------------------------------
def shelf_set(user_id: int, rkey: str, state: str, title="", author="", cover="", fmt="audiobook"):
    """Put a book on a shelf (want|reading|read). 'read' stamps finished_at
    (drives the goal + heatmap). state='' removes it from shelves."""
    with conn() as c:
        if not state:
            c.execute("DELETE FROM shelf WHERE user_id=? AND rkey=?", (user_id, rkey))
            return
        fin = "datetime('now','localtime')" if state == "read" else "NULL"
        c.execute(
            f"INSERT INTO shelf (user_id,rkey,state,title,author,cover,format,finished_at) "
            f"VALUES (?,?,?,?,?,?,?,{fin}) "
            f"ON CONFLICT(user_id,rkey) DO UPDATE SET state=excluded.state, "
            f"title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE shelf.title END, "
            f"author=CASE WHEN excluded.author<>'' THEN excluded.author ELSE shelf.author END, "
            f"cover=CASE WHEN excluded.cover<>'' THEN excluded.cover ELSE shelf.cover END, "
            f"format=excluded.format, "
            f"finished_at=CASE WHEN excluded.state='read' AND shelf.finished_at IS NULL "
            f"THEN datetime('now','localtime') WHEN excluded.state<>'read' THEN NULL ELSE shelf.finished_at END",
            (user_id, rkey, state, title, author, cover, fmt))


def shelf_state(user_id: int, rkey: str) -> str:
    with conn() as c:
        r = c.execute("SELECT state FROM shelf WHERE user_id=? AND rkey=?", (user_id, rkey)).fetchone()
        return r["state"] if r else ""


def shelf_list(user_id: int, state: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM shelf WHERE user_id=? AND state=? ORDER BY COALESCE(finished_at,added_at) DESC",
            (user_id, state))]


def shelf_counts(user_id: int) -> dict:
    with conn() as c:
        rows = c.execute("SELECT state, COUNT(*) n FROM shelf WHERE user_id=? GROUP BY state", (user_id,)).fetchall()
    return {r["state"]: r["n"] for r in rows}


def finished_dates(user_id: int) -> list[str]:
    """YYYY-MM-DD of every 'read'-shelf finish — feeds the goal ring + heatmap."""
    with conn() as c:
        return [r["d"] for r in c.execute(
            "SELECT substr(finished_at,1,10) d FROM shelf WHERE user_id=? AND state='read' AND finished_at IS NOT NULL",
            (user_id,))]


# --- book tags (mood / pace / genre / content-warning) ----------------------
def set_tags(rkey: str, tags: list[tuple], replace_kind: str | None = None):
    """tags: list of (tag, kind). If replace_kind given, clear that kind's
    'auto' tags for this book first (so a re-fetch refreshes cleanly)."""
    with conn() as c:
        if replace_kind:
            c.execute("DELETE FROM book_tags WHERE rkey=? AND kind=? AND source='auto'", (rkey, replace_kind))
        for tag, kind in tags:
            t = (tag or "").strip()
            if t:
                c.execute("INSERT OR IGNORE INTO book_tags (rkey,tag,kind,source) VALUES (?,?,?,?)",
                          (rkey, t, kind, "auto"))


def tags_for(rkey: str) -> dict:
    with conn() as c:
        rows = c.execute("SELECT tag, kind FROM book_tags WHERE rkey=?", (rkey,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["kind"], []).append(r["tag"])
    return out


def has_tags(rkey: str) -> bool:
    with conn() as c:
        return bool(c.execute("SELECT 1 FROM book_tags WHERE rkey=? LIMIT 1", (rkey,)).fetchone())


def review_vote(user_id: int, rating_id: int) -> int:
    """Toggle a helpful vote; returns the new vote count for that review."""
    with conn() as c:
        ex = c.execute("SELECT 1 FROM review_votes WHERE user_id=? AND rating_id=?",
                       (user_id, rating_id)).fetchone()
        if ex:
            c.execute("DELETE FROM review_votes WHERE user_id=? AND rating_id=?", (user_id, rating_id))
        else:
            c.execute("INSERT OR IGNORE INTO review_votes (user_id,rating_id) VALUES (?,?)", (user_id, rating_id))
        return c.execute("SELECT COUNT(*) n FROM review_votes WHERE rating_id=?", (rating_id,)).fetchone()["n"]


def recent_ratings(limit: int = 14) -> list[dict]:
    """Most-recently-rated books across all users — the 'Recently rated'
    discovery row. One row per book (latest rating wins)."""
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT r.asin, r.title, r.author, r.stars, r.review, r.format, u.username, "
            "COALESCE(r.updated_at, r.created_at) AS ts "
            "FROM ratings r JOIN users u ON u.id=r.user_id "
            "WHERE r.title<>'' ORDER BY ts DESC LIMIT 120")]
    seen, out = set(), []
    for r in rows:
        k = (r["title"] or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return out
