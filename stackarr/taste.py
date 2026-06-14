"""Deterministic taste-profile helpers — NO AI. Turns a user's explicit mood
preferences (vibe picker / feedback / DNF-propagation) plus the moods derived
from the books they've read into a mood/pace affinity map, and scores candidate
books by overlap. Also provides the serendipity bonus (rewards well-rated but
lesser-known books — the anti-'too obvious' lever) and the adventurousness dial
(Comfort ↔ Discovery)."""
from __future__ import annotations

from . import config, db, tagging


def mood_signals(user_id: int, fmt: str | None = None) -> dict:
    """Explicit mood affinity: positive from the vibe picker / 'more like this',
    negative from DNF/pass propagation. {mood_lower: weight}. Moods are treated as
    CROSS-FORMAT (a "dark" preference applies to audiobooks and ebooks alike), so
    `fmt` is accepted for API symmetry but moods aren't format-isolated — unlike
    ratings/author signals, which are. (The signals table is value-unique, so a
    per-format mood row can't be isolated without a schema change anyway.)"""
    clause, args = "", [user_id]
    if fmt in ("audiobook", "ebook"):
        clause = " AND (format=? OR format IS NULL OR format='')"
        args.append(fmt)
    out: dict[str, float] = {}
    with db.conn() as c:
        for r in c.execute(f"SELECT value, weight FROM signals WHERE user_id=? AND kind='mood'{clause}", args):
            k = (r["value"] or "").lower()
            out[k] = out.get(k, 0.0) + r["weight"]
    return out


def adventurousness(user_id: int) -> int:
    try:
        return max(0, min(100, int(db.get_meta(f"adventurousness_{user_id}", str(config.ADVENTUROUSNESS)))))
    except (ValueError, TypeError):
        return config.ADVENTUROUSNESS


def adv_multipliers(user_id: int) -> tuple[float, float]:
    """(familiar_mult, discover_mult) from the adventurousness dial. 50 = (1,1);
    higher favours discovery/new-author lanes, lower favours author/series."""
    adv = adventurousness(user_id)
    shift = (adv - 50) / 100.0            # -0.5 .. +0.5
    return round(1 - shift, 3), round(1 + shift, 3)


def candidate_moods(categories: list[str]) -> set:
    d = tagging.derive(categories or [])
    return set(d.get("mood", [])) | set(d.get("pace", []))


def mood_overlap(categories: list[str], mood_profile: dict) -> float:
    """Sum of profile weights for the moods this candidate carries (its own
    categories → derived moods). Positive when the book matches liked moods,
    negative when it matches disliked ones."""
    if not mood_profile:
        return 0.0
    return sum(mood_profile.get(m.lower(), 0.0) for m in candidate_moods(categories))


def serendipity(book: dict, adv: int) -> float:
    """Bonus for a well-rated but lesser-known book, scaled by adventurousness.
    Directly counters the 'recommendations are too obvious/popular' complaint."""
    nr = book.get("num_ratings", 0) or 0
    rating = book.get("rating") or 0
    if rating >= 4.3 and 0 < nr <= 3000:
        rarity = 1.0 - min(nr, 3000) / 3000.0
        return (adv / 50.0) * rarity * (rating - 4.0)
    return 0.0
