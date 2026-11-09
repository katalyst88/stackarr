"""Why hasn't a request downloaded? One background look at Chaptarr's releases.

A request Chaptarr can't fill (every release is for a tracker's VIP members,
the account's ratio is too low, there's no ebook at all) used to read
"Requested" for a week and then "Not found yet". Chaptarr knows sooner: its
release list for the book carries each release's rejections. needs.py turns
those into "what this download needs" in plain words.

Chaptarr's /api/v1/release runs a live indexer search, so it is never called
on a page request or in a loop. A background thread looks at ONE request at
a time, at most once a minute, only for requests that were handed over at
least CHECK_AFTER_MIN minutes ago and still aren't in Chaptarr's queue. The
answer is kept in meta ("relcheck:<request id>"):

  needs   {needs: [...]} — no usable release, and why (needs.classify)
  none    {needs: [no_release]} — no release in this format at all
  retry   what it needed has cleared; Stackarr asked Chaptarr to search again
  ok      there are usable releases; Chaptarr will grab
  error   Chaptarr didn't answer (tried again in an hour)

Automatic retry: each need says how soon it might clear (needs.retry_hours —
an indexer outage hourly, an account limit daily). And when Chaptarr's
indexer or download-client settings change (VIP switched on, a seedbox
registered, a new client), every request held by an account or setup need
is looked at again straight away (settings_changed()). When a look finds
usable releases where there were none, Stackarr asks Chaptarr to search
(BookSearch) once — the request then moves on by itself.

Settings → General → "Check stuck requests" turns it off (meta
"release_check" = "0")."""
import datetime
import hashlib
import json
import logging
import time

from . import chaptarr, db, needs, tz

log = logging.getLogger("stackarr.releasecheck")

CHECK_AFTER_MIN = 15          # give Chaptarr's own search time to grab first
PAUSE_SECONDS = 60            # one indexer search per minute, at most
SETTINGS_POLL_MINUTES = 10    # how often Chaptarr's indexer / client settings are compared
OPEN = ("handed", "queued")
HELD = ("needs", "none")      # kinds that hold a request back


def enabled() -> bool:
    return db.get_meta("release_check", "1") == "1" and chaptarr.configured()


def result(request_id) -> dict | None:
    """The last look at a request, in today's shape (older stored kinds are
    read as their needs: "vip" / "rejected" → needs, "vip_retry" → retry)."""
    raw = db.get_meta(f"relcheck:{request_id}", "")
    try:
        r = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(r, dict):
        return None
    kind = r.get("kind")
    if kind == "vip":
        r = dict(r, kind="needs", needs=[needs._need(needs.rule("vip"))])
    elif kind == "rejected":
        r = dict(r, kind="needs", needs=needs.classify([(r.get("detail") or "").split(": ", 1)[-1]]))
    elif kind == "vip_retry":
        r = dict(r, kind="retry")
    return r


def _store(request_id, out: dict):
    db.set_meta(f"relcheck:{request_id}", json.dumps(out))


def _reasons(rel: dict) -> list[str]:
    out = []
    for x in rel.get("rejections") or []:
        if isinstance(x, dict):
            x = x.get("reason") or x.get("message") or ""
        x = str(x or "").strip()
        if x:
            out.append(x)
    return out


def classify(payload, media: str = "audiobook") -> dict:
    """{kind, needs} from Chaptarr's release answer (a list of releases, or a
    dict carrying "releases")."""
    rels = payload.get("releases") if isinstance(payload, dict) else payload
    rels = [r for r in (rels or []) if isinstance(r, dict)]
    if not rels:
        return {"kind": "none", "needs": [needs.no_release(media)]}
    usable = [r for r in rels if not r.get("rejected") and not _reasons(r)]
    if usable:
        return {"kind": "ok", "needs": []}
    found = needs.classify([x for r in rels for x in _reasons(r)])
    if not found:                                   # rejected, but no words why
        found = needs.classify(["Every release was rejected"])
    return {"kind": "needs", "needs": found}


def _indexers_failing() -> bool:
    """Does Chaptarr have an indexer switched off for failures right now?
    (Then "no releases" may just mean it couldn't ask.)"""
    try:
        r = chaptarr.requests.get(f"{chaptarr.url()}/api/v1/indexerstatus", headers=chaptarr._h(), timeout=15)
        if not r.ok:
            return False
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return any(str(x.get("disabledTill") or "")[:19] > now for x in (r.json() or []) if isinstance(x, dict))
    except Exception:
        return False


def _wait_seconds(prev: dict) -> float:
    kind = prev.get("kind")
    if kind == "error":
        return 3600
    if kind == "retry":
        return CHECK_AFTER_MIN * 60
    if kind in HELD:
        return needs.retry_hours(prev.get("needs") or []) * 3600
    return 24 * 3600


def candidates(limit: int = 1) -> list[dict]:
    """Open requests worth a look: handed over long enough ago, due for a look
    (see _wait_seconds), not in Chaptarr's queue right now. Oldest look first."""
    from . import status
    cutoff = time.time() - CHECK_AFTER_MIN * 60
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, title, author, asin, format, chaptarr_ref, created_at, updated_at FROM requests "
            "WHERE status IN ('handed','queued') AND title NOT LIKE 'Full series: %' "
            "AND title NOT LIKE 'All books by %' ORDER BY id")]
    q = None
    out = []
    for r in rows:
        when = tz.from_db(r.get("updated_at") or r.get("created_at"))
        if when and when.timestamp() > cutoff:
            continue
        prev = result(r["id"])
        if prev and time.time() - float(prev.get("at") or 0) < _wait_seconds(prev):
            continue
        media = "ebook" if r["format"] == "ebook" else "audiobook"
        if q is None:
            q = status.live_queue()
        if status._queue_hit(q, r["title"], r["author"], r.get("asin") or "", media):
            continue
        out.append(dict(r, media=media, _prev=float((prev or {}).get("at") or 0), _kind=(prev or {}).get("kind")))
    out.sort(key=lambda r: r["_prev"])
    return out[:limit]


def _author_id(r: dict) -> int | None:
    ref = r.get("chaptarr_ref") or ""
    return int(ref) if str(ref).isdigit() else chaptarr._author_id((r.get("author") or "").split(",")[0], {})


def check(r: dict) -> dict | None:
    """Look at one request's releases in Chaptarr and remember the answer.
    If it was held and now has usable releases, ask Chaptarr to search."""
    media = r.get("media") or ("ebook" if r.get("format") == "ebook" else "audiobook")
    prev = result(r["id"]) or {}
    author_id = _author_id(r)
    if not author_id:
        return None
    try:
        # not chaptarr._author_books(): that reads a failed call as "no books",
        # and an unreachable Chaptarr must not read as "doesn't list this book"
        resp = chaptarr.requests.get(f"{chaptarr.url()}/api/v1/book", headers=chaptarr._h(),
                                     params={"authorId": author_id}, timeout=40)
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}")
        books = resp.json() or []
    except Exception as e:
        log.info("release check for %r: Chaptarr's book list failed: %s", r["title"], e)
        out = {"kind": "error", "needs": [], "at": int(time.time())}
        _store(r["id"], out)
        return out
    row, have = chaptarr.pick_row(books, r["title"], r.get("asin") or "", media)
    if have:
        out = {"kind": "ok", "needs": []}            # Chaptarr has it; the next refresh flips it
    elif not row:
        out = {"kind": "needs", "needs": needs.classify(["Chaptarr doesn't list this book for the author"])}
    else:
        try:
            resp = chaptarr.requests.get(f"{chaptarr.url()}/api/v1/release", headers=chaptarr._h(),
                                         params={"bookId": row["id"]}, timeout=120)
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status_code}")
            out = classify(resp.json(), media)
            if out["kind"] == "none" and _indexers_failing():
                out["needs"] = [needs._need(needs.rule("indexer_down"))] + out["needs"]
        except Exception as e:
            log.info("release check for %r failed: %s", r["title"], e)
            # remember the attempt so a failing Chaptarr isn't asked every minute
            out = {"kind": "error", "needs": []}
        if out["kind"] == "ok" and prev.get("kind") in HELD:
            # what it needed has cleared (VIP on, ratio back, a release appeared):
            # one search, and the request moves on by itself
            chaptarr._dispatch([row["id"]], {"name": "BookSearch", "bookIds": [row["id"]]})
            was = needs.badge(prev.get("needs") or [])
            out = {"kind": "retry", "needs": [], "detail": f"Searching again — {was.lower()} has cleared"
                   if was else "Searching again"}
            log.info("release check: %s (%s) — %s", r["title"], media, out["detail"])
    out["at"] = int(time.time())
    _store(r["id"], out)
    if out["kind"] in HELD:
        log.info("release check: %s (%s) — %s", r["title"], media,
                 "; ".join(n.get("raw") or n["label"] for n in out["needs"]))
    return out


def held_ids() -> list[int]:
    """Open requests the release check found held back."""
    with db.conn() as c:
        ids = [x["id"] for x in c.execute("SELECT id FROM requests WHERE status IN ('handed','queued') ORDER BY id")]
    return [i for i in ids if (result(i) or {}).get("kind") in HELD]


def recheck(only_settings: bool = False) -> int:
    """Make held requests due for a look now (the worker still takes one a
    minute). only_settings: just those a settings change could clear."""
    n = 0
    for i in held_ids():
        rc = result(i)
        if only_settings and not needs.cleared_by_settings(rc.get("needs") or []):
            continue
        _store(i, dict(rc, at=0))
        n += 1
    return n


def _fingerprint() -> str | None:
    """A hash of Chaptarr's indexer + download-client settings (read-only;
    only the hash is kept), or None when Chaptarr can't be read."""
    parts = []
    try:
        for path in ("/api/v1/indexer", "/api/v1/downloadclient"):
            r = chaptarr.requests.get(f"{chaptarr.url()}{path}", headers=chaptarr._h(), timeout=15)
            if not r.ok:
                return None
            for x in r.json() or []:
                if not isinstance(x, dict):
                    continue
                fields = sorted((str(f.get("name")), json.dumps(f.get("value"), sort_keys=True))
                                for f in x.get("fields") or [] if isinstance(f, dict))
                parts.append([path, x.get("id"), x.get("enable", x.get("enableRss")), x.get("priority"), fields])
    except Exception as e:
        log.debug("chaptarr settings read failed: %s", e)
        return None
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def settings_changed(now: float | None = None) -> int | None:
    """Compare Chaptarr's indexer / client settings with the last look (at most
    every SETTINGS_POLL_MINUTES); on a change, re-check every request an
    account or setup need holds. Returns how many, or None when it didn't look."""
    now = now or time.time()
    try:
        last = json.loads(db.get_meta("needs_settings", "") or "{}")
    except ValueError:
        last = {}
    if last and now - float(last.get("at") or 0) < SETTINGS_POLL_MINUTES * 60:
        return None
    fp = _fingerprint()
    if fp is None:
        return None
    db.set_meta("needs_settings", json.dumps({"fp": fp, "at": int(now)}))
    if not last.get("fp") or last["fp"] == fp:
        return 0
    n = recheck(only_settings=True)
    if n:
        log.info("Chaptarr's indexer or download-client settings changed — looking at %d held request(s) again", n)
    return n


def run_once() -> int:
    """Check at most one request. Returns how many were checked."""
    if not enabled():
        return 0
    n = 0
    for r in candidates(1):
        check(r)
        n += 1
    return n


def worker():
    while True:
        try:
            if enabled():
                settings_changed()
            run_once()
        except Exception as e:
            log.debug("release check worker error: %s", e)
        time.sleep(PAUSE_SECONDS)
