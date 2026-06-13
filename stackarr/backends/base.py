"""The pluggable library-source interface.

A *backend* is a place Stackarr reads books and reading activity from. Today
that's Audiobookshelf (audiobooks) and, optionally, Kavita / Calibre-Web
(ebooks). Each backend exposes the same small surface so the scheduler and
recommender can aggregate across whatever is connected without caring which
service a book actually lives in.

Two roles, deliberately separate:
  * **login backend** — supplies identity. Only Audiobookshelf does this; users
    sign in with their ABS credentials and that stays the single source of
    truth for *who* a user is (see backends/__init__.py LOGIN).
  * **source backend** — supplies a library snapshot + per-user reading
    history (the recommendation seed). ABS is both; Kavita/Calibre-Web are
    sources only.

Normalised book dict every source returns (the contract the rest of the app
relies on)::

    {
      "item_id":   str,   # unique within Stackarr; non-ABS sources are
                          #   namespaced "<source>:<raw>" to avoid collisions
      "library_id":str,
      "title":     str,
      "author":    str,
      "asin":      str,   # usually "" for ebooks
      "series":    str,
      "series_seq":float | None,
      "narrator":  str,   # "" for ebooks
      "format":    "audiobook" | "ebook",
      "source":    str,   # backend id
    }

Reading-history dict (the seed)::

    {"item_id": str, "finished": bool, "progress": float, "last_update": int}
"""
from __future__ import annotations

import abc


class Backend(abc.ABC):
    # --- identity / capabilities (class-level metadata) --------------------
    id: str = ""                    # stable short id: "abs", "kavita", "calibreweb"
    label: str = ""                 # human name shown in Settings
    media_format: str = "audiobook"  # "audiobook" | "ebook"
    is_login: bool = False          # supplies identity (ABS only)
    # Whether per-user reading progress is reliable. Calibre-Web's is weak, so
    # the UI warns when it's the only source of "what you've read".
    supports_progress: bool = True

    # --- connection -------------------------------------------------------
    @abc.abstractmethod
    def enabled(self) -> bool:
        """True when this backend is configured/connected and should be used."""

    @abc.abstractmethod
    def test(self) -> dict:
        """Probe the connection. Returns {"ok": bool, "detail": str}."""

    # --- data -------------------------------------------------------------
    @abc.abstractmethod
    def library_items(self) -> list[dict]:
        """Every owned book as a normalised dict (see module docstring)."""

    def reading_history(self, user: dict) -> list[dict]:
        """Books this user has finished or made progress on, recent first.
        Default: none (a source with no per-user signal). ABS/Kavita override."""
        return []

    # --- optional niceties (sane defaults) --------------------------------
    def listening_stats(self, user: dict) -> dict:
        return {"total_seconds": 0, "days_listened": 0, "items_count": 0}

    def cover_url(self, item_id: str) -> str | None:
        """An app-relative URL that serves this item's cover, or None."""
        return None

    # convenience so callers can stamp normalised dicts uniformly
    def _tag(self, item: dict) -> dict:
        item.setdefault("format", self.media_format)
        item.setdefault("source", self.id)
        return item
