"""Format mode — which media formats this Stackarr install surfaces.

One of: "audiobook" (default), "ebook", or "both". The DB setting `formats`
overrides the STACKARR_FORMATS env default, like every other runtime setting.

Everything format-aware reads from here so the rule is enforced in one place:
a single-format install NEVER renders the other format's UI (no badges, no
filters, no picker), and the recommender only pulls from sources of an active
format. `both` unlocks the multi-format chrome."""
from __future__ import annotations

from . import config, db

VALID = ("audiobook", "ebook", "both")
LABELS = {"audiobook": "Audiobooks", "ebook": "eBooks"}
NOUN = {"audiobook": "audiobook", "ebook": "ebook"}     # lower-case singular


def mode() -> str:
    m = (db.setting("formats", config.FORMATS) or "audiobook").lower().strip()
    return m if m in VALID else "audiobook"


def active() -> list[str]:
    """Concrete formats in play (expands 'both' to both)."""
    m = mode()
    return ["audiobook", "ebook"] if m == "both" else [m]


def multi() -> bool:
    return mode() == "both"


def show(fmt: str) -> bool:
    """Should UI/recommendations for this format be surfaced at all?"""
    return fmt in active()


def primary() -> str:
    """The default/leading format (what a new pick is tagged when ambiguous)."""
    a = active()
    return "audiobook" if "audiobook" in a else a[0]


def flags() -> dict:
    """Template globals — injected on every render via the context processor."""
    a = active()
    return {
        "format_mode": mode(),
        "multi_format": len(a) > 1,
        "show_audio": "audiobook" in a,
        "show_ebook": "ebook" in a,
        "active_formats": a,
        "format_labels": LABELS,
    }
