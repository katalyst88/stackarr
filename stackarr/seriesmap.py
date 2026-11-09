"""Series maps: the ordered books of a series, and where each one stands for
this household — finished, in the library, requested, not out yet, or
missing. Drives the "Next in <series>" strip on the book page and the
"Continue <series>" card on Home.

Audible is the source when it knows the series. A series is itself a
catalogue product whose `relationships` list every book's ASIN and position,
so the whole order costs two calls: the series product, then ONE batch lookup
for titles, covers and release dates (a keyword search, which Up Next's "Find
missing books" uses, only finds whatever happens to rank). Series Audible
doesn't carry — ebook-only ones — fall back to Chaptarr's "Name #n"
seriesTitle, for authors Chaptarr already manages.

Every lookup is cached in meta: a series map for a day, a miss (not a series,
or the catalogue was down) for an hour, so a page render is a cache hit and a
failing catalogue isn't asked again on every view."""
import json
import logging
import time

from . import audible, chaptarr, db, titles

log = logging.getLogger("stackarr.series")

CACHE_TTL = 24 * 3600
MISS_TTL = 3600
STRIP = 3              # books on a "Next in" strip
MAX_REQUEST = 10       # books one "Request remaining" may send
MEDIA = ("audiobook", "ebook")
DRAMATIZED = ("dramatized", "graphic audio", "graphicaudio", "[ga]", "radio play")


def _cached(key: str, fetch):
    """fetch() once per TTL, remembering None (a miss) for MISS_TTL."""
    raw = db.get_meta(key, "")
    if raw:
        try:
            hit = json.loads(raw)
            ttl = CACHE_TTL if hit.get("v") is not None else MISS_TTL
            if time.time() - float(hit.get("at") or 0) < ttl:
                return hit.get("v")
        except (ValueError, TypeError, AttributeError):
            pass
    v = fetch()
    db.set_meta(key, json.dumps({"at": int(time.time()), "v": v}))
    return v


def _first_author(a: str) -> str:
    return (a or "").split(",")[0].strip()


def _language() -> str:
    from . import config
    return (db.get_meta("language", config.TARGET_LANGUAGE) or "english").lower()


def _placeholder(p: dict) -> bool:
    """Audible lists not-yet-announced books as "Embargo title …" dated 2200."""
    return (p.get("title") or "").lower().startswith("embargo title") or (p.get("release_date") or "") >= "2100"


def from_audible(series_asin: str) -> dict | None:
    """{name, series_asin, source, books: [{seq, asin, title, author, cover,
    release_date}]} for an Audible series, or None."""
    def fetch():
        kids = audible.series_children(series_asin)
        if not kids:
            return None
        prods = {p["asin"]: p for p in audible.by_asins([a for a, _ in kids]) if not _placeholder(p)}
        # editions in another language that share the series product are not
        # this reader's next book (unless the whole series is in that language)
        want = _language()
        if want != "any":
            same = {a: p for a, p in prods.items() if (p.get("language") or "").lower() in ("", want)}
            prods = same or prods
        name, by_seq = "", {}
        for asin, seq in kids:
            p = prods.get(asin)
            if not p:
                continue                      # not sold in this marketplace
            if p.get("series_asin") == series_asin and not name:
                name = p.get("series") or ""
            dram = any(d in (p.get("title") or "").lower() for d in DRAMATIZED)
            cur = by_seq.get(seq)
            # one book per position: keep the first plain edition over a
            # dramatized/GraphicAudio one that shares its number
            if cur and (dram or not cur["dram"]):
                continue
            by_seq[seq] = {"dram": dram, "b": {
                "seq": seq, "asin": asin, "title": p.get("title") or "",
                "author": p.get("author") or "", "cover": p.get("cover") or "",
                "release_date": p.get("release_date") or ""}}
        books = [by_seq[s]["b"] for s in sorted(by_seq)]
        return {"name": name, "series_asin": series_asin, "source": "audible", "books": books} if books else None
    return _cached("ser:a:" + series_asin, fetch)


def from_chaptarr(name: str, author: str) -> dict | None:
    """The same shape from Chaptarr's seriesTitle (no covers; read-only)."""
    author = _first_author(author)

    def fetch():
        ents = chaptarr.series_entries(author, name)
        if not ents:
            return None
        return {"name": name, "series_asin": "", "source": "chaptarr", "books": [
            {"seq": e["seq"], "asin": e["asin"], "title": e["title"], "author": author,
             "cover": "", "release_date": e["release_date"]} for e in ents]}
    return _cached(f"ser:c:{titles.norm(name)}|{titles.norm(author)}", fetch)


def series_of(asin: str = "", title: str = "", author: str = "") -> dict:
    """{series_asin, name, seq} for one book, or {}. A library book usually has
    no ASIN, so it's found by title + author — and that search hit is only
    trusted when it's the same work by the same author."""
    real = asin if (asin or "").startswith("B0") else ""
    key = db.rating_key(real, titles.clean(title), author)
    if not key or key == "t-":
        return {}

    def fetch():
        a = real or (audible.find_asin(titles.clean(title), _first_author(author), _language()) if title else "")
        p = audible.by_asin(a) if a else None
        if not p or not p.get("series_asin"):
            return None
        if not real:
            fa = _first_author(author).lower()
            if not fa or fa not in (p.get("author") or "").lower() or not titles.matches([p], title):
                return None
        return {"series_asin": p["series_asin"], "name": p.get("series") or "", "seq": p.get("sequence")}
    return _cached("serof:" + key, fetch) or {}


def resolve(asin: str = "", title: str = "", author: str = "", series: str = "",
            series_asin: str = "") -> dict | None:
    """The series map for a book (by its ASIN, or title + author) or a named
    series (series + author). Audible first, then Chaptarr. None if neither
    can place it."""
    sa = series_asin
    if not sa and ((asin or "").startswith("B0") or title):
        sa = series_of(asin, title, author).get("series_asin", "")
    data = from_audible(sa) if sa else None
    if not data and series and author:
        data = from_chaptarr(series, author)
    if data:
        data = dict(data, name=data.get("name") or series)
    return data


def _media(fmt: str) -> tuple:
    f = (fmt or "").lower()
    return MEDIA if f == "both" else ((f,) if f in MEDIA else ("audiobook",))


def annotate(data: dict, finished: list[dict], default_author: str = "", viewer=None) -> list[dict]:
    """The series' books, each with where it stands:
      owned      formats it's in the library in
      requested  formats with an open request (anyone's — the library is shared)
      finished   this user finished it in Audiobookshelf
      upcoming   its release date is still ahead
      state      one word for the badge: finished | missing | upcoming, else the
                 status.py state (available | downloading | waiting | requested …)
      status     the status.py dict (label, cls, detail) the badge shows
      key        stable id the request-remaining confirm round-trips
    `finished` is picks.finished_works() for the viewing user."""
    from . import formats, picks, status, tz
    today = str(tz.today())
    books = [dict(b, author=b.get("author") or default_author) for b in data.get("books") or []]
    authors = {_first_author(b["author"]).lower() for b in books if b["author"]}
    asins = [b["asin"] for b in books if b.get("asin")]
    lib, req = [], []
    with db.conn() as c:
        marks = ",".join("?" * len(picks.ACTIVE_REQUEST))
        for a in authors:
            lib += [dict(r) for r in c.execute(
                "SELECT title, author, asin, format FROM library WHERE gone_at IS NULL "
                "AND lower(author) LIKE ?", (f"%{a}%",))]
            req += [dict(r) for r in c.execute(
                f"SELECT title, author, asin, format FROM requests WHERE status IN ({marks}) "
                f"AND lower(author) LIKE ?", picks.ACTIVE_REQUEST + (f"%{a}%",))]
        if asins:
            q = ",".join("?" * len(asins))
            lib += [dict(r) for r in c.execute(
                f"SELECT title, author, asin, format FROM library WHERE gone_at IS NULL AND asin IN ({q})", asins)]
    sts = status.for_books(books, viewer=viewer)
    for b, st in zip(books, sts):
        b["owned"] = [f for f in MEDIA if titles.matches(
            [r for r in lib if (r["format"] or "audiobook") == f], b["title"], b.get("asin", ""))]
        b["requested"] = [f for f in MEDIA if titles.matches(
            [r for r in req if f in _media(r["format"])], b["title"], b.get("asin", ""))]
        b["finished"] = picks.is_finished(finished, b["title"], b["author"], b.get("asin", ""))
        b["upcoming"] = bool(b.get("release_date") and b["release_date"] > today)
        b["status"] = st
        b["state"] = ("finished" if b["finished"]
                      else st["state"] if st["state"] not in ("none", "upcoming", "failed", "denied")
                      else "upcoming" if b["upcoming"] else "missing")
        b["key"] = b.get("asin") or db.rating_key("", b["title"], b["author"])
    return books


def strip(books: list[dict], after: float | None = None, n: int = STRIP) -> tuple:
    """(start, next n books): the books after the highest one this user has
    finished, or after `after` (the book being viewed) when they've finished
    none, or from the start."""
    done = [b["seq"] for b in books if b.get("finished")]
    start = max(done) if done else after
    return start, [b for b in books if start is None or b["seq"] > start][:n]


def plan(books: list[dict], fmts: list[str], cap: int = MAX_REQUEST) -> dict:
    """What "Request remaining" would send, in series order: {todo: books with
    `need` (the formats to request), more: how many past the cap, skipped:
    books with `why` (finished | upcoming | owned | requested)}. A book is
    skipped when you've finished it, it isn't out yet, or every format asked
    for is already in the library or requested. The confirm dialog
    (Stackarr.requestRemaining) computes the same plan client-side to list the
    exact books; the server recomputes it before sending anything."""
    todo, skipped = [], []
    for b in books:
        if b.get("finished"):
            skipped.append(dict(b, why="finished"))
            continue
        if b.get("upcoming"):
            skipped.append(dict(b, why="upcoming"))
            continue
        need = [f for f in fmts if f not in b.get("owned", []) and f not in b.get("requested", [])]
        if not need:
            skipped.append(dict(b, why="owned" if any(f in b.get("owned", []) for f in fmts) else "requested"))
            continue
        todo.append(dict(b, need=need))
    return {"todo": todo[:cap], "more": max(len(todo) - cap, 0), "skipped": skipped}
