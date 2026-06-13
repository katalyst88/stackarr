"""Background worker: refreshes the shared library snapshot (detecting
deletions -> negative taste signals), flips requests to 'available' when
they appear in the library, runs the per-user recommender on its interval,
and sends digests. Survives individual failures; daemon thread."""
import logging
import threading
import time

from . import absclient, config, db, notify, recommend

log = logging.getLogger("stackarr.scheduler")


def refresh_library():
    seen = set()
    with db.conn() as c:
        for lib in absclient.libraries():
            try:
                for it in absclient.items(lib["id"]):
                    m = absclient.item_meta(it)
                    if not m["item_id"]:
                        continue
                    seen.add(m["item_id"])
                    c.execute(
                        "INSERT INTO library (item_id,library_id,title,author,asin,last_seen) "
                        "VALUES (?,?,?,?,?,datetime('now','localtime')) "
                        "ON CONFLICT(item_id) DO UPDATE SET title=excluded.title,"
                        "author=excluded.author,asin=excluded.asin,"
                        "last_seen=excluded.last_seen,gone_at=NULL",
                        (m["item_id"], lib["id"], m["title"], m["author"], m["asin"]))
            except Exception as e:
                log.warning("library refresh failed for %s: %s", lib.get("name"), e)

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
        for r in c.execute("SELECT id,title,author FROM requests WHERE status IN ('queued','handed')"):
            hit = c.execute("SELECT 1 FROM library WHERE gone_at IS NULL AND lower(title) LIKE ? "
                            "AND (?='' OR lower(author) LIKE ?)",
                            (f"%{r['title'].lower()[:40]}%",
                             (r['author'] or '').split(',')[0].lower(),
                             f"%{(r['author'] or '').split(',')[0].lower()}%")).fetchone()
            if hit:
                c.execute("UPDATE requests SET status='available',updated_at=datetime('now','localtime') WHERE id=?", (r["id"],))


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


def suggestion_cycle():
    with db.conn() as c:
        users = [r["id"] for r in c.execute("SELECT id FROM users")]
    for uid in users:
        run_for_user(uid)


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
