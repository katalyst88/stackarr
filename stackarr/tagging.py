"""Book attribute tags — mood / pace / genre / content-warning — derived
deterministically (no AI) from free metadata: Open Library subjects, Google
Books categories, and Audible category ladders. Subjects are mapped to a small
curated vocabulary of moods/paces/warnings via keyword rules, and cached in the
book_tags table. Powers mood lanes, the mood filter, Insights' taste profile,
content warnings, and browse-by-mood."""
from __future__ import annotations

import logging
import re

from . import db, ebookmeta

log = logging.getLogger("stackarr.tagging")

# subject keyword (lowercased substring) -> mood label
MOOD_MAP = {
    "humor": "funny", "humour": "funny", "comic": "funny", "satire": "funny", "wit": "funny",
    "dark": "dark", "grimdark": "dark", "horror": "dark", "gothic": "dark", "macabre": "dark",
    "romance": "romantic", "love story": "romantic", "romantic": "romantic",
    "thriller": "tense", "suspense": "tense", "psychological": "tense",
    "adventure": "adventurous", "action": "adventurous", "quest": "adventurous",
    "literary": "reflective", "literature": "reflective", "philosoph": "thought-provoking",
    "epic": "epic", "high fantasy": "epic", "saga": "epic",
    "cozy": "cozy", "feel-good": "cozy", "heartwarming": "cozy", "wholesome": "cozy",
    "grief": "emotional", "loss": "emotional", "tragic": "emotional", "emotional": "emotional",
    "whimsical": "whimsical", "fairy tale": "whimsical", "fairy tales": "whimsical",
    "mystery": "mysterious", "detective": "mysterious", "noir": "mysterious",
    "dystop": "bleak", "post-apocalyptic": "bleak", "war": "intense",
    "coming of age": "tender", "young adult": "tender",
}
# subject keyword -> pace label (medium left untagged)
PACE_FAST = ("thriller", "action", "adventure", "page-turner", "fast-paced", "spy", "heist")
PACE_SLOW = ("literary", "epic", "saga", "slow", "meditative", "memoir", "essays", "philosoph")
# subject keyword -> content warning label
WARNING_MAP = {
    "sexual abuse": "Sexual assault", "rape": "Sexual assault", "sexual assault": "Sexual assault",
    "suicide": "Suicide", "self-harm": "Self-harm",
    "child abuse": "Child abuse", "abuse": "Abuse",
    "violence": "Violence", "torture": "Violence",
    "addiction": "Addiction", "drug": "Substance use", "alcoholism": "Substance use",
    "racism": "Racism", "slavery": "Racism",
    "death": "Death/grief", "grief": "Death/grief", "genocide": "Death/grief",
    "eating disorder": "Eating disorder",
}
GENRE_STOP = ("fiction", "general", "ebook", "audiobook", "english", "fiction in english",
              "accessible book", "protected daisy", "large type books")


def _clean_genre(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", s or "").strip()
    return s


def derive(subjects: list[str]) -> dict:
    """Map a raw subject list to {genre, mood, pace, warning} label lists."""
    subs = [(_clean_genre(s)) for s in (subjects or []) if s]
    low = [s.lower() for s in subs]
    moods, paces, warns, genres = set(), set(), set(), []
    for raw, s in zip(subs, low):
        for kw, mood in MOOD_MAP.items():
            if kw in s:
                moods.add(mood)
        if any(kw in s for kw in PACE_FAST):
            paces.add("fast-paced")
        if any(kw in s for kw in PACE_SLOW):
            paces.add("slow-paced")
        for kw, w in WARNING_MAP.items():
            if kw in s:
                warns.add(w)
        # keep short, human genre-ish subjects (drop noise + overly long ones)
        if (2 <= len(raw) <= 28 and s not in GENRE_STOP
                and not any(st in s for st in GENRE_STOP) and raw[:1].isalpha()):
            if raw not in genres:
                genres.append(raw)
    # pace is mutually-ish exclusive; if both, prefer the stronger signal (fast)
    if "fast-paced" in paces and "slow-paced" in paces:
        paces = {"fast-paced"}
    return {"genre": genres[:6], "mood": sorted(moods)[:5],
            "pace": sorted(paces), "warning": sorted(warns)}


def _subjects(title: str, author: str, existing_categories: list[str] | None = None) -> list[str]:
    subs = list(existing_categories or [])
    try:
        docs = ebookmeta._ol_get({"q": f"{title} {author}".strip(),
                                  "fields": "subject", "limit": 1})
        if docs:
            subs += (docs[0].get("subject") or [])[:30]
    except Exception as e:
        log.debug("tag subject fetch failed: %s", e)
    return subs


def fetch_for(rkey: str, title: str, author: str, categories: list[str] | None = None,
              force: bool = False) -> dict:
    """Ensure tags exist for a book; fetch + derive + cache if missing. Returns
    the stored {genre, mood, pace, warning} dict."""
    if not rkey or not title:
        return {}
    if not force and db.has_tags(rkey):
        return db.tags_for(rkey)
    derived = derive(_subjects(title, author, categories))
    rows = [(t, kind) for kind in ("genre", "mood", "pace", "warning") for t in derived.get(kind, [])]
    if rows:
        db.set_tags(rkey, rows)
    return derived


# the full mood vocabulary, for the browse/filter UI
ALL_MOODS = sorted(set(MOOD_MAP.values()))
