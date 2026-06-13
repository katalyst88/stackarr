"""Calibre-Web as an ebook source backend. Connected via its OPDS feed (HTTP
basic auth) — Calibre-Web has no real API. It serves the library and a binary
read/unread flag (/opds/readbooks) for the configured account, but NOT true
reading *progress*, and only for that one account. So `supports_progress` is
False and the Settings UI warns that Stackarr can't see per-user progress here
— Kavita/ABS remain the reliable "what have I finished" source."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

from .. import config, db
from .base import Backend

log = logging.getLogger("stackarr.calibreweb")

NS = {"a": "http://www.w3.org/2005/Atom"}


def _url() -> str:
    return db.setting("calibreweb_url", config.CALIBREWEB_URL).rstrip("/")


def _auth() -> tuple[str, str]:
    return (db.setting("calibreweb_user", config.CALIBREWEB_USER),
            db.setting("calibreweb_pass", config.CALIBREWEB_PASS))


class CalibreWebBackend(Backend):
    id = "calibreweb"
    label = "Calibre-Web"
    media_format = "ebook"
    is_login = False
    supports_progress = False           # only a binary read flag, one account -> warn in UI

    # --- connection -------------------------------------------------------
    def enabled(self) -> bool:
        u, p = _auth()
        return bool(_url() and u and p)

    def test(self) -> dict:
        try:
            r = requests.get(f"{_url()}/opds", auth=_auth(), timeout=20)
            if r.status_code == 401:
                return {"ok": False, "detail": "Wrong username or password"}
            r.raise_for_status()
            return {"ok": True, "detail": "Connected — reading progress is limited (read/unread only)"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # --- OPDS helpers -----------------------------------------------------
    def _feed_entries(self, start_path: str, max_pages: int = 60) -> list[dict]:
        """Crawl an OPDS acquisition feed, following rel=next. Returns
        [{item_id, title, author}]."""
        out, path, pages = [], start_path, 0
        while path and pages < max_pages:
            try:
                r = requests.get(f"{_url()}{path}", auth=_auth(), timeout=30)
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception as e:
                log.warning("calibre-web feed %s failed: %s", path, e)
                break
            for e in root.findall("a:entry", NS):
                eid = (e.findtext("a:id", default="", namespaces=NS) or "").strip()
                title = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
                a = e.find("a:author", NS)
                author = (a.findtext("a:name", default="", namespaces=NS) or "").strip() if a is not None else ""
                if eid and title:
                    out.append({"item_id": "calibreweb:" + eid, "title": title, "author": author})
            nxt = [l.get("href") for l in root.findall("a:link", NS) if l.get("rel") == "next"]
            path = nxt[0] if nxt else None
            pages += 1
        return out

    # --- data -------------------------------------------------------------
    def library_items(self) -> list[dict]:
        items = self._feed_entries("/opds/books/letter/00")     # the "All" feed
        return [self._tag({
            "item_id": it["item_id"], "library_id": "calibreweb",
            "title": it["title"], "author": it["author"], "asin": "",
            "series": "", "series_seq": None, "narrator": "",
        }) for it in items]

    def reading_history(self, user: dict) -> list[dict]:
        """Binary read flag from /opds/readbooks (configured account only).
        Every entry counts as finished; there is no progress %."""
        out = []
        for it in self._feed_entries("/opds/readbooks"):
            out.append({"item_id": it["item_id"], "finished": True,
                        "progress": 1.0, "last_update": 0})
        return out
