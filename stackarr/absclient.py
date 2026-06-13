"""Audiobookshelf client. Handles multi-user login (users sign in with their
own ABS credentials), reads each user's listening history (the recommendation
seed), and lists library contents for dedupe + deletion detection."""
import logging

import requests

from . import config

log = logging.getLogger("stackarr.abs")


def _admin_headers():
    return {"Authorization": f"Bearer {config.ABS_ADMIN_TOKEN}"}


def login(username: str, password: str) -> dict | None:
    """Authenticate against Audiobookshelf. Returns {id, username, token,
    isAdmin} on success, None on bad credentials."""
    try:
        r = requests.post(f"{config.ABS_URL}/login",
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
    r = requests.get(f"{config.ABS_URL}{path}", params=params or {},
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


def libraries() -> list[dict]:
    libs = _user_get(config.ABS_ADMIN_TOKEN, "/api/libraries").get("libraries", [])
    libs = [l for l in libs if l.get("mediaType") == "book"]
    if config.ABS_LIBRARY_IDS:
        libs = [l for l in libs if l["id"] in config.ABS_LIBRARY_IDS]
    return libs


def items(library_id: str) -> list[dict]:
    out, page = [], 0
    while True:
        d = _user_get(config.ABS_ADMIN_TOKEN, f"/api/libraries/{library_id}/items",
                      {"limit": 200, "page": page})
        batch = d.get("results", [])
        out.extend(batch)
        page += 1
        if not batch or len(out) >= d.get("total", 0):
            return out


def item_meta(it: dict) -> dict:
    md = ((it.get("media") or {}).get("metadata") or {})
    return {"item_id": it.get("id", ""), "title": md.get("title") or "",
            "author": md.get("authorName") or "", "asin": md.get("asin") or ""}


def item_detail(item_id: str) -> dict:
    """Full metadata for one item (used to resolve ASIN/series of a seed)."""
    try:
        return item_meta(_user_get(config.ABS_ADMIN_TOKEN, f"/api/items/{item_id}"))
    except Exception:
        return {"item_id": item_id, "title": "", "author": "", "asin": ""}


def set_finished(token: str, item_id: str, finished: bool = True) -> bool:
    """Mark a library item finished for this user (the 'mark as read' op)."""
    try:
        r = requests.patch(f"{config.ABS_URL}/api/me/progress/{item_id}",
                          json={"isFinished": finished},
                          headers={"Authorization": f"Bearer {token}"}, timeout=20)
        return r.ok
    except Exception as e:
        log.warning("set_finished failed for %s: %s", item_id, e)
        return False


def scan(library_id: str):
    try:
        requests.post(f"{config.ABS_URL}/api/libraries/{library_id}/scan",
                      headers=_admin_headers(), timeout=30)
    except Exception as e:
        log.warning("scan failed for %s: %s", library_id, e)
