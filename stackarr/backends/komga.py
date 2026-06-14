"""Komga as an ebook source backend (REST + HTTP basic auth). Komga exposes a
clean API with per-book read progress, so it's a reliable ebook source like
Kavita. Connected, not logged-into; ABS stays the identity."""
from __future__ import annotations

import logging

import requests

from .. import config, db
from .base import Backend

log = logging.getLogger("stackarr.komga")


def _url() -> str:
    return db.setting("komga_url", config.KOMGA_URL).rstrip("/")


def _auth() -> tuple[str, str]:
    return (db.setting("komga_user", config.KOMGA_USER),
            db.setting("komga_pass", config.KOMGA_PASS))


class KomgaBackend(Backend):
    id = "komga"
    label = "Komga"
    media_format = "ebook"
    is_login = False
    supports_progress = True
    can_write_progress = True
    can_login = True

    def enabled(self) -> bool:
        u, p = _auth()
        return bool(_url() and u and p)

    def verify_login(self, username: str, password: str) -> dict | None:
        for path in ("/api/v2/users/me", "/api/v1/users/me"):
            try:
                r = requests.get(f"{_url()}{path}", auth=(username, password), timeout=20)
            except Exception as e:
                log.warning("komga login failed for %s: %s", username, e)
                return None
            if r.status_code == 200:
                d = r.json() or {}
                roles = d.get("roles") or []
                return {"external_id": str(d.get("id") or username),
                        "username": d.get("email") or username,
                        "token": "", "is_admin": "ADMIN" in roles}
        return None

    def mark_read(self, user: dict, item_id: str, finished: bool = True) -> bool:
        if not (item_id or "").startswith("komga:"):
            return False
        bid = item_id.split(":", 1)[1]
        try:
            r = requests.patch(f"{_url()}/api/v1/books/{bid}/read-progress",
                               auth=_auth(), json={"completed": bool(finished)}, timeout=30)
            return r.status_code in (200, 204)
        except Exception as e:
            log.warning("komga mark_read failed: %s", e)
            return False

    def _get(self, path, **params):
        r = requests.get(f"{_url()}{path}", auth=_auth(), params=params or {}, timeout=30)
        r.raise_for_status()
        return r.json()

    def test(self) -> dict:
        try:
            libs = self._get("/api/v1/libraries")
            n = len(libs)
            return {"ok": True, "detail": f"Connected — {n} librar{'y' if n == 1 else 'ies'}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _books(self, raise_on_error: bool = False) -> list[dict]:
        out, page = [], 0
        while page < 60:                       # safety cap (~36k books)
            try:
                d = self._get("/api/v1/books", page=page, size=600, sort="metadata.title,asc")
            except Exception as e:
                log.warning("komga books failed (page %s): %s", page, e)
                if raise_on_error:
                    raise          # let refresh_library skip Komga, not delete it
                break
            out.extend(d.get("content", []))
            if d.get("last", True):
                break
            page += 1
        return out

    def library_items(self) -> list[dict]:
        items = []
        for b in self._books(raise_on_error=True):
            md = b.get("metadata") or {}
            title = md.get("title") or b.get("name") or ""
            if not title:
                continue
            authors = ", ".join(a.get("name", "") for a in (md.get("authors") or [])
                                if (a.get("role") in (None, "writer", "author")))
            items.append(self._tag({
                "item_id": f"komga:{b.get('id')}", "library_id": b.get("libraryId", ""),
                "title": title, "author": authors, "asin": "",
                "series": b.get("seriesTitle", ""), "series_seq": None, "narrator": "",
            }))
        return items

    def reading_history(self, user: dict) -> list[dict]:
        # multi-user: progress comes from one shared account that can't be
        # attributed to the requesting user — don't seed everyone from it.
        if db.user_count() > 1:
            return []
        import datetime

        def _ts(s):
            if not s:
                return 0
            try:
                return int(datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
            except (ValueError, TypeError):
                return 0
        out = []
        for b in self._books():
            rp = b.get("readProgress") or {}
            if not rp:
                continue
            completed = bool(rp.get("completed"))
            pages = b.get("media", {}).get("pagesCount") or 0
            read = rp.get("page") or 0
            if completed or read > 0:
                out.append({"item_id": f"komga:{b.get('id')}", "finished": completed,
                            "progress": (read / pages) if pages else (1.0 if completed else 0.0),
                            # real read time so recency ordering works (was hard-coded 0)
                            "last_update": _ts(rp.get("readDate") or rp.get("lastModified"))})
        return out
