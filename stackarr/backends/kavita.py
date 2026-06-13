"""Kavita as an ebook source backend. Connected (not logged-into) via a Kavita
API key — Stackarr exchanges it for a short-lived JWT through Kavita's plugin
auth endpoint. Kavita exposes reliable per-user reading progress, so it's the
preferred ebook "what have I finished" signal.

Note on identity: ABS owns login. Kavita is connected with one API key, so its
reading progress reflects that Kavita account (fine for a household/single-user
install — JD's case). Per-user Kavita mapping isn't attempted here."""
from __future__ import annotations

import datetime
import logging
import time

import requests

from .. import config, db
from .base import Backend

log = logging.getLogger("stackarr.kavita")


def _url() -> str:
    return db.setting("kavita_url", config.KAVITA_URL).rstrip("/")


def _key() -> str:
    return db.setting("kavita_api_key", config.KAVITA_API_KEY)


def _parse_dt(s: str) -> int:
    """Kavita ISO timestamp -> epoch ms. Kavita's 'never' is year 0001."""
    if not s or s.startswith("0001"):
        return 0
    try:
        return int(datetime.datetime.fromisoformat(s[:26]).timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _clean_series_name(name: str) -> str:
    """E-Books imported as loose files give messy series names like
    'A Crown of Swords (The Wheel of Time' — drop a dangling '(' fragment."""
    name = (name or "").strip()
    if name.count("(") > name.count(")"):
        name = name.rsplit("(", 1)[0].strip()
    return name


class KavitaBackend(Backend):
    id = "kavita"
    label = "Kavita"
    media_format = "ebook"
    is_login = False
    supports_progress = True

    _jwt = ""
    _jwt_exp = 0.0

    # --- auth -------------------------------------------------------------
    def _token(self) -> str:
        if self._jwt and time.time() < self._jwt_exp:
            return self._jwt
        r = requests.post(f"{_url()}/api/Plugin/authenticate",
                          params={"apiKey": _key(), "pluginName": "Stackarr"}, timeout=20)
        r.raise_for_status()
        self._jwt = r.json().get("token", "")
        self._jwt_exp = time.time() + 9 * 60          # JWT lives ~10 min; refresh early
        return self._jwt

    def _get(self, path: str, **kw):
        h = {"Authorization": f"Bearer {self._token()}"}
        return requests.get(f"{_url()}{path}", headers=h, timeout=30, **kw)

    def _post(self, path: str, **kw):
        h = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        return requests.post(f"{_url()}{path}", headers=h, timeout=30, **kw)

    # --- connection -------------------------------------------------------
    def enabled(self) -> bool:
        return bool(_url() and _key())

    def test(self) -> dict:
        try:
            r = self._get("/api/library/libraries")
            r.raise_for_status()
            n = len(r.json())
            return {"ok": True, "detail": f"Connected — {n} librar{'y' if n == 1 else 'ies'}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # --- data -------------------------------------------------------------
    def _all_series(self) -> list[dict]:
        try:
            r = self._post("/api/series/all-v2", params={"PageNumber": 1, "PageSize": 2000}, json={})
            r.raise_for_status()
            return r.json() or []
        except Exception as e:
            log.warning("kavita series list failed: %s", e)
            return []

    def library_items(self) -> list[dict]:
        out = []
        for s in self._all_series():
            sid = s.get("id")
            if sid is None:
                continue
            name = _clean_series_name(s.get("name") or s.get("originalName") or "")
            if not name:
                continue
            out.append(self._tag({
                "item_id": f"kavita:{sid}", "library_id": str(s.get("libraryId", "")),
                "title": name, "author": "", "asin": "",
                "series": "", "series_seq": None, "narrator": "",
            }))
        return out

    def reading_history(self, user: dict) -> list[dict]:
        """Series with reading progress, recent first. Finished = read to the
        last page. (Uses the connected Kavita account, see module docstring.)"""
        out = []
        for s in self._all_series():
            read = s.get("pagesRead") or 0
            if read <= 0:
                continue
            pages = s.get("pages") or 0
            out.append({
                "item_id": f"kavita:{s.get('id')}",
                "finished": bool(pages and read >= pages),
                "progress": (read / pages) if pages else 0.0,
                "last_update": _parse_dt(s.get("latestReadDate") or s.get("lastChapterAddedUtc") or ""),
            })
        out.sort(key=lambda x: x["last_update"], reverse=True)
        return out
