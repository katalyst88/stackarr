"""Discover: deterministic genre-trending picks and a cold-start popular set.
No personalisation, no AI — recent, well-rated catalog entries in given
genres. Used for the Discover tab and as the new-user fallback."""
import logging

from . import audible, config

log = logging.getLogger("stackarr.discover")

DEFAULT_GENRES = ["Science Fiction & Fantasy", "Mystery, Thriller & Suspense",
                  "Literature & Fiction", "Biographies & Memoirs", "History"]


def genre_new(genres: list[str], num_per: int = 6) -> list[dict]:
    out, seen = [], set()
    for g in genres or DEFAULT_GENRES:
        for b in audible.search(g, num=num_per * 3):
            if not b.get("asin") or b["asin"] in seen:
                continue
            if (b.get("rating") or 0) < config.SUGGEST_RATING_FLOOR:
                continue
            if (b.get("language") or "english") != "english":
                continue
            seen.add(b["asin"])
            out.append(b)
    out.sort(key=lambda b: (b.get("rating") or 0, b.get("release_date") or ""), reverse=True)
    return out


def popular(num: int = 12) -> list[dict]:
    return genre_new(DEFAULT_GENRES, num_per=4)[:num]
