"""Format mode — which media formats this Stackarr install surfaces.

Source libraries are connected and maintained by the **admin** (server-wide), and
the admin chooses what the install offers: "audiobook" (default), "ebook", or
"both". Every user sees the same offered formats and just requests books — so
availability is install-wide, not per-user. A single-format install NEVER renders
the other format's UI (no badges, filters or picker); "both" unlocks the
multi-format chrome.

The public helpers accept an optional `user` for forward-compatibility (a future
admin-set per-user format permission could narrow it), but today it's ignored."""
from __future__ import annotations

from . import config, db

VALID = ("audiobook", "ebook", "both")
LABELS = {"audiobook": "Audiobooks", "ebook": "eBooks"}
NOUN = {"audiobook": "audiobook", "ebook": "ebook"}     # lower-case singular


def mode() -> str:
    m = (db.setting("formats", config.FORMATS) or "audiobook").lower().strip()
    return m if m in VALID else "audiobook"


def available(user: dict | None = None) -> list[str]:
    """Concrete formats in play (expands 'both'). Install-wide for every user."""
    return ["audiobook", "ebook"] if mode() == "both" else [mode()]


def active() -> list[str]:
    return available()


def multi(user: dict | None = None) -> bool:
    return len(available(user)) > 1


def show(fmt: str, user: dict | None = None) -> bool:
    """Should UI/recommendations for this format be surfaced at all?"""
    return fmt in available(user)


def primary(user: dict | None = None) -> str:
    a = available(user)
    return "audiobook" if "audiobook" in a else a[0]


def flags(user: dict | None = None) -> dict:
    """Template globals — injected on every render via the context processor."""
    a = available(user)
    return {
        "format_mode": mode(),
        "multi_format": len(a) > 1,
        "show_audio": "audiobook" in a,
        "show_ebook": "ebook" in a,
        "active_formats": a,
        "format_labels": LABELS,
    }
