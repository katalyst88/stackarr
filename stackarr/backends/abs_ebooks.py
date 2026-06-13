"""Audiobookshelf eBooks as a source. ABS book libraries can hold epub/pdf
ebooks alongside audiobooks; this surfaces the *ebook* items (those with an
ebook file) as an ebook source — no extra credentials, since ABS is already
connected. Per-user reading progress comes from the same ABS token.

Off unless ABS_EBOOKS / the Settings toggle is on, since most ABS installs are
audiobook-only and we don't want to imply ebooks that aren't there."""
from __future__ import annotations

from .. import absclient, config, db
from .base import Backend


def _enabled() -> bool:
    return db.get_meta("abs_ebooks", "1" if config.ABS_EBOOKS else "0") == "1"


def _has_ebook(it: dict) -> bool:
    media = it.get("media") or {}
    return bool(media.get("ebookFile") or media.get("ebookFormat")
                or (media.get("numEbooks") or 0) > 0)


class ABSEbooksBackend(Backend):
    id = "abs_ebooks"
    label = "Audiobookshelf eBooks"
    media_format = "ebook"
    is_login = False
    supports_progress = True

    def enabled(self) -> bool:
        return _enabled() and bool(absclient.abs_url() and absclient.admin_token())

    def test(self) -> dict:
        try:
            n = sum(1 for lib in absclient.libraries()
                    for it in absclient.items(lib["id"]) if _has_ebook(it))
            return {"ok": True, "detail": f"Connected — {n} ebook(s) in your Audiobookshelf libraries"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _ebook_ids(self) -> dict:
        ids = {}
        for lib in absclient.libraries():
            try:
                for it in absclient.items(lib["id"]):
                    if _has_ebook(it):
                        m = absclient.item_meta(it)
                        if m.get("item_id"):
                            ids[m["item_id"]] = m
            except Exception:
                continue
        return ids

    def library_items(self) -> list[dict]:
        out = []
        for m in self._ebook_ids().values():
            m["library_id"] = m.get("library_id", "")
            m["narrator"] = ""                  # ebooks have no narrator
            out.append(self._tag(m))
        return out

    def reading_history(self, user: dict) -> list[dict]:
        token = (user or {}).get("abs_token")
        if not token:
            return []
        ebook_ids = set(self._ebook_ids().keys())
        return [h for h in absclient.listening_history(token) if h["item_id"] in ebook_ids]
