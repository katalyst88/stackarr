"""Why hasn't a request downloaded? One background look at Chaptarr's releases.

A request Chaptarr can never fill — the only releases are VIP-only on the tracker,
or there are none — used to read "Requested" for a week and then "Not found
yet". Chaptarr knows sooner: its release list for the book carries each
release's rejections ("VIP-only torrent requires VIP membership").

Chaptarr's /api/v1/release runs a live indexer search, so it is never called
on a page request or in a loop. A background thread looks at ONE request at
a time, at most once a minute, only for requests that were handed over at
least CHECK_AFTER_MIN minutes ago and still aren't in Chaptarr's queue, and
each request at most once per RECHECK_HOURS. The answer is kept in meta
("relcheck:<request id>") and status.py turns it into plain words:

  vip       Not available — VIP-only on the tracker
  none      Not found — no releases on your indexers
  rejected  Not found — every release was rejected: <reasons>
  ok        (nothing to say: there are usable releases, Chaptarr will grab)

Settings → General → "Check stuck requests" turns it off (meta
"release_check" = "0")."""
import json
import logging
import time

from . import chaptarr, db, tz

log = logging.getLogger("stackarr.releasecheck")

CHECK_AFTER_MIN = 15          # give Chaptarr's own search time to grab first
RECHECK_HOURS = 24
PAUSE_SECONDS = 60            # one indexer search per minute, at most
OPEN = ("handed", "queued")


def enabled() -> bool:
    return db.get_meta("release_check", "1") == "1" and chaptarr.configured()


def result(request_id) -> dict | None:
    raw = db.get_meta(f"relcheck:{request_id}", "")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _reasons(rel: dict) -> list[str]:
    out = []
    for x in rel.get("rejections") or []:
        if isinstance(x, dict):
            x = x.get("reason") or x.get("message") or ""
        x = str(x or "").strip()
        if x:
            out.append(x)
    return out


def classify(payload) -> dict:
    """{kind, detail} from Chaptarr's release answer (a list of releases, or a
    dict carrying "releases")."""
    rels = payload.get("releases") if isinstance(payload, dict) else payload
    rels = [r for r in (rels or []) if isinstance(r, dict)]
    if not rels:
        return {"kind": "none", "detail": "No releases found on your indexers"}
    usable = [r for r in rels if not r.get("rejected") and not _reasons(r)]
    if usable:
        return {"kind": "ok", "detail": ""}
    reasons = [x for r in rels for x in _reasons(r)]
    if any("vip" in x.lower() for x in reasons):
        return {"kind": "vip", "detail": "VIP-only on the tracker (needs VIP membership)"}
    uniq = list(dict.fromkeys(reasons))[:2]
    return {"kind": "rejected", "detail": "Every release was rejected" + (": " + "; ".join(uniq) if uniq else "")}


def candidates(limit: int = 1) -> list[dict]:
    """Open requests worth a look: handed over long enough ago, not checked
    recently, not in Chaptarr's queue right now. Oldest check first."""
    from . import status
    cutoff = time.time() - CHECK_AFTER_MIN * 60
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, title, author, asin, format, chaptarr_ref, created_at, updated_at FROM requests "
            "WHERE status IN ('handed','queued') AND title NOT LIKE 'Full series: %' "
            "AND title NOT LIKE 'All books by %' ORDER BY id")]
    q = status.live_queue()
    out = []
    for r in rows:
        when = tz.from_db(r.get("updated_at") or r.get("created_at"))
        if when and when.timestamp() > cutoff:
            continue
        prev = result(r["id"])
        wait = (1 if (prev or {}).get("kind") == "error" else RECHECK_HOURS) * 3600
        if prev and time.time() - float(prev.get("at") or 0) < wait:
            continue
        media = "ebook" if r["format"] == "ebook" else "audiobook"
        if status._queue_hit(q, r["title"], r["author"], r.get("asin") or "", media):
            continue
        out.append(dict(r, media=media, _prev=float((prev or {}).get("at") or 0)))
    out.sort(key=lambda r: r["_prev"])
    return out[:limit]


def check(r: dict) -> dict | None:
    """Look at one request's releases in Chaptarr and remember the answer."""
    cache: dict = {}
    ref = r.get("chaptarr_ref") or ""
    author_id = int(ref) if str(ref).isdigit() else chaptarr._author_id((r.get("author") or "").split(",")[0], cache)
    if not author_id:
        return None
    rows = chaptarr._author_books(author_id)
    row, have = chaptarr.pick_row(rows, r["title"], r.get("asin") or "", r["media"])
    if have:
        out = {"kind": "ok", "detail": ""}            # Chaptarr has it; the next refresh flips it
    elif not row:
        out = {"kind": "none", "detail": "Chaptarr doesn't list this book for the author"}
    else:
        try:
            resp = chaptarr.requests.get(f"{chaptarr.url()}/api/v1/release", headers=chaptarr._h(),
                                         params={"bookId": row["id"]}, timeout=120)
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status_code}")
            out = classify(resp.json())
        except Exception as e:
            log.info("release check for %r failed: %s", r["title"], e)
            # remember the attempt so a failing Chaptarr isn't asked every minute
            out = {"kind": "error", "detail": ""}
    out["at"] = int(time.time())
    db.set_meta(f"relcheck:{r['id']}", json.dumps(out))
    if out["kind"] in ("vip", "none", "rejected"):
        log.info("release check: %s (%s) — %s", r["title"], r["media"], out["detail"])
    return out


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
            run_once()
        except Exception as e:
            log.debug("release check worker error: %s", e)
        time.sleep(PAUSE_SECONDS)
