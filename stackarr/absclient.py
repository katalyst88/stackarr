"""Audiobookshelf client. Handles multi-user login (users sign in with their
own ABS credentials), reads each user's listening history (the recommendation
seed), and lists library contents for dedupe + deletion detection."""
import logging
import re

import requests

from . import config, db


def _dedup_key(title: str, author: str) -> tuple:
    """Collapse multi-disc / CD fragments of the same book to one key."""
    t = (title or "").lower()
    t = re.sub(r"\(.*?\)", "", t)                                   # drop "(unabridged)" etc.
    t = re.sub(r"\b(disc|cd|part|vol|volume)\s*\d+\b", "", t)       # drop disc/cd markers
    t = re.sub(r"\s+\d+\s*$", "", t)                               # drop a trailing number
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return (t, (author or "").split(",")[0].strip().lower())

log = logging.getLogger("stackarr.abs")


def abs_url() -> str:
    return db.setting("abs_url", config.ABS_URL).rstrip("/")


def admin_token() -> str:
    return db.setting("abs_admin_token", config.ABS_ADMIN_TOKEN)


def _admin_headers():
    return {"Authorization": f"Bearer {admin_token()}"}


def login(username: str, password: str) -> dict | None:
    """Authenticate against Audiobookshelf. Returns {id, username, token,
    isAdmin} on success, None on bad credentials."""
    try:
        r = requests.post(f"{abs_url()}/login",
                          json={"username": username, "password": password}, timeout=20)
        if r.status_code != 200:
            return None
        u = r.json().get("user") or {}
        if not u.get("token"):
            return None
        return {"id": u.get("id", ""), "username": u.get("username", username),
                "token": u["token"], "isAdmin": u.get("type") in ("admin", "root")}
    except Exception as e:
        log.warning("ABS login failed for %s: %s", username, e)
        return None


def _user_get(token: str, path: str, params: dict | None = None):
    r = requests.get(f"{abs_url()}{path}", params=params or {},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def listening_history(token: str) -> list[dict]:
    """Books this user has finished or made real progress on, recent first.
    Each: {item_id, finished, progress, last_update}."""
    out = []
    try:
        me = _user_get(token, "/api/me")
        for mp in me.get("mediaProgress", []):
            if mp.get("isFinished") or (mp.get("progress") or 0) >= 0.25:
                out.append({
                    "item_id": mp.get("libraryItemId", ""),
                    "finished": bool(mp.get("isFinished")),
                    "progress": mp.get("progress") or 0,
                    "last_update": mp.get("lastUpdate") or 0,
                })
    except Exception as e:
        log.warning("listening_history failed: %s", e)
    out.sort(key=lambda x: x["last_update"], reverse=True)
    return out


def listening_stats(token: str) -> dict:
    """Totals from ABS for the fun-facts insights page."""
    try:
        d = _user_get(token, "/api/me/listening-stats")
        return {"total_seconds": d.get("totalTime", 0),
                "days_listened": len(d.get("days", {}) or {}),
                "items_count": len(d.get("items", {}) or {})}
    except Exception as e:
        log.warning("listening_stats failed: %s", e)
        return {"total_seconds": 0, "days_listened": 0, "items_count": 0}


def libraries() -> list[dict]:
    libs = _user_get(admin_token(), "/api/libraries").get("libraries", [])
    libs = [l for l in libs if l.get("mediaType") == "book"]
    if config.ABS_LIBRARY_IDS:
        libs = [l for l in libs if l["id"] in config.ABS_LIBRARY_IDS]
    return libs


def items(library_id: str) -> list[dict]:
    out, page = [], 0
    while True:
        d = _user_get(admin_token(), f"/api/libraries/{library_id}/items",
                      {"limit": 200, "page": page})
        batch = d.get("results", [])
        out.extend(batch)
        page += 1
        if not batch or len(out) >= d.get("total", 0):
            return out


def recent_added(limit: int = 14) -> list[dict]:
    """Most recently added audiobooks across libraries, for the dashboard row.
    Each: {item_id, title, author, asin, cover, added}."""
    out = []
    tok = admin_token()
    for lib in libraries():
        try:
            d = _user_get(tok, f"/api/libraries/{lib['id']}/items",
                          {"limit": limit, "sort": "addedAt", "desc": 1})
            for it in d.get("results", []):
                m = item_meta(it)
                if not m["item_id"]:
                    continue
                m["added"] = it.get("addedAt", 0)
                out.append(m)            # cover served via Stackarr's /cover/<item_id> proxy
        except Exception as e:
            log.warning("recent_added failed for %s: %s", lib.get("name"), e)
    out.sort(key=lambda x: x.get("added", 0), reverse=True)
    # one entry per book (dedupe by title+author, keep the most recent)
    seen, uniq = set(), []
    for m in out:
        key = _dedup_key(m["title"], m["author"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(m)
    return uniq[:limit]


def _parse_series(md: dict) -> tuple:
    """Pull (series_name, sequence) from ABS metadata. ABS exposes either a
    `series` list [{name, sequence}] or a `seriesName` string like
    'The Stormlight Archive #3' (first series only — good enough for tracking)."""
    srs = md.get("series")
    if isinstance(srs, list) and srs:
        s0 = srs[0] or {}
        name, seq = (s0.get("name") or "").strip(), s0.get("sequence")
        try:
            seq = float(seq) if seq not in (None, "") else None
        except (ValueError, TypeError):
            seq = None
        if name:
            return name, seq
    name = (md.get("seriesName") or "").split(",")[0].strip()   # "Name #3" or "Name #3, Other #1"
    if not name:
        return "", None
    m = re.search(r"#\s*([\d.]+)\s*$", name)
    seq = float(m.group(1)) if m else None
    name = re.sub(r"\s*#\s*[\d.]+\s*$", "", name).strip()
    return name, seq


def item_meta(it: dict) -> dict:
    md = ((it.get("media") or {}).get("metadata") or {})
    series, seq = _parse_series(md)
    return {"item_id": it.get("id", ""), "title": md.get("title") or "",
            "author": md.get("authorName") or "", "asin": md.get("asin") or "",
            "series": series, "series_seq": seq, "narrator": md.get("narratorName") or ""}


def item_detail(item_id: str) -> dict:
    """Full metadata for one item (used to resolve ASIN/series of a seed)."""
    try:
        return item_meta(_user_get(admin_token(), f"/api/items/{item_id}"))
    except Exception:
        return {"item_id": item_id, "title": "", "author": "", "asin": ""}


def set_finished(token: str, item_id: str, finished: bool = True) -> bool:
    """Mark a library item finished for this user (the 'mark as read' op)."""
    try:
        r = requests.patch(f"{abs_url()}/api/me/progress/{item_id}",
                          json={"isFinished": finished},
                          headers={"Authorization": f"Bearer {token}"}, timeout=20)
        return r.ok
    except Exception as e:
        log.warning("set_finished failed for %s: %s", item_id, e)
        return False


def scan(library_id: str):
    try:
        requests.post(f"{abs_url()}/api/libraries/{library_id}/scan",
                      headers=_admin_headers(), timeout=30)
    except Exception as e:
        log.warning("scan failed for %s: %s", library_id, e)
