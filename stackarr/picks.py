"""Keeping pending picks honest. A suggestion is checked against the library,
your requests and your reading only when it's generated; after that nothing
looked at it again. So a pick you went on to request from search, a book
someone added straight to Audiobookshelf, or one you finished there stayed a
"fresh pick" forever — and because the pending queue is capped
(STACKARR_SUGGEST_MAX), a full queue of stale picks stopped the recommender
from ever running again.

`retire()` closes that gap. It's cheap (one pass over the user's pending rows
with the same subtitle-tolerant matching the recommenders use) and runs on
every library refresh, before a recommender run, and before Home renders.

Retired rows get their own status — 'owned', 'requested' or 'finished' — never
'rejected': nothing is written to `signals`, so a retired pick is not a
"not interested" and doesn't push the recommender away from that author or
series. The rows stay in the table, so the recommenders' dedupe still keeps
the same book from being suggested again."""
import logging

from . import absclient, db, titles

log = logging.getLogger("stackarr.picks")

RETIRED = ("owned", "requested", "finished")
# request states that mean "this is on its way" — a failed or denied request
# leaves the pick in place so it can still be added from its card
ACTIVE_REQUEST = ("queued", "handed", "available", "pending_approval", "approving")
MEDIA = ("audiobook", "ebook")


def finished_works(user: dict | None, history: list[dict] | None = None) -> list[dict]:
    """Library rows this user has finished in Audiobookshelf, most recent first,
    each with the finish time (`last_update`, ms). Pass `history` when the
    caller already fetched it (the page memoises it per request)."""
    if history is None:
        tok = (user or {}).get("abs_token")
        history = absclient.listening_history(tok) if tok else []
    done = [h for h in history if h.get("finished")]
    if not done:
        return []
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in c.execute(
            "SELECT item_id,title,author,asin,series,series_seq,format FROM library")}
    out = []
    for h in done:
        m = lib.get(h.get("item_id"))
        if m and m.get("title"):
            out.append(dict(m, last_update=h.get("last_update") or 0))
    return out


def is_finished(rows: list[dict], title: str, author: str, asin: str = "") -> bool:
    """Is (title, author) among `rows` (from finished_works)? Same author
    required, same subtitle tolerance as the owned check."""
    a = (author or "").split(",")[0].strip().lower()
    if not a or not (title or "").strip():
        return False
    cands = [r for r in rows if a in (r.get("author") or "").lower()]
    return bool(titles.matches(cands, title, asin))


def retire(user_id: int, finished: list[dict] | None = None) -> dict:
    """Retire this user's pending picks that are already in the library (in
    the pick's format), already requested by them (that format or 'both'), or
    already read — finished in Audiobookshelf (`finished`, from
    finished_works) or on the Read shelf. Reading counts across formats: a
    book you finished as an audiobook isn't a fresh pick as an eBook.
    Returns {status: count} for what was retired."""
    with db.conn() as c:
        pend = [dict(r) for r in c.execute(
            "SELECT id,asin,title,author,format FROM suggestions WHERE user_id=? AND status='pending'",
            (user_id,))]
        if not pend:
            return {}
        owned = {f: titles.KnownWorks() for f in MEDIA}
        owned_asin = {f: set() for f in MEDIA}
        for r in c.execute("SELECT title,author,asin,format FROM library WHERE gone_at IS NULL"):
            f = r["format"] if r["format"] in MEDIA else "audiobook"
            owned[f].add(r["title"], r["author"])
            if r["asin"]:
                owned_asin[f].add(r["asin"].upper())
        reqd = {f: titles.KnownWorks() for f in MEDIA}
        reqd_asin = {f: set() for f in MEDIA}
        marks = ",".join("?" * len(ACTIVE_REQUEST))
        for r in c.execute(f"SELECT title,author,asin,format FROM requests WHERE user_id=? "
                           f"AND status IN ({marks})", (user_id,) + ACTIVE_REQUEST):
            f = (r["format"] or "").lower()
            for m in (MEDIA if f == "both" else (f if f in MEDIA else "audiobook",)):
                reqd[m].add(r["title"], r["author"])
                if r["asin"]:
                    reqd_asin[m].add(r["asin"].upper())
        read, read_asin = titles.KnownWorks(), set()
        for r in finished or []:
            read.add(r["title"], r.get("author", ""))
            if r.get("asin"):
                read_asin.add(r["asin"].upper())
        for r in c.execute("SELECT rkey,title,author FROM shelf WHERE user_id=? AND state='read'", (user_id,)):
            if r["title"]:
                read.add(r["title"], r["author"] or "")
            if (r["rkey"] or "").startswith("B0"):
                read_asin.add(r["rkey"].upper())

        done = {}
        for s in pend:
            f = s["format"] if s["format"] in MEDIA else "audiobook"
            asin = (s["asin"] or "").upper()
            # no author, no title match — same rule as the owned check, so a
            # one-word title can't retire someone else's book
            work = (s["title"], s["author"]) if (s["author"] or "").strip() else None
            if (asin and asin in read_asin) or (work and work in read):
                status = "finished"
            elif (asin and asin in owned_asin[f]) or (work and work in owned[f]):
                status = "owned"
            elif (asin and asin in reqd_asin[f]) or (work and work in reqd[f]):
                status = "requested"
            else:
                continue
            if c.execute("UPDATE suggestions SET status=?,decided_at=datetime('now','localtime') "
                         "WHERE id=? AND status='pending'", (status, s["id"])).rowcount:
                done[status] = done.get(status, 0) + 1
    if done:
        log.info("user %s: retired stale picks %s", user_id, done)
    return done


def retire_for(user: dict | None) -> dict:
    """retire() for a user, fetching their Audiobookshelf finishes first (one
    /api/me call). Best-effort: a failure retires nothing."""
    if not user:
        return {}
    try:
        return retire(user["id"], finished_works(user))
    except Exception as e:
        log.warning("retiring stale picks failed for user %s: %s", user.get("id"), e)
        return {}
