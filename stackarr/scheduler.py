"""Background worker: refreshes the shared library snapshot (detecting
deletions -> negative taste signals), flips requests to 'available' when
they appear in the library, runs the per-user recommender on its interval,
and sends digests. Survives individual failures; daemon thread."""
import logging
import threading
import time

from . import absclient, backends, config, db, formats, notify, recommend

log = logging.getLogger("stackarr.scheduler")


def refresh_library():
    seen = set()
    with db.conn() as c:
        # aggregate the library snapshot across every connected source backend
        # of an *active* format (ABS today; Kavita/Calibre-Web once connected and
        # the format toggle allows them). One source = identical to the old
        # ABS-only behaviour, just now stamped with format/source.
        for backend in backends.sources(formats.mode()):
            try:
                items = backend.library_items()
            except Exception as e:
                log.warning("library refresh failed for %s: %s", backend.id, e)
                continue
            for m in items:
                if not m.get("item_id"):
                    continue
                seen.add(m["item_id"])
                c.execute(
                    "INSERT INTO library (item_id,library_id,title,author,asin,series,series_seq,narrator,format,source,last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime')) "
                    "ON CONFLICT(item_id) DO UPDATE SET title=excluded.title,"
                    "author=excluded.author,asin=excluded.asin,series=excluded.series,"
                    "series_seq=excluded.series_seq,narrator=excluded.narrator,"
                    "format=excluded.format,source=excluded.source,"
                    "last_seen=excluded.last_seen,gone_at=NULL",
                    (m["item_id"], m.get("library_id", ""), m["title"], m["author"], m.get("asin", ""),
                     m.get("series", ""), m.get("series_seq"), m.get("narrator", ""),
                     m.get("format", "audiobook"), m.get("source", "abs")))

        # deletions -> "delete habit" negative signal for every user
        user_ids = [r["id"] for r in c.execute("SELECT id FROM users")]
        for row in c.execute("SELECT item_id,title,author,asin FROM library WHERE gone_at IS NULL"):
            if row["item_id"] in seen:
                continue
            c.execute("UPDATE library SET gone_at=datetime('now','localtime') WHERE item_id=?", (row["item_id"],))
            if row["asin"]:
                for uid in user_ids:
                    c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) "
                              "VALUES (?,?,?,?,?)",
                              (uid, "asin", row["asin"], -5, f"deleted from library: {row['title']}"))
            log.info("library item gone -> negative: %s", row["title"])

        # requests -> available when their book shows up
        newly_available = []
        for r in c.execute("SELECT id,title,author,cover FROM requests WHERE status IN ('queued','handed')"):
            hit = c.execute("SELECT 1 FROM library WHERE gone_at IS NULL AND lower(title) LIKE ? "
                            "AND (?='' OR lower(author) LIKE ?)",
                            (f"%{r['title'].lower()[:40]}%",
                             (r['author'] or '').split(',')[0].lower(),
                             f"%{(r['author'] or '').split(',')[0].lower()}%")).fetchone()
            if hit:
                c.execute("UPDATE requests SET status='available',updated_at=datetime('now','localtime') WHERE id=?", (r["id"],))
                newly_available.append(dict(r))

    # notify outside the DB transaction (each channel self-gates on its config)
    base = db.get_meta("public_url", "")
    for r in newly_available:
        try:
            notify.available(r, base_url=base)
        except Exception as e:
            log.warning("availability notify failed for %s: %s", r.get("title"), e)


def interval_hours() -> int:
    try:
        return max(int(db.get_meta("suggest_interval_hours", str(config.SUGGEST_INTERVAL_HOURS))), 1)
    except ValueError:
        return config.SUGGEST_INTERVAL_HOURS


def run_for_user(user_id: int, force: bool = False) -> int:
    """Run the recommender for one user if due (or forced), notify on new
    picks, and stamp the per-user last-run time. Returns picks added."""
    if not config.SUGGEST_ENABLED:
        return 0
    key = f"suggest_run_{user_id}"
    last = db.get_meta(key)
    if not force and last and (time.time() - float(last)) / 3600 < interval_hours():
        return 0
    db.set_meta(key, str(time.time()))
    with db.conn() as c:
        pending = c.execute("SELECT COUNT(*) n FROM suggestions WHERE user_id=? AND status='pending'",
                            (user_id,)).fetchone()["n"]
    room = max(config.SUGGEST_MAX_PENDING - pending, 0)
    if not room:
        return 0
    db.set_meta(f"running_{user_id}", "1")
    try:
        added = recommend.run(user_id, room)
    except Exception as e:
        log.warning("recommend failed for user %s: %s", user_id, e)
        return 0
    finally:
        db.set_meta(f"running_{user_id}", "0")
    if added:
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT title,author,reason,cover FROM suggestions "
                "WHERE user_id=? AND status='pending' ORDER BY score DESC", (user_id,))]
        notify.suggestion_digest(rows, base_url=db.get_meta("public_url", ""))
    return added


# Auto-add tiers: lane allow-set (None = all lanes) and a per-cycle cap.
AUTO_TIERS = {
    "conservative": ({"series"}, 3),
    "moderate": ({"series", "author", "importlist"}, 5),
    "aggressive": (None, 10),
}


def auto_approve(user_id: int) -> int:
    """Optionally hand high-confidence pending picks to Chaptarr automatically.
    Off by default; tier decides which lanes qualify and the per-cycle cap.
    Records a request + marks approved only on a successful handoff — on
    failure (e.g. Chaptarr's metadata backend down) it leaves the suggestion
    pending and stops, so nothing piles up during an outage."""
    from . import chaptarr
    tier = AUTO_TIERS.get(db.get_meta("auto_add_level", "off"))
    if not tier or not chaptarr.configured():
        return 0
    lanes, cap = tier
    from .routes import _owned          # deferred: avoid import cycle at load
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM suggestions WHERE user_id=? AND status='pending' ORDER BY score DESC",
            (user_id,))]
    added = 0
    for r in rows:
        if added >= cap:
            break
        if r["lane"] == "upcoming":               # never auto-add unreleased titles
            continue
        if lanes is not None and r["lane"] not in lanes:
            continue
        with db.conn() as c:
            if _owned(c, r["asin"], r["title"], r["author"]):
                c.execute("UPDATE suggestions SET status='approved' WHERE id=?", (r["id"],))
                continue
        res = chaptarr.add_and_search(r["title"], r.get("author", ""), r.get("asin", ""))
        if not res.get("ok"):
            log.info("auto-add paused: Chaptarr not adding right now (%s)", res.get("detail", ""))
            break
        with db.conn() as c:
            c.execute("INSERT INTO requests (user_id,asin,title,author,cover,status,detail,chaptarr_ref,source) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (user_id, r.get("asin", ""), r["title"], r.get("author", ""),
                       r.get("cover", ""), "handed", res.get("detail", ""), res.get("ref", ""), "auto"))
            c.execute("UPDATE suggestions SET status='approved',decided_at=datetime('now','localtime') WHERE id=?",
                      (r["id"],))
        added += 1
        log.info("auto-added [%s]: %s — %s", db.get_meta("auto_add_level", "off"), r["title"], r.get("author", ""))
    if added:
        log.info("auto-approve added %d pick(s) for user %s", added, user_id)
    return added


def suggestion_cycle():
    with db.conn() as c:
        users = [r["id"] for r in c.execute("SELECT id FROM users")]
    for uid in users:
        run_for_user(uid)
        try:
            auto_approve(uid)
        except Exception as e:
            log.warning("auto-approve failed for user %s: %s", uid, e)


def _loop():
    while True:
        try:
            refresh_library()
        except Exception as e:
            log.warning("refresh cycle failed: %s", e)
        try:
            suggestion_cycle()
        except Exception as e:
            log.warning("suggestion cycle failed: %s", e)
        time.sleep(config.LIBRARY_REFRESH_MINUTES * 60)


def start():
    threading.Thread(target=_loop, name="stackarr-worker", daemon=True).start()
    log.info("background worker started (every %d min)", config.LIBRARY_REFRESH_MINUTES)
