"""Backend registry. One place that knows every library source Stackarr can
read from, which one owns login, and which are currently connected.

Phase 1 ships only Audiobookshelf. Kavita and Calibre-Web register here in
Phase 4; because everything goes through `sources()`, the scheduler and
recommender pick them up automatically once connected — no call-site changes."""
from __future__ import annotations

from .abs import ABSBackend
from .base import Backend

# The login backend (identity). Audiobookshelf only — users always sign in
# with their ABS credentials; other sources are connected, not logged-into.
LOGIN: Backend = ABSBackend()

# Every backend Stackarr knows about, login first. Kavita/Calibre-Web append
# in Phase 4. Order here is the order lanes/aggregation prefer.
ALL: list[Backend] = [LOGIN]


def register(backend: Backend) -> None:
    """Add a source backend (idempotent by id). Called from Phase 4 modules."""
    if not any(b.id == backend.id for b in ALL):
        ALL.append(backend)


def by_id(bid: str) -> Backend | None:
    return next((b for b in ALL if b.id == bid), None)


def sources(media_format: str | None = None) -> list[Backend]:
    """Connected source backends, optionally filtered to one media format.
    `media_format` of None / "both" means no filter."""
    out = []
    for b in ALL:
        try:
            if not b.enabled():
                continue
        except Exception:
            continue
        if media_format and media_format != "both" and b.media_format != media_format:
            continue
        out.append(b)
    return out
