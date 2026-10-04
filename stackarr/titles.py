"""Title matching for "is this the same book?" checks against the library.

Library titles (Audiobookshelf, Kavita, …) and catalogue titles (Audible,
Google Books) describe the same work differently: "The Captain: The Last
Horizon, Book 1" in the library is "The Captain" in the catalogue, and the
library copy usually has no ASIN to fall back on. Exact comparison misses them,
so an owned book is offered for request again. These helpers strip edition and
series decoration and compare titles in both directions, refusing to guess when
a stem could be more than one work ("Mistborn: Secret History" is not
"Mistborn: The Final Empire")."""
import re

_BRACKETS = re.compile(r"\s*[(\[][^)\]]*[)\]]")             # "(Unabridged)", "[Dramatized]"
_NUM = r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)"
_SERIES_SUFFIX = re.compile(                                 # ", Book 1" / ", Volume 1" / " #3"
    rf"(?:\s*[,:]|\s+-)?\s*(?:\b(?:book|volume|vol\.?|part)\s*{_NUM}|#\s*\d+(?:\.\d+)?)\s*$", re.I)
_SUBTITLE = re.compile(r"\s*(?::|\s-\s|\s—\s)\s*")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def clean(title: str) -> str:
    """The title without edition/series decoration: bracketed notes and a
    trailing ", Book N" / ", Volume N" / " #N". Never returns an empty title."""
    t = _BRACKETS.sub("", title or "").strip()
    prev = None
    while t != prev:
        prev, t = t, _SERIES_SUFFIX.sub("", t).strip()
    return t if norm(t) else (title or "").strip()


def parts(title: str) -> tuple[str, str, str]:
    """(full, base, tail) normalized keys of the cleaned title: the whole thing,
    the part before a ':' / ' - ' subtitle separator, and the part after it
    ("" when there's no subtitle, in which case base == full)."""
    c = clean(title)
    p = _SUBTITLE.split(c, 1)
    full = norm(c)
    base = norm(p[0]) or full
    tail = norm(p[1]) if len(p) > 1 else ""
    return full, base, tail


def matches(rows: list[dict], title: str, asin: str = "") -> list[dict]:
    """The rows (each with `title`, optionally `asin`) that are the same work as
    `title` — all of them, e.g. one per format — or [] if none or ambiguous.

    Tiers, first hit wins: ASIN; same cleaned title; one title is the other's
    part after the separator ("Star Wars: Thrawn" / "Thrawn"); one title is
    the other minus its subtitle ("The Captain" / "The Captain: The Last
    Horizon"). The last two only count when they resolve to a single work, and
    two titles that both carry a different subtitle never match. Callers
    filter `rows` by author first."""
    asin = (asin or "").strip().upper()
    if asin:
        hit = [r for r in rows if (r.get("asin") or "").upper() == asin]
        if hit:
            return hit
    full, base, tail = parts(title)
    if not full:
        return []
    keyed = [(r, parts(r.get("title"))) for r in rows]
    hit = [r for r, (f, _, _) in keyed if f == full]
    if hit:
        return hit
    tiers = (
        lambda f, b, t: (tail and f == tail) or (t and t == full),
        lambda f, b, t: (tail and f == base) or (t and b == full),
    )
    for tier in tiers:
        hit = [(r, f) for r, (f, b, t) in keyed if tier(f, b, t)]
        if hit and len({f for _, f in hit}) == 1:
            return [r for r, _ in hit]
    return []


def _author_key(author: str) -> str:
    return norm((author or "").split(",")[0])


class KnownWorks:
    """A set of (title, author) works with the same subtitle tolerance as
    `matches`, for cheap bulk membership tests (the recommenders' "already
    owned / requested / suggested" filter): `(title, author) in known`."""

    def __init__(self):
        self._full, self._base = set(), set()

    def add(self, title: str, author: str) -> None:
        full, base, tail = parts(title)
        a = _author_key(author)
        self._full.add((full, a))
        if tail:
            self._base.add((base, a))

    def __contains__(self, item) -> bool:
        title, author = item
        full, base, tail = parts(title)
        a = _author_key(author)
        if (full, a) in self._full:
            return True
        if tail:                               # "Beware of Chicken: A Xianxia…" vs "Beware of Chicken"
            return (base, a) in self._full
        return (full, a) in self._base         # "The Captain" vs "The Captain: The Last Horizon"
