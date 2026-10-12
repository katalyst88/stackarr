"""Chaptarr handoff. When a suggestion is approved, Stackarr asks Chaptarr
(the *arr book backend) to add the author/book and search for it. Stackarr
never touches a download client directly — Chaptarr owns grab/import."""
import logging
import re
import time

import requests

from . import config, db

log = logging.getLogger("stackarr.chaptarr")


def url() -> str:
    return db.setting("chaptarr_url", config.CHAPTARR_URL).rstrip("/")


def api_key() -> str:
    return db.setting("chaptarr_api_key", config.CHAPTARR_API_KEY)


def root_folder() -> str:
    rf = db.setting("chaptarr_root_folder", config.CHAPTARR_ROOT_FOLDER)
    # Guard against Git-Bash path mangling (a unix /path passed through Git Bash
    # at container-create time becomes "C:/Program Files/Git/path"). Recover the
    # real container path so Chaptarr doesn't reject it as an invalid path.
    m = re.search(r"/Git(/.*)$", rf)
    if rf.startswith("C:") and "Program Files/Git" in rf and m:
        rf = m.group(1)
    return rf


def ebook_root_folder() -> str:
    """Root folder for ebook handoffs (Chaptarr's E-Book library). Falls back to
    the audiobook root_folder() when unconfigured, so legacy single-root installs
    are unchanged."""
    rf = db.setting("chaptarr_ebook_root_folder", config.CHAPTARR_EBOOK_ROOT_FOLDER)
    return (rf or "").rstrip("/") or root_folder()


def _profile(key: str, fallback: int) -> int:
    try:
        return int(db.setting(key, str(fallback)))
    except ValueError:
        return fallback


def _h():
    return {"X-Api-Key": api_key(), "Content-Type": "application/json"}


def configured() -> bool:
    return bool(url() and api_key())


def monitored_keys() -> set[str]:
    """author names Chaptarr already manages, lowercased, for dedupe."""
    keys = set()
    try:
        for a in requests.get(f"{url()}/api/v1/author", headers=_h(), timeout=20).json():
            keys.add((a.get("authorName") or "").lower())
    except Exception as e:
        log.debug("chaptarr monitored_keys failed: %s", e)
    return keys


def health() -> list[dict]:
    """Chaptarr's own health warnings (e.g. the api.chaptarr.com metadata
    outage) — surfaced in Stackarr's connection test so degraded metadata is
    visible. Each: {type, message}."""
    try:
        r = requests.get(f"{url()}/api/v1/health", headers=_h(), timeout=15)
        return [{"type": h.get("type", ""), "message": h.get("message", "")} for h in r.json()] if r.ok else []
    except Exception:
        return []


def can_grab(title: str, author: str) -> bool:
    """Cheap pre-check: does Chaptarr's catalogue know this author/title? Lets
    the UI flag 'no releases yet' before queueing a dead-end."""
    if not configured():
        return False
    try:
        r = requests.get(f"{url()}/api/v1/author/lookup", headers=_h(),
                         params={"term": author or title}, timeout=30)
        return bool(r.ok and r.json())
    except Exception:
        return False


def _plain_reason(rec: dict) -> str:
    """Chaptarr's reason a queue item is held or stuck, in one line."""
    msgs = []
    for m in rec.get("statusMessages") or []:
        msgs += [x for x in (m.get("messages") or []) if x] or ([m.get("title")] if m.get("title") else [])
    if rec.get("errorMessage"):
        msgs.insert(0, rec["errorMessage"])
    return "; ".join(dict.fromkeys(str(x).strip() for x in msgs if str(x).strip()))[:240]


def _eta(timeleft) -> str:
    """Chaptarr's "d.hh:mm:ss" / "hh:mm:ss" time-left as "4 days" / "3 h" / "36 min"."""
    m = re.fullmatch(r"(?:(\d+)\.)?(\d+):(\d+):(\d+)", str(timeleft or "").strip())
    if not m:
        return ""
    d, h, mi = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    if d:
        return f"{d} day{'s' if d != 1 else ''}"
    if h:
        return f"{h} h"
    return f"{max(mi, 1)} min"


def queue() -> list[dict] | None:
    """Chaptarr's download queue, one entry per item:
    {title, author, asin, media, state, pct, eta, reason}. `state` is one of
    downloading | importing | waiting (Chaptarr is holding it: a delay profile
    or a pending/blocked release) | stalled (no peers) | failed.
    None when Chaptarr can't be read, so callers can tell "nothing queued"
    from "don't know". One GET per page of 200; callers cache it."""
    out = []
    try:
        page = 1
        while page <= 10:                                # safety cap: 2,000 items
            r = requests.get(f"{url()}/api/v1/queue", headers=_h(),
                             params={"page": page, "pageSize": 200,
                                     "includeAuthor": "true", "includeBook": "true"}, timeout=8)
            if not r.ok:
                return None if page == 1 else out
            data = r.json() or {}
            records = data.get("records", [])
            for rec in records:
                book = rec.get("book") or {}
                st = (rec.get("status") or "").lower()
                tds = (rec.get("trackedDownloadStatus") or "").lower()
                tdl = (rec.get("trackedDownloadState") or "").lower()
                reason = _plain_reason(rec)
                low = reason.lower()
                if "409" in low or "already exists" in low or "already in the download client" in low:
                    # qBittorrent answered 409 Conflict: the torrent is already
                    # there (another row of the same work grabbed it) — not a failure
                    state, reason = "waiting", ("Already in the download client — Chaptarr picks it up "
                                                "when that download finishes")
                elif "import" in tdl or st == "completed":
                    state = "importing"
                elif st == "failed" or tdl == "failed" or tds == "error":
                    state = "failed"
                elif "stall" in reason.lower():
                    state = "stalled"
                elif st in ("delay", "pending") or tds == "warning" or st == "warning":
                    state = "waiting"
                else:
                    state = "downloading"
                size, left = rec.get("size") or 0, rec.get("sizeleft") or 0
                out.append({
                    "title": book.get("title") or rec.get("title") or "",
                    "author": ((rec.get("author") or {}).get("authorName") or book.get("authorTitle") or ""),
                    "asin": book.get("audibleASIN") or book.get("asin") or "",
                    "media": (book.get("mediaType") or "").lower(),
                    "state": state,
                    "pct": int(round(100 * (size - left) / size)) if size and state == "downloading" else None,
                    "eta": _eta(rec.get("timeleft")) if state == "downloading" else "",
                    "reason": reason,
                })
            if len(records) < 200:
                break
            page += 1
    except Exception as e:
        log.debug("chaptarr queue failed: %s", e)
        return None
    return out


def queue_status() -> dict:
    """Live download state keyed by lowercased title — {title: state}. Kept
    for API callers; Stackarr itself reads queue() through status.py."""
    return {q["title"].lower().strip(): q["state"] for q in (queue() or []) if q["title"]}


def _ensure_tag(label: str = "stackarr") -> int | None:
    """Ensure a Chaptarr tag exists; return its id (so Stackarr-added books are
    tagged for easy filtering in Chaptarr). Best-effort."""
    try:
        for t in requests.get(f"{url()}/api/v1/tag", headers=_h(), timeout=15).json():
            if (t.get("label") or "").lower() == label:
                return t.get("id")
        r = requests.post(f"{url()}/api/v1/tag", headers=_h(), json={"label": label}, timeout=15)
        return r.json().get("id") if r.ok else None
    except Exception:
        return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _find_author(foreign_id: str, name: str) -> dict | None:
    """Locate an author already in Chaptarr by foreign id (preferred) or name.
    Raises on a transport/HTTP error instead of swallowing it — otherwise a
    transient failure looks like 'author absent' and add_and_search POSTs a
    DUPLICATE author. The caller's outer try turns a raise into a clean retry."""
    r = requests.get(f"{url()}/api/v1/author", headers=_h(), timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return None
    for a in data:
        if foreign_id and a.get("foreignAuthorId") == foreign_id:
            return a
        if _norm(a.get("authorName")) == _norm(name):
            return a
    return None


def _author_books(author_id: int) -> list[dict]:
    try:
        return requests.get(f"{url()}/api/v1/book", headers=_h(),
                            params={"authorId": author_id}, timeout=40).json() or []
    except Exception:
        return []


def _pick_author(results: list[dict], author: str) -> dict:
    """Choose the author the request actually named. Chaptarr's /author/lookup is
    a fuzzy search that does NOT always rank the requested author first — e.g. for
    "James Baldwin" another author whose book metadata mentions the name can
    outrank him, so blindly taking results[0] adds the wrong author and the title
    is never found. Prefer an exact normalized name match, then a contains match,
    before falling back to the top result."""
    na = _norm(author)
    if na:
        for r in results:
            if _norm(r.get("authorName")) == na:
                return r
        for r in results:
            rn = _norm(r.get("authorName"))
            if rn and (na in rn or rn in na):
                return r
    return results[0]


_SUBTITLE = re.compile(r"\s*(?::|\s-\s|\s—\s)\s*")


def _base_title(s: str) -> str:
    """Normalized title with any subtitle dropped — the part before the first
    ':' / ' - ' separator ("Mindset - Updated Edition" -> "mindset")."""
    return _norm(_SUBTITLE.split(s or "", 1)[0])


def _one_work(cands: list[dict]) -> dict | None:
    """The single work in `cands`, else None. Counts DISTINCT normalized titles,
    not rows: Chaptarr's media split lists one work twice (an audiobook row and
    an ebook row), and those two are the same work, not an ambiguity."""
    if not cands:
        return None
    return cands[0] if len({_norm(b.get("title")) for b in cands}) == 1 else None


def _match_book(books: list[dict], title: str, asin: str) -> dict | None:
    """Pick the specific book a recommendation refers to: ASIN first (exact),
    then exact normalized title, then the title minus its subtitle, then a
    prefix / subtitle-stripped fuzzy match."""
    asin = (asin or "").strip().upper()
    if asin:
        for b in books:
            if (b.get("asin") or "").upper() == asin or (b.get("audibleASIN") or "").upper() == asin:
                return b
    nt = _norm(title)
    if not nt:
        return None
    for b in books:
        if _norm(b.get("title")) == nt:
            return b
    # Audible decorates its titles with a subtitle Chaptarr's metadata doesn't
    # carry ("Pandora's Star: Commonwealth Saga 1" vs "Pandora's Star"), and the
    # ASINs differ too (a later Audible re-release vs Chaptarr's), so the tiers
    # above both miss and the whole request fails with "couldn't find … to
    # search". Retry on the title either side of the separator.
    parts = _SUBTITLE.split(title or "", 1)
    nb, ntail = _norm(parts[0]), (_norm(parts[1]) if len(parts) > 1 else "")
    # The lead-in is sometimes the franchise rather than the work ("Star Wars:
    # Thrawn"), so a book titled exactly like the part AFTER the separator beats
    # the part before it.
    for cand in (ntail, nb):
        if cand and cand != nt:
            for b in books:
                if _norm(b.get("title")) == cand:
                    return b
    # Fuzzy tiers: only for a specific-enough title AND only when unambiguous — a
    # short/common title ("it", "dune") matches the wrong book otherwise, and
    # we'd confidently search+grab that. Chaptarr's title may be the longer one
    # ("Pandora's Star (Commonwealth Saga #1)"), or both sides may be decorated
    # differently, so try prefix first and subtitle-vs-subtitle second.
    if len(nt) >= 8:
        hit = _one_work([b for b in books if _norm(b.get("title")).startswith(nt)])
        if hit:
            return hit
    if len(nb) >= 8:
        hit = _one_work([b for b in books if _base_title(b.get("title")) == nb])
        if hit:
            return hit
    return None


def _files(row: dict) -> int:
    return int(((row or {}).get("statistics") or {}).get("bookFileCount") or 0)


def work_rows(rows: list[dict], title: str, asin: str = "", media: str = "") -> list[dict]:
    """Every row Chaptarr holds for this WORK in `media` — not just the first
    title match. Chaptarr can list one work two or three times for an author:
    a series-linked row and a series-less one from another metadata source
    (Dungeon Crawler Carl #2: audiobook 8798 "gr:" + 8805 "hc:", ebook 8859
    + 8866 "Carl's Doomsday Scenario: Dungeon Crawler Carl Book 2"). Each row
    is the same title (brackets aside), or that title plus a subtitle ("…:
    Dungeon Crawler Carl Book 2"); a different "Book N" never is, and omnibus
    rows never count."""
    from . import titles
    pool = [r for r in (_for_media(rows, media) if media else rows) if not r.get("isOmnibus")]
    a = (asin or "").strip().upper()
    want = titles.norm(titles._BRACKETS.sub("", title or ""))
    out = []
    for r in pool:
        rt = r.get("title") or ""
        full, base, tail = titles.parts(rt)
        if (a and a in ((r.get("asin") or "").upper(), (r.get("audibleASIN") or "").upper())) \
                or (want and (titles.norm(titles._BRACKETS.sub("", rt)) == want or (tail and base == want))):
            out.append(r)
    return out


def pick_row(rows: list[dict], title: str, asin: str = "", media: str = "") -> tuple:
    """(row to monitor + search, sibling that already has a file) for a work.

    If ANY row of the work holds a file, Chaptarr already has the book: the
    caller must not search again (DCC #2's ebook was re-downloaded because the
    file sat on the other row). Otherwise prefer the row with the requested
    ASIN, then one with any ASIN (the row Chaptarr's import matches: DCC #2's
    audiobook import was blocked — "grabbed for BookId 8798 … but import
    matched BookId 8805"), then a monitored one."""
    sibs = work_rows(rows, title, asin, media)
    have = next((r for r in sibs if _files(r) > 0), None)
    if have or not sibs:
        return (have, have)
    a = (asin or "").strip().upper()
    best = sorted(sibs, key=lambda r: (
        not (a and a in ((r.get("asin") or "").upper(), (r.get("audibleASIN") or "").upper())),
        not (r.get("asin") or r.get("audibleASIN")), not r.get("monitored"), r.get("id") or 0))
    return (best[0], None)


def _media_edition_id(book_id: int, media: str) -> int:
    """Chaptarr splits each work into per-media book rows (audiobook + ebook
    siblings). A title match can land on the wrong-media row, so the caller would
    search/grab the format that's already satisfied and never fetch the requested
    one. The release view reports this row's sibling id + the sibling's media
    type; if this row isn't the requested media, switch to its sibling."""
    try:
        rs = requests.get(f"{url()}/api/v1/release", headers=_h(),
                          params={"bookId": book_id}, timeout=60).json()
    except Exception:
        return book_id
    if not isinstance(rs, dict):
        return book_id
    # siblingMediaType names the OTHER row's media, so this row is the opposite.
    sib_media = rs.get("siblingMediaType")
    this_media = {"ebook": "audiobook", "audiobook": "ebook"}.get(sib_media)
    sib_id = rs.get("siblingBookId")
    if this_media and this_media != media and sib_id:
        return sib_id
    return book_id


# Chaptarr's per-media "Monitor existing books" setting
# ({audiobook,ebook}MonitorExisting): 0 = None, 1 = All, 2 = Selected. "All"
# monitors the author's whole bibliography for that media the moment it's set —
# for a request-driven tool that means Chaptarr goes on to grab every book by
# every author anyone has requested one book from. "Selected" keeps the author
# monitored for the media while only the books we monitor explicitly are wanted.
MONITOR_EXISTING_NONE, MONITOR_EXISTING_ALL, MONITOR_EXISTING_SELECTED = 0, 1, 2


def _for_media(books: list[dict], media: str) -> list[dict]:
    """The author's book rows for one media type. Chaptarr's media split gives
    each work an audiobook row and an ebook row, tagged `mediaType`; a schema
    without the split (no `mediaType` on any row) is returned unchanged."""
    if not any(b.get("mediaType") for b in books):
        return books
    return [b for b in books if b.get("mediaType") == media]


def _series_books(books: list[dict], series: str) -> list[dict]:
    """The rows belonging to `series`. Each row's `seriesTitle` lists the series
    it's in as "Name #n", ';'-separated (Readarr's format)."""
    ns = _norm(series)
    if not ns:
        return []
    return [b for b in books
            if any(_norm(re.sub(r"#\s*[\d.]+\s*$", "", part)) == ns
                   for part in (b.get("seriesTitle") or "").split(";"))]


def series_entries(author: str, series: str) -> list[dict]:
    """The books Chaptarr lists for `series` by an author it already manages,
    in series order: [{seq, title, asin, release_date, media}] with one entry
    per work (a media-split work's audiobook and ebook rows merge, `media`
    lists both). The fallback series map for series Audible doesn't carry
    (ebook-only ones). Read-only: it never runs an author lookup or adds
    anyone, so an author Chaptarr doesn't have yet simply has no entries."""
    if not configured() or not series:
        return []
    aid = _author_id((author or "").split(",")[0].strip(), {})
    if not aid:
        return []
    ns, works = _norm(series), {}
    for b in _series_books(_author_books(aid), series):
        seq = None
        for part in (b.get("seriesTitle") or "").split(";"):
            m = re.match(r"\s*(.*?)\s*#\s*([\d.]+)\s*$", part)
            if m and _norm(m.group(1)) == ns:
                try:
                    seq = float(m.group(2))
                except ValueError:
                    seq = None
                break
        if seq is None:
            continue                     # no position -> can't place it in the order
        w = works.setdefault(_norm(b.get("title")), {
            "seq": seq, "title": b.get("title") or "", "media": [],
            "asin": (b.get("asin") or b.get("audibleASIN") or "").strip(),
            "release_date": (b.get("releaseDate") or "")[:10]})
        if b.get("mediaType") and b["mediaType"] not in w["media"]:
            w["media"].append(b["mediaType"])
    return sorted(works.values(), key=lambda w: w["seq"])


def _ensure_media_monitored(author_obj: dict, media: str, rf: str,
                            qp: int | None = None, mp: int | None = None) -> None:
    """Chaptarr's current schema gates book monitoring behind per-media author
    monitoring (audiobook/ebookMonitorExisting + matching root folder + profiles).
    Make sure the active media type is fully set up — crucially when REUSING an
    author first added for the OTHER format, whose {media}RootFolderPath/profiles
    are unset, so a cross-format request isn't 400'd. Idempotent; best-effort.

    Enabling a media type sets it to monitor *selected* books only, never All —
    All would make Chaptarr want the author's entire bibliography in that format
    when the user asked for one book. A media type the author is already
    monitored for keeps whatever the user chose (we never unmonitor anything)."""
    changed = False
    if not author_obj.get("monitored"):
        author_obj["monitored"] = True; changed = True
    pfx = "audiobook" if media == "audiobook" else "ebook"
    if not author_obj.get(f"{pfx}MonitorExisting"):
        author_obj[f"{pfx}MonitorExisting"] = MONITOR_EXISTING_SELECTED
        # nor anything the author publishes later, for a media type that was off
        author_obj[f"{pfx}MonitorNewItems"] = "none"
        if f"{pfx}Monitored" in author_obj:
            author_obj[f"{pfx}Monitored"] = True
        changed = True
    if qp and not author_obj.get(f"{pfx}QualityProfileId"):
        author_obj[f"{pfx}QualityProfileId"] = qp; changed = True
    if mp and not author_obj.get(f"{pfx}MetadataProfileId"):
        author_obj[f"{pfx}MetadataProfileId"] = mp; changed = True
    # Force the correct per-media root even if already set: an author first added
    # for audiobooks has its ebookRootFolderPath left at the audiobook folder, so
    # a later ebook request would file the ebook into the audiobook library.
    if rf and author_obj.get(f"{pfx}RootFolderPath") != rf:
        author_obj[f"{pfx}RootFolderPath"] = rf; changed = True
    if changed and author_obj.get("id"):
        try:
            requests.put(f"{url()}/api/v1/author/{author_obj['id']}", headers=_h(),
                         json=author_obj, timeout=30)
        except Exception as e:
            log.debug("chaptarr _ensure_media_monitored PUT failed: %s", e)


def _author_id(name: str, cache: dict) -> int | None:
    """An author already in Chaptarr, by name (exact, then contains — never a
    guess). The author list is fetched once per `cache`."""
    if "authors" not in cache:
        try:
            r = requests.get(f"{url()}/api/v1/author", headers=_h(), timeout=20)
            cache["authors"] = r.json() if r.ok and isinstance(r.json(), list) else []
        except Exception as e:
            log.debug("chaptarr author list failed: %s", e)
            cache["authors"] = []
    na = _norm(name)
    if not na:
        return None
    for exact in (True, False):
        for a in cache["authors"]:
            an = _norm(a.get("authorName"))
            if an and (an == na if exact else (na in an or an in na)):
                return a.get("id")
    return None


def has_book_file(title: str, author: str, asin: str = "", media: str = "audiobook",
                  ref: str = "", cache: dict | None = None) -> bool:
    """Has Chaptarr imported a file for this book in this media? True when the
    requested media edition's row reports statistics.bookFileCount > 0.

    Chaptarr knows a request is done even when the library snapshot can't see
    it — an ebook filed where no connected source looks, or a title the library
    spells differently. `ref` is the request's chaptarr_ref (the author id
    add_and_search returned); without one the author is found by name. Pass
    the same `cache` across a batch so each author's books are fetched once."""
    if not configured():
        return False
    cache = {} if cache is None else cache
    author = (author or "").split(",")[0].strip()
    author_id = int(ref) if str(ref or "").isdigit() else _author_id(author, cache)
    if not author_id:
        return False
    books = cache.setdefault(("books", author_id), None)
    if books is None:
        books = cache[("books", author_id)] = _author_books(author_id)
    # any row of the work counts: the file can sit on a sibling row
    if any(_files(r) > 0 for r in work_rows(books, title, asin, media)):
        return True
    target = _match_book(_for_media(books, media), title, asin)
    return bool(target and _files(target) > 0)


def mark_read(title: str, author: str) -> bool:
    """A book you've already read shouldn't be grabbed. If Chaptarr is managing
    it and the book is monitored, unmonitor it so it won't be searched/downloaded.
    Best-effort; returns True only if something was actually unmonitored. Chaptarr
    has no 'read' state, so unmonitoring is the meaningful equivalent."""
    if not configured():
        return False
    try:
        look = requests.get(f"{url()}/api/v1/author/lookup", headers=_h(),
                            params={"term": author or title}, timeout=30)
        results = look.json() if look.ok else []
        if not results:
            return False
        # Same fuzzy lookup as add_and_search — take the author actually named,
        # not the top hit, or we look for the book under someone else.
        a = _pick_author(results, author)
        existing = _find_author(a.get("foreignAuthorId", ""), a.get("authorName") or author)
        if not existing:
            return False
        target = _match_book(_author_books(existing["id"]), title, "")
        if not target or not (target.get("monitored") or target.get("audiobookMonitored")
                              or target.get("ebookMonitored")):
            return False
        r = requests.put(f"{url()}/api/v1/book/monitor", headers=_h(),
                         json={"bookIds": [target["id"]], "monitored": False}, timeout=20)
        return bool(r.ok)
    except Exception as e:
        log.debug("chaptarr mark_read failed: %s", e)
        return False


def _existing_author(author: str) -> dict | None:
    """An author Chaptarr already manages, by exact (normalized) name — checked
    BEFORE /author/lookup. The lookup is a live metadata search, and it can come
    back empty on rapid back-to-back calls even for an author Chaptarr has: in
    the field two requests seconds apart both failed with "Chaptarr couldn't
    find 'Will Wight'" while he was author id 3. Reusing the author we already
    have needs no lookup at all. A failed author list isn't fatal — it falls
    through to the lookup, as before."""
    if not (author or "").strip():
        return None
    try:
        return _find_author("", author)
    except Exception as e:
        log.debug("chaptarr author list failed: %s", e)
        return None


def _ensure_author(title: str, author: str, media: str, rf: str, profiles: tuple,
                   tag_id: int | None) -> tuple:
    """(author_obj, None) for the author a request names — reused when Chaptarr
    already has them, else looked up and added with the media-split fields set
    — or (None, {ok: False, retry?, detail}) saying why not. `profiles` is
    (qp, mp, ab_qp, ab_mp, eb_qp, eb_mp). May raise on a transport error; the
    callers' outer try turns that into a clean failure."""
    qp, mp, ab_qp, ab_mp, eb_qp, eb_mp = profiles
    author_obj = _existing_author(author)
    if author_obj is not None:
        return author_obj, None
    look = requests.get(f"{url()}/api/v1/author/lookup",
                        headers=_h(), params={"term": author or title}, timeout=60)
    # Distinguish a real outage / auth / rate-limit from a genuine catalogue
    # miss, so a config error isn't reported to the user as "not found" and
    # auto_approve can tell "retry later" (stop) from "skip this one" (continue).
    if look.status_code >= 500:
        return None, {"ok": False, "retry": True, "detail": "The upstream book database is offline right now — we've kept this; try again shortly."}
    if look.status_code in (401, 403):
        return None, {"ok": False, "detail": "Upstream rejected the API key — check Settings → Connections."}
    if look.status_code == 429:
        return None, {"ok": False, "retry": True, "detail": "Upstream is rate-limited right now — try again shortly."}
    results = look.json() if look.ok else []
    if not results:
        return None, {"ok": False, "detail": f"Couldn't find “{author or title}” upstream."}
    a = _pick_author(results, author)
    foreign_id = a.get("foreignAuthorId", "")
    author_obj = _find_author(foreign_id, a.get("authorName") or author)
    if author_obj is None:
        folder = a.get("folder") or a.get("authorName") or author
        # Set BOTH the media-split fields (current Chaptarr) and the legacy
        # fields (older builds) so the handoff works across schema versions.
        a.update(
            mediaType=media, selectedMediaType=media, lastSelectedMediaType=media,
            qualityProfileId=qp, metadataProfileId=mp,
            audiobookQualityProfileId=ab_qp, audiobookMetadataProfileId=ab_mp,
            ebookQualityProfileId=eb_qp, ebookMetadataProfileId=eb_mp,
            rootFolderPath=rf, path=f"{rf.rstrip('/')}/{folder}",
            # Monitored for the requested media, but only for books the caller
            # monitors explicitly — "All" here made Chaptarr want (and
            # grab) the author's whole bibliography for a one-book request.
            audiobookMonitorExisting=(MONITOR_EXISTING_SELECTED if media == "audiobook"
                                      else MONITOR_EXISTING_NONE),
            ebookMonitorExisting=(MONITOR_EXISTING_SELECTED if media == "ebook"
                                  else MONITOR_EXISTING_NONE),
            audiobookMonitored=(media == "audiobook"), ebookMonitored=(media == "ebook"),
            audiobookMonitorFuture=False, ebookMonitorFuture=False,
            audiobookMonitorNewItems="none", ebookMonitorNewItems="none",
            tags=([tag_id] if tag_id else []),
            monitored=True, monitorNewItems="none",
            # Don't auto-grab on add; the caller monitors + searches precisely.
            addOptions={"monitor": "none", "booksToMonitor": [],
                        "searchForMissingBooks": False})
        # Pin the per-media root so the pick lands in the configured folder,
        # but Chaptarr 400s ("root folder does not have <media> defaults
        # configured") if that folder isn't set up for this media type. So
        # retry once without it, letting Chaptarr fall back to its default root.
        media_root_key = f"{media}RootFolderPath"
        a[media_root_key] = rf
        r = requests.post(f"{url()}/api/v1/author", headers=_h(), json=a, timeout=90)
        if not r.ok and "defaults configured" in (r.text or ""):
            a.pop(media_root_key, None)
            r = requests.post(f"{url()}/api/v1/author", headers=_h(), json=a, timeout=90)
        if not r.ok:
            body = r.text or ""
            if r.status_code in (502, 503) or "V5 API" in body or "author info" in body:
                return None, {"ok": False, "retry": True, "detail": "The upstream metadata service is down right now, so it can't add "
                              "books — this is a Chaptarr-side issue, not Stackarr. We've kept your pick; retry shortly."}
            if "Invalid Path" in body:
                return None, {"ok": False, "detail": "Upstream rejected the root folder path — check Settings → Connections → "
                              "Chaptarr root folder matches a path that exists inside the Chaptarr container."}
            return None, {"ok": False, "detail": "Couldn't add this one upstream right now — please try again in a bit."}
        author_obj = r.json()
    return author_obj, None


# Monitor the target book(s) then dispatch a search. These calls are
# checked (HTTP status + transport errors) so we never claim "it's
# searching now" when nothing actually started — the old code ignored the
# responses and reported success on a 4xx/5xx or a swallowed timeout,
# leaving the UI stuck on "Requested". The author IS created either way, so
# a failure here returns ok:False (retry is idempotent via _find_author).
def _dispatch(book_ids: list, command: dict) -> bool:
    try:
        mr = requests.put(f"{url()}/api/v1/book/monitor", headers=_h(),
                          json={"bookIds": book_ids, "monitored": True}, timeout=30)
        if not mr.ok:
            return False
        cr = requests.post(f"{url()}/api/v1/command", headers=_h(), json=command, timeout=20)
        return bool(cr.ok)
    except Exception as e:
        log.debug("chaptarr dispatch failed: %s", e)
        return False


NOT_CONNECTED = "Stackarr isn't connected to Chaptarr yet — add it in Settings → Connections."


def _media_setup(fmt: str, root_folder_override: str = "") -> tuple:
    """(media, root folder, profiles) for a handoff in `fmt`. Audiobook + ebook
    each have their own quality/metadata profile pair in Chaptarr; the active
    media type's pair becomes the author's primary. `profiles` is
    (qp, mp, ab_qp, ab_mp, eb_qp, eb_mp) as _ensure_author takes it."""
    ab_qp = _profile("chaptarr_quality_profile_id", config.CHAPTARR_QUALITY_PROFILE_ID)
    ab_mp = _profile("chaptarr_metadata_profile_id", config.CHAPTARR_METADATA_PROFILE_ID)
    eb_qp = _profile("chaptarr_ebook_quality_profile_id", config.CHAPTARR_EBOOK_QUALITY_PROFILE_ID)
    eb_mp = _profile("chaptarr_ebook_metadata_profile_id", config.CHAPTARR_EBOOK_METADATA_PROFILE_ID)
    media = "ebook" if fmt == "ebook" else "audiobook"
    qp, mp = (eb_qp, eb_mp) if media == "ebook" else (ab_qp, ab_mp)
    default_root = ebook_root_folder() if media == "ebook" else root_folder()
    rf = (root_folder_override or default_root).rstrip("/") or default_root
    return media, rf, (qp, mp, ab_qp, ab_mp, eb_qp, eb_mp)


def _wait_for_books(author_id) -> list[dict]:
    """The author's book rows. A just-added author's books populate via an
    async refresh in Chaptarr, so poll briefly."""
    books = []
    for _ in range(8):
        books = _author_books(author_id)
        if books:
            break
        time.sleep(2)
    return books


def add_books(author: str, books: list[dict], fmt: str = "audiobook") -> dict:
    """Monitor + search exactly `books` ([{title, asin}], one author) in one
    media — the "request the rest of this series" handoff.

    The author is reused when Chaptarr already has them (or added, as for a
    single request). Each book is matched to its Chaptarr row for that media;
    ONLY those rows are monitored, and ONE BookSearch covers them all. A book
    Chaptarr doesn't list is reported as such, never widened to the author's
    backlist, and a book Chaptarr already holds a file for is reported and left
    alone. Returns {ok, ref, detail, books: [{ok, detail, available?}]}, one
    result per input book, in order."""
    def _all(res):
        return dict(res, books=[{"ok": False, "detail": res.get("detail", "")} for _ in books])
    if not configured():
        return _all({"ok": False, "detail": NOT_CONNECTED})
    if not books:
        return {"ok": False, "detail": "Nothing to request.", "books": []}
    media, rf, profiles = _media_setup(fmt)
    tag_id = _ensure_tag("stackarr")
    try:
        author_obj, err = _ensure_author(books[0].get("title", ""), author, media, rf, profiles, tag_id)
        if err:
            return _all(err)
        author_id = author_obj.get("id")
        name = author_obj.get("authorName") or author
        _ensure_media_monitored(author_obj, media, rf, profiles[0], profiles[1])
        rows = _wait_for_books(author_id)
        if not rows:
            return _all({"ok": False, "ref": str(author_id),
                         "detail": f"Added “{name}” upstream; its catalogue is still loading — try again shortly."})
        ids, out = [], []
        for bk in books:
            title, asin = bk.get("title", ""), bk.get("asin", "")
            pick, have = pick_row(rows, title, asin, media) if any(r.get("mediaType") for r in rows) else (None, None)
            if have:
                out.append({"ok": True, "available": True, "detail": "Already requested upstream."})
                continue
            hit = pick or _match_book(_for_media(rows, media), title, asin) or _match_book(rows, title, asin)
            if not hit:
                out.append({"ok": False, "detail": f"“{title}” isn't listed upstream for {name} yet."})
                continue
            # same media resolution as a single request (see add_and_search)
            tid = hit["id"] if hit.get("mediaType") == media else _media_edition_id(hit["id"], media)
            row = next((r for r in rows if r.get("id") == tid), hit)
            if _files(row) > 0:
                out.append({"ok": True, "available": True, "detail": "Already requested upstream."})
                continue
            if tid not in ids:
                ids.append(tid)
            out.append({"ok": True, "_id": tid})
        sent = _dispatch(ids, {"name": "BookSearch", "bookIds": ids}) if ids else False
        for o in out:
            if o.pop("_id", None) is not None:
                o["ok"] = sent
                o["detail"] = ("Requested — searching upstream." if sent
                               else f"Added “{name}” upstream, but the search didn't start — retry shortly.")
        n = sum(1 for o in out if o["ok"] and not o.get("available"))
        have = sum(1 for o in out if o.get("available"))
        return {"ok": n + have > 0, "ref": str(author_id), "books": out,
                "detail": (f"Requested {n} of {len(books)} — searching upstream." if n
                           else "Chaptarr already has these." if have
                           else out[0]["detail"])}
    except Exception:
        return _all({"ok": False, "detail": "Couldn't reach upstream — check Settings → Connections."})


def add_and_search(title: str, author: str, asin: str = "", fmt: str = "audiobook",
                   root_folder_override: str = "", series: str = "") -> dict:
    """Ensure the author exists in Chaptarr (tagged 'stackarr'), then monitor +
    search. `fmt` (audiobook | ebook) decides the media type + profiles.

    Chaptarr uses a media-split schema: monitoring lives in
    audiobook/ebookMonitorExisting + audiobook/ebookRootFolderPath, NOT the
    generic monitored/rootFolderPath, and book monitoring is rejected unless the
    author is monitored for that media type. So we (1) add/find the author with
    the media-split fields set, (2) when the request names a specific title, pin
    that book and monitor+search just it (avoids grabbing the whole backlist),
    else fall back to monitoring all the author's books + an author search.
    `series` names a series to get the rest of: its books are monitored and
    searched (the whole author if Chaptarr reports no matching series).
    Returns {ok, ref, detail}. Fails gracefully if Chaptarr is unavailable."""
    if not configured():
        return {"ok": False, "detail": NOT_CONNECTED}
    media, rf, profiles = _media_setup(fmt, root_folder_override)
    qp, mp = profiles[:2]
    tag_id = _ensure_tag("stackarr")
    try:
        author_obj, err = _ensure_author(title, author, media, rf, profiles, tag_id)
        if err:
            return err
        author_id = author_obj.get("id")
        # Gate: the author must be monitored AND set up for this media type before
        # any book can be monitored — critical when reusing an author first added
        # for the other format (its {media} root/profiles would be unset → 400).
        _ensure_media_monitored(author_obj, media, rf, qp, mp)
        books = _wait_for_books(author_id)
        if not books:
            return {"ok": True, "ref": str(author_id),
                    "detail": f"Added “{author_obj.get('authorName')}” upstream; it'll search once its catalogue finishes loading."}
        # A request that names a specific title → grab just that book. An
        # author-level add (title is the author name / no match) → whole backlist.
        in_series = _series_books(_for_media(books, media), series) if series else []
        if in_series:
            ids = [b["id"] for b in in_series]
            ok = _dispatch(ids, {"name": "BookSearch", "bookIds": ids})
            if ok:
                return {"ok": True, "ref": str(author_id),
                        "detail": f"Requested the “{series}” series ({len(ids)} books) — searching upstream."}
            return {"ok": False, "ref": str(author_id),
                    "detail": f"Added “{author_obj.get('authorName')}” upstream, but the search didn't start — retry shortly."}
        # A series Chaptarr doesn't list falls back to the whole author (the
        # documented "get the rest of the series" behaviour).
        is_author_level = bool(series) or _norm(title) == _norm(author)
        target = None
        if not is_author_level:
            pick, have = pick_row(books, title, asin, media) if any(b.get("mediaType") for b in books) else (None, None)
            if have:
                return {"ok": True, "available": True, "ref": str(author_id),
                        "detail": f"“{have.get('title') or title}” is already upstream — nothing to fetch."}
            target = pick or _match_book(_for_media(books, media), title, asin) or _match_book(books, title, asin)
        if target:
            # A title match can land on the wrong-media sibling row; resolve to the
            # row for the requested media so we search/grab the format asked for.
            # Rows tagged with the requested mediaType need no lookup — the
            # release endpoint is a live indexer search, so only fall back to it
            # when the row's media is unknown or the requested row isn't listed.
            target_id = (target["id"] if target.get("mediaType") == media
                         else _media_edition_id(target["id"], media))
            ok = _dispatch([target_id], {"name": "BookSearch", "bookIds": [target_id]})
            name = target.get("title") or title
        elif is_author_level:
            # genuine whole-author add → monitor everything in the requested
            # media + author search (not the other media's rows: nobody asked
            # for that format, and the author isn't set up for it)
            ok = _dispatch([b["id"] for b in _for_media(books, media)],
                           {"name": "AuthorSearch", "authorId": author_id})
            name = author_obj.get("authorName") or author
        else:
            # a specific title was requested but isn't among the author's books — do
            # NOT fall back to grabbing the entire backlist (that also re-monitored
            # books the user had marked read, silently undoing it).
            return {"ok": False, "ref": str(author_id),
                    "detail": f"Added “{author_obj.get('authorName')}” upstream, but couldn't find “{title}” to search."}
        if ok:
            return {"ok": True, "ref": str(author_id),
                    "detail": f"Requested “{name}” — searching upstream."}
        return {"ok": False, "ref": str(author_id),
                "detail": f"Added “{author_obj.get('authorName')}” upstream, but the search didn't start — retry shortly."}
    except Exception:
        return {"ok": False, "detail": "Couldn't reach upstream — check Settings → Connections."}
