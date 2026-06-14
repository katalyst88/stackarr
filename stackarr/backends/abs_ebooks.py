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


def _has_audio(it: dict) -> bool:
    media = it.get("media") or {}
    return bool((media.get("numAudioFiles") or 0) > 0 or media.get("audioFiles")
                or media.get("tracks") or (media.get("duration") or 0) > 0)


class ABSEbooksBackend(Backend):
    id = "abs_ebooks"
    label = "Audiobookshelf eBooks"
    media_format = "ebook"
    is_login = False
    supports_progress = True
    can_write_progress = True

    def enabled(self) -> bool:
        return _enabled() and bool(absclient.abs_url() and absclient.admin_token())

    def test(self) -> dict:
        try:
            n = sum(1 for lib in absclient.libraries()
                    for it in absclient.items(lib["id"]) if _has_ebook(it))
            return {"ok": True, "detail": f"Connected — {n} ebook(s) in your Audiobookshelf libraries"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _ebook_ids(self, raise_on_error: bool = False) -> dict:
        ids = {}
        for lib in absclient.libraries():
            try:
                for it in absclient.items(lib["id"]):
                    # ebook-ONLY items: a hybrid (audio+epub) item is owned by the
                    # audiobook source, so skip it here to avoid an item_id clash
                    # that would mislabel the audiobook as an ebook.
                    if _has_ebook(it) and not _has_audio(it):
                        m = absclient.item_meta(it)
                        if m.get("item_id"):
                            m["library_id"] = lib["id"]      # item_meta doesn't set it
                            ids[m["item_id"]] = m
            except Exception:
                # for the library snapshot, propagate so refresh_library skips this
                # source instead of treating a partial crawl as "these books are gone"
                # and -5-poisoning every user (matches abs.library_items hardening).
                if raise_on_error:
                    raise
                continue
        return ids

    def library_items(self) -> list[dict]:
        out = []
        for m in self._ebook_ids(raise_on_error=True).values():
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

    def mark_read(self, user: dict, item_id: str, finished: bool = True) -> bool:
        # ABS ebooks use raw ABS library-item ids (no namespace prefix); skip any
        # id that belongs to another ebook source.
        if not item_id or ":" in item_id:
            return False
        token = (user or {}).get("abs_token")
        if not token:
            return False
        return absclient.set_finished(token, item_id, finished)
