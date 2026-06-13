"""Generic OPDS 1.x ebook source. Works with any OPDS catalogue (Ubooquity,
Kavita's OPDS, Komga's OPDS, Calibre's content server, etc.) — point it at an
acquisition feed URL and it crawls the entries. OPDS has no standard read-
progress, so this is a library-only source (supports_progress=False)."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

from .. import config, db
from .base import Backend

log = logging.getLogger("stackarr.opds")
NS = {"a": "http://www.w3.org/2005/Atom"}


def _url() -> str:
    return db.setting("opds_url", config.OPDS_URL).rstrip("/")


def _auth():
    u = db.setting("opds_user", config.OPDS_USER)
    p = db.setting("opds_pass", config.OPDS_PASS)
    return (u, p) if u else None


class OPDSBackend(Backend):
    id = "opds"
    label = "OPDS catalogue"
    media_format = "ebook"
    is_login = False
    supports_progress = False

    def enabled(self) -> bool:
        return bool(_url())

    def test(self) -> dict:
        try:
            r = requests.get(_url(), auth=_auth(), timeout=20)
            if r.status_code == 401:
                return {"ok": False, "detail": "Auth required / wrong credentials"}
            r.raise_for_status()
            ET.fromstring(r.content)            # must be valid OPDS/Atom
            return {"ok": True, "detail": "Connected — library only (no reading progress over OPDS)"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def library_items(self) -> list[dict]:
        out, path, pages = [], _url(), 0
        seen_urls = set()
        while path and pages < 80:
            if path in seen_urls:
                break
            seen_urls.add(path)
            try:
                r = requests.get(path, auth=_auth(), timeout=30)
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception as e:
                log.warning("opds feed %s failed: %s", path, e)
                break
            for e in root.findall("a:entry", NS):
                eid = (e.findtext("a:id", default="", namespaces=NS) or "").strip()
                title = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
                a = e.find("a:author", NS)
                author = (a.findtext("a:name", default="", namespaces=NS) or "").strip() if a is not None else ""
                # only acquisition entries (real books) carry an acquisition link
                acq = any("acquisition" in (l.get("rel") or "") for l in e.findall("a:link", NS))
                if eid and title and acq:
                    out.append(self._tag({
                        "item_id": "opds:" + eid, "library_id": "opds",
                        "title": title, "author": author, "asin": "",
                        "series": "", "series_seq": None, "narrator": "",
                    }))
            nxt = [l.get("href") for l in root.findall("a:link", NS) if l.get("rel") == "next"]
            if nxt:
                href = nxt[0]
                if href.startswith("/"):          # resolve relative next links
                    from urllib.parse import urljoin
                    href = urljoin(_url(), href)
                path = href
            else:
                path = None
            pages += 1
        return out
