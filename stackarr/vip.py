"""Tracker VIP: which requests need it, and re-searching them when it
turns on.

Some trackers mark torrents VIP-only; Chaptarr rejects those for a non-VIP
account ("VIP-only torrent requires VIP membership"). The background
release check (releasecheck.py) files such requests under kind "vip", which
status.py shows as "Needs VIP".

Whether the account is VIP is Chaptarr's setting (Settings → Indexers →
the indexer → "Is VIP"). Stackarr only READS it — GET /api/v1/indexer on
its normal background schedule — and never changes Chaptarr's indexers.
When it flips off → on, every "Needs VIP" request is searched again:
one Chaptarr BookSearch per book, REFLOW_PER_MINUTE at most, with progress
in meta "vip_reflow". Admins can start the same re-search by hand."""
import json
import logging
import time

from . import chaptarr, db

log = logging.getLogger("stackarr.vip")

REFLOW_PER_MINUTE = 3
POLL_MINUTES = 10


def _has_vip_field(ix: dict) -> bool:
    return any(isinstance(f, dict) and f.get("name") == "isVip" for f in ix.get("fields") or [])


def read_indexer() -> dict | None:
    """{vip, user_class, name} from Chaptarr's indexer with an "Is VIP" setting, or None
    if Chaptarr has none (or can't be read). Read-only."""
    try:
        r = chaptarr.requests.get(f"{chaptarr.url()}/api/v1/indexer", headers=chaptarr._h(), timeout=15)
        if not r.ok:
            return None
        for ix in r.json() or []:
            if not _has_vip_field(ix):
                continue
            fields = {f.get("name"): f.get("value") for f in ix.get("fields") or [] if isinstance(f, dict)}
            return {"vip": bool(fields.get("isVip")), "user_class": str(fields.get("userClass") or ""),
                    "name": ix.get("name") or "Indexer"}
    except Exception as e:
        log.debug("chaptarr indexer read failed: %s", e)
    return None


def state() -> dict:
    """The last reading: {vip, user_class, name, at}, or {} if never read."""
    try:
        return json.loads(db.get_meta("indexer_vip", "") or "{}")
    except ValueError:
        return {}


def waiting_ids() -> list[int]:
    """Open requests the release check filed as VIP-only."""
    from . import releasecheck
    with db.conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM requests WHERE status IN ('handed','queued') ORDER BY id")]
    return [i for i in ids if (releasecheck.result(i) or {}).get("kind") == "vip"]


def poll(now: float | None = None) -> dict | None:
    """Read the indexer (at most every POLL_MINUTES); start a reflow when VIP
    turns on. Returns the new reading, or None when it wasn't time / unknown."""
    now = now or time.time()
    prev = state()
    if prev and now - float(prev.get("at") or 0) < POLL_MINUTES * 60:
        return None
    got = read_indexer()
    if got is None:
        return None
    got["at"] = int(now)
    db.set_meta("indexer_vip", json.dumps(got))
    if got["vip"] and prev and not prev.get("vip"):
        n = start_reflow()
        log.info("VIP turned on in Chaptarr — searching %d VIP-only request(s) again", n)
    return got


def start_reflow() -> int:
    """Queue every "Needs VIP" request for one more search."""
    ids = waiting_ids()
    db.set_meta("vip_reflow", json.dumps({"todo": ids, "done": [], "total": len(ids), "started": int(time.time())}))
    return len(ids)


def progress() -> dict:
    try:
        p = json.loads(db.get_meta("vip_reflow", "") or "{}")
    except ValueError:
        p = {}
    return p


def reflow_step(limit: int = REFLOW_PER_MINUTE) -> int:
    """Search up to `limit` queued VIP-only requests again. Each search's
    result is left to Chaptarr; the request's release check is cleared, so
    it looks again (and stops saying "Needs VIP" once there's a grab)."""
    p = progress()
    todo = list(p.get("todo") or [])
    if not todo:
        return 0
    n = 0
    while todo and n < limit:
        rid = todo.pop(0)
        with db.conn() as c:
            r = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if r:
            r = dict(r)
            media = "ebook" if r["format"] == "ebook" else "audiobook"
            ref = r.get("chaptarr_ref") or ""
            aid = int(ref) if str(ref).isdigit() else chaptarr._author_id((r["author"] or "").split(",")[0], {})
            row, have = chaptarr.pick_row(chaptarr._author_books(aid), r["title"], r.get("asin") or "", media) if aid else (None, None)
            if row and not have:
                chaptarr._dispatch([row["id"]], {"name": "BookSearch", "bookIds": [row["id"]]})
            db.set_meta(f"relcheck:{rid}", json.dumps({"kind": "vip_retry", "detail": "", "at": int(time.time())}))
        p.setdefault("done", []).append(rid)
        n += 1
    p["todo"] = todo
    db.set_meta("vip_reflow", json.dumps(p))
    return n
