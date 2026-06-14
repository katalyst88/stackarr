"""Backend registry. One place that knows every library source Stackarr can
read from, which one owns login, and which are currently connected.

To ADD a new source backend (contributor guide):
  1. Create `backends/<name>.py` with a class subclassing `base.Backend`.
     Implement the abstract surface (`enabled()`, `test()`, `library_items()`)
     and override what applies (`reading_history()`, `cover_url()`,
     `verify_login()`/`can_login` to allow sign-in, `mark_read()`/
     `can_write_progress` for write-back, `list_users()`/`can_import_users` for
     user import). See `base.Backend` for the full documented contract, and
     `kavita.py` / `komga.py` as worked examples.
  2. Set `media_format` ("audiobook" | "ebook") and a unique `id`/`label`.
  3. Add an instance to `ALL` below. Everything else (scheduler, recommender,
     settings, login) discovers it through `sources()` — no call-site changes."""
from __future__ import annotations

from .abs import ABSBackend
from .abs_ebooks import ABSEbooksBackend
from .base import Backend
from .calibreweb import CalibreWebBackend
from .kavita import KavitaBackend
from .komga import KomgaBackend
from .opds import OPDSBackend

# The login backend (identity). Audiobookshelf only — users always sign in
# with their ABS credentials; other sources are connected, not logged-into.
LOGIN: Backend = ABSBackend()

# Every backend Stackarr knows about, login first, then ebook sources. They're
# all registered; `sources()` filters to the ones actually connected (enabled)
# and of an active format, so an unconfigured backend simply never appears.
ALL: list[Backend] = [LOGIN, KavitaBackend(), CalibreWebBackend(),
                      KomgaBackend(), OPDSBackend(), ABSEbooksBackend()]


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
