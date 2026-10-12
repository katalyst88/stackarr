"""Where a book stands for this household — the ONE place that decides it.

Every page used to work it out for itself: the book page and the cards read
the latest request row by ASIN ("Requested"), the Requests page patched in
Chaptarr's queue afterwards ("Downloading"), Up Next matched request titles
by a 30-character substring, the series strip had its own rules. So the same
book said "Requested" on one page and "Downloading" on the next.

`for_book()` / `for_books()` answer for a work, per media format and overall:

  available    In library      the library holds it in that format (or a
                               request for it flipped to available)
  downloading  Downloading     in Chaptarr's queue, with % and ETA
  importing    Importing       downloaded, Chaptarr is importing it
  stalled      Stalled         in the queue, no peers to download from
  waiting      Waiting         Chaptarr is holding the release (a delay
                               profile, a tracker check) — reason attached
  requested    Requested       handed to Chaptarr, searching
  not_found    Needs attention no usable release: the background release check
                               found none / only rejected ones, or the request
                               is STALE_DAYS old and never reached the queue
  needs_vip    Needs VIP       every release is VIP-only on the indexer
  unavailable  Needs attention every release was rejected (both say "Might need
                               Check upstream", not the reasons)
  pending      Needs approval  waiting for an admin
  failed       Failed          the handoff failed (retryable)
  denied       Not approved    an admin declined it
  upcoming     Upcoming        not released yet
  none         —               nothing yet

Overall: "In library" when any format is held (you can read it now — the
per-format detail says what else is on its way), else the most advanced
state of any format.

Chaptarr's queue is read at most once per QUEUE_TTL seconds, and only when a
book being shown has an open request — a page of cards with nothing on order
never calls it. Requests are household-wide for the active states (the
library is shared: a book someone else requested is on its way for you too);
failed / denied attempts count only for the person who made them."""
import threading
import time

from . import chaptarr, db, formats, titles, tz

QUEUE_TTL = 30
STALE_DAYS = 7
# Stackarr flags a stuck request; the why lives in Chaptarr (its search, its
# indexers, its rejections), so the flag points there instead of retelling it.
CHECK_UPSTREAM = "Check upstream"
MEDIA = ("audiobook", "ebook")

LABELS = {
    "available": ("In library", "available"),
    "downloading": ("Downloading", "downloading"),
    "importing": ("Importing", "downloading"),
    "stalled": ("Stalled", "stalled"),
    "waiting": ("Waiting", "waiting"),
    "requested": ("Requested", "handed"),
    "not_found": ("Needs attention", "stalled"),
    "unavailable": ("Needs attention", "failed"),
    "needs_vip": ("Needs VIP", "vip"),
    "pending": ("Needs approval", "pending_approval"),
    "failed": ("Failed", "failed"),
    "denied": ("Not approved", "denied"),
    "upcoming": ("Upcoming", "upcoming"),
    "none": ("", ""),
}
# most advanced first — the overall state of a book that isn't in the library
ORDER = ("downloading", "importing", "stalled", "waiting", "requested", "needs_vip", "not_found", "unavailable",
         "pending", "failed", "denied")
OPEN = ("queued", "handed", "approving")            # stored statuses Chaptarr is working on
FMT_NAME = {"audiobook": "Audiobook", "ebook": "eBook"}

_q_lock = threading.Lock()
_q_cache = {"at": 0.0, "v": None}


def live_queue() -> list[dict]:
    """chaptarr.queue(), cached for QUEUE_TTL seconds (a failure too, so a
    Chaptarr outage costs one timeout per TTL, not one per page)."""
    with _q_lock:
        if time.time() - _q_cache["at"] < QUEUE_TTL:
            return _q_cache["v"] or []
    v = chaptarr.queue() if chaptarr.configured() else []
    with _q_lock:
        _q_cache.update(at=time.time(), v=v)
    return v or []


def reset_cache():
    with _q_lock:
        _q_cache.update(at=0.0, v=None)


def _first(a: str) -> str:
    return (a or "").split(",")[0].strip().lower()


def _media_of(fmt: str) -> tuple:
    f = (fmt or "").lower()
    return MEDIA if f == "both" else (("ebook",) if f == "ebook" else ("audiobook",))


def _queue_hit(q: list[dict], title: str, author: str, asin: str, media: str) -> dict | None:
    a = _first(author)
    cands = [x for x in q if (x.get("media") or "audiobook") == media
             and (not a or not x.get("author") or a in x["author"].lower() or _first(x["author"]) in a)]
    hit = titles.matches(cands, title, asin)
    return hit[0] if hit else None


def _days_since(ts: str) -> float:
    then = tz.from_db(ts)
    return (tz.now() - then).total_seconds() / 86400 if then else 0.0


def _entry(state: str, detail: str = "", **extra) -> dict:
    label, cls = LABELS[state]
    return {"state": state, "label": label, "cls": cls, "detail": detail, **extra}


def _format_state(fmt, owned, rows, queue_fn, title, author, asin) -> dict:
    if fmt in owned:
        return _entry("available")
    mine = [r for r in rows if fmt in _media_of(r.get("format"))]
    if not mine:
        return _entry("none")
    r = max(mine, key=lambda x: x.get("id") or 0)          # the latest attempt is the truth
    st = r.get("status") or ""
    if st == "available":
        return _entry("available")
    if st == "pending_approval":
        return _entry("pending", "Waiting for an admin to approve")
    if st == "failed":
        return _entry("failed", r.get("detail") or "")
    if st == "denied":
        return _entry("denied", r.get("detail") or "")
    if st in OPEN:
        hit = _queue_hit(queue_fn(), title, author, asin, fmt)
        if hit:
            s = hit["state"]
            if s == "downloading":
                bits = [f"{hit['pct']}%" if hit.get("pct") is not None else "",
                        f"about {hit['eta']} left" if hit.get("eta") else ""]
                return _entry("downloading", " · ".join(b for b in bits if b), pct=hit.get("pct"), eta=hit.get("eta"))
            if s == "waiting":
                return _entry("waiting", "Held upstream")
            if s == "stalled":
                return _entry("stalled", "Stalled upstream")
            if s == "failed":
                return _entry("failed", "Failed upstream — you can retry")
            return _entry(s if s in LABELS else "downloading")
        # what the background release check (releasecheck.py) found, if it looked
        from . import releasecheck
        rc = releasecheck.result(r.get("id")) if r.get("id") else None
        if rc and rc.get("kind") == "vip":
            return _entry("needs_vip", releasecheck.VIP_EXPLAINER)
        if rc and rc.get("kind") == "vip_retry":
            return _entry("requested", "Searching again — VIP is on")
        if rc and rc.get("kind") == "rejected":
            return _entry("unavailable", CHECK_UPSTREAM)
        if rc and rc.get("kind") == "none":
            return _entry("not_found", CHECK_UPSTREAM)
        days = _days_since(r.get("created_at") or "")
        if days >= STALE_DAYS:
            return _entry("not_found", CHECK_UPSTREAM)
        return _entry("requested", "Sent upstream — searching")
    return _entry("none")


def _combine(per: dict, release_date: str = "") -> dict:
    states = {f: v["state"] for f, v in per.items()}
    if "available" in states.values():
        out = _entry("available")
    else:
        best = next((s for s in ORDER if s in states.values()), None)
        if best:
            src = next(v for v in per.values() if v["state"] == best)
            out = dict(src)
        elif release_date and release_date > str(tz.today()):
            out = _entry("upcoming", f"Out {release_date}")
        else:
            out = _entry("none")
    out["formats"] = per
    # one-line per-format summary for multi-format installs ("Audiobook: In
    # library · eBook: Requested") — only formats with something to say
    out["summary"] = " · ".join(f"{FMT_NAME[f]}: {v['label']}" for f, v in per.items() if v["state"] != "none")
    return out


def _rows_for(c, author: str, viewer) -> list[dict]:
    a = _first(author)
    if not a:
        return []
    rows = [dict(r) for r in c.execute(
        "SELECT id, user_id, asin, title, author, status, format, detail, created_at FROM requests "
        "WHERE lower(author) LIKE ? AND title NOT LIKE 'Full series: %' AND title NOT LIKE 'All books by %'",
        (f"%{a}%",))]
    return [r for r in rows if r["status"] in OPEN + ("available", "pending_approval")
            or viewer is None or r["user_id"] == viewer]


def _owned_formats(c, asin: str, title: str, author: str) -> set:
    from .routes import _library_works          # one matcher for "owned", shared with the cards
    out = {r["format"] or "audiobook" for r in _library_works(c, title, author)}
    if asin:
        out |= {r["format"] or "audiobook" for r in c.execute(
            "SELECT format FROM library WHERE gone_at IS NULL AND asin=? AND asin<>''", (asin,))}
    return out


def for_books(books: list[dict], viewer=None) -> list[dict]:
    """Status for each {asin, title, author[, release_date]} — see the module
    docstring. `viewer` is the user id whose failed/denied attempts count."""
    shown = formats.available()
    out, q = [], {"v": None}

    def queue_fn():
        if q["v"] is None:
            q["v"] = live_queue()
        return q["v"]

    by_author = {}
    with db.conn() as c:
        for b in books:
            title, author, asin = b.get("title") or "", b.get("author") or "", (b.get("asin") or "").strip()
            a = _first(author)
            if a not in by_author:
                by_author[a] = _rows_for(c, author, viewer)
            rows = titles.matches(by_author[a], title, asin) if title else \
                [r for r in by_author[a] if asin and r["asin"] == asin]
            owned = _owned_formats(c, asin, title, author)
            per = {f: _format_state(f, owned, rows, queue_fn, title, author, asin) for f in shown}
            out.append(_combine(per, b.get("release_date") or ""))
    return out


def for_book(asin: str = "", title: str = "", author: str = "", viewer=None, release_date: str = "") -> dict:
    return for_books([{"asin": asin, "title": title, "author": author, "release_date": release_date}], viewer)[0]


def for_request_group(rows: list[dict]) -> dict:
    """Status of one book on the Requests page from its own request rows (all
    one user's, one work) — the same rules as for_book()."""
    if not rows:
        return _combine({})
    r0 = max(rows, key=lambda x: x.get("id") or 0)
    shown = [f for f in MEDIA if any(f in _media_of(r.get("format")) for r in rows)]
    q = {"v": None}

    def queue_fn():
        if q["v"] is None:
            q["v"] = live_queue()
        return q["v"]

    with db.conn() as c:
        owned = _owned_formats(c, r0.get("asin") or "", r0.get("title") or "", r0.get("author") or "")
    per = {f: _format_state(f, owned, rows, queue_fn, r0.get("title") or "", r0.get("author") or "",
                            r0.get("asin") or "") for f in shown}
    return _combine(per)
