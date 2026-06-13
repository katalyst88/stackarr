"""Routes: pages + the JSON API the front-end uses. Approval, manual
'already read', 5-star ratings, discover, settings, email preview, health."""
import logging

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from . import (absclient, audible, audnexus, auth, chaptarr, config, db,
               discover, notify, recommend)

log = logging.getLogger("stackarr.routes")
bp = Blueprint("main", __name__)


# ------------------------------------------------------------------ auth ---
@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        u = auth.do_login(request.form.get("username", ""), request.form.get("password", ""))
        if u:
            return redirect(request.args.get("next") or url_for("main.index"))
        error = "Wrong Audiobookshelf username or password"
    return render_template("login.html", error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# ----------------------------------------------------------------- pages ---
@bp.route("/")
@auth.login_required
def index():
    return redirect(url_for("main.suggestions_page"))


@bp.route("/suggestions")
@auth.login_required
def suggestions_page():
    u = auth.current_user()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM suggestions WHERE user_id=? AND status='pending' ORDER BY lane,score DESC",
            (u["id"],))]
        for r in rows:
            r["available"] = _owned(c, r["asin"], r["title"], r["author"])
    lanes = {}
    for r in rows:
        lanes.setdefault(r["lane"], []).append(r)
    lane_titles = {"series": "Series to finish", "author": "More from authors you love",
                   "enjoyed": "Readers also enjoyed", "discover_author": "New authors to discover",
                   "narrator": "Narrators you love", "genre": "More in your favourite genres",
                   "hidden": "Hidden gems", "awards": "Award winners",
                   "short": "Short listens", "epic": "Epic listens",
                   "upcoming": "New & upcoming", "importlist": "From your reading list",
                   "discover": "Popular picks", "foryou": "For you"}
    lane_order = ["series", "author", "enjoyed", "discover_author", "narrator", "genre",
                  "hidden", "awards", "short", "epic", "upcoming", "importlist", "foryou", "discover"]
    lanes = {k: lanes[k] for k in lane_order if k in lanes}
    return render_template("suggestions.html", lanes=lanes, lane_titles=lane_titles)


@bp.route("/discover")
@auth.login_required
def discover_page():
    return render_template("discover.html")


@bp.route("/requests")
@auth.login_required
def requests_page():
    u = auth.current_user()
    admin = u["role"] == "admin"
    wanted = bool(request.args.get("wanted"))            # Sonarr-style: couldn't-grab list
    where = "WHERE status='failed'" if wanted else "WHERE 1=1"
    with db.conn() as c:
        if admin and request.args.get("all"):
            rows = [dict(r) for r in c.execute(f"SELECT r.*, u.username FROM requests r "
                    f"JOIN users u ON u.id=r.user_id {where.replace('status','r.status')} ORDER BY r.id DESC LIMIT 200")]
        else:
            rows = [dict(r) for r in c.execute(f"SELECT * FROM requests {where} AND user_id=? ORDER BY id DESC LIMIT 200", (u["id"],))]
    return render_template("requests.html", requests=rows, admin=admin, wanted=wanted)


@bp.route("/insights")
@auth.login_required
def insights_page():
    u = auth.current_user()
    hist = absclient.listening_history(u["abs_token"])
    stats = absclient.listening_stats(u["abs_token"])
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in c.execute("SELECT item_id,title,author FROM library")}
        ratings = [r["stars"] for r in c.execute("SELECT stars FROM ratings WHERE user_id=?", (u["id"],))]
        req_total = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=?", (u["id"],)).fetchone()["n"]
        req_avail = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=? AND status='available'", (u["id"],)).fetchone()["n"]
    authors, finished, in_prog = {}, 0, 0
    for h in hist:
        if h["finished"]:
            finished += 1
        elif h["progress"] > 0.02:
            in_prog += 1
        m = lib.get(h["item_id"])
        if m and m["author"]:
            a = m["author"].split(",")[0]
            authors[a] = authors.get(a, 0) + 1
    hours = round(stats["total_seconds"] / 3600)
    facts = []
    if hours:
        facts.append(("⏳", f"{hours:,} hours", "listened all-time" +
                      (f" — about {round(hours/24):,} full days" if hours >= 48 else "")))
    if stats["days_listened"]:
        facts.append(("📅", f"{stats['days_listened']:,} days", "with listening activity"))
    if top := (sorted(authors.items(), key=lambda x: x[1], reverse=True)[:1] or [None])[0]:
        facts.append(("✍️", top[0], f"your most-listened author ({top[1]} books)"))
    if ratings:
        facts.append(("⭐", f"{round(sum(ratings)/len(ratings),1)} avg", f"across {len(ratings)} books you've rated"))
    if req_avail:
        facts.append(("📚", f"{req_avail}", "books added to your library via Stackarr"))
    top_authors = sorted(authors.items(), key=lambda x: x[1], reverse=True)[:10]
    return render_template("insights.html", total=len(hist), finished=finished, in_progress=in_prog,
                           hours=hours, req_total=req_total, req_avail=req_avail,
                           facts=facts, top_authors=top_authors)


@bp.route("/book/<asin>")
@auth.login_required
def book_page(asin):
    from . import audnexus
    b = audible.by_asin(asin) or {"asin": asin, "title": "Unknown", "author": ""}
    ax = audnexus.book(asin) or {}
    if ax.get("genres"):
        b["genres"] = ax["genres"]
    if ax.get("series") and not b.get("series"):
        b["series"], b["sequence"] = ax["series"], ax.get("sequence")
    b["state"] = _state_for(asin, b.get("title", ""), b.get("author", ""))
    return render_template("book.html", b=b)


@bp.route("/settings")
@auth.login_required
def settings_page():
    g = db.setting
    return render_template("settings.html",
                           email_configured=notify.email_configured(),
                           email_enabled=db.get_meta("email_enabled", "1") == "1",
                           email_theme=notify.current_theme(),
                           email_frequency=db.get_meta("email_frequency", "immediate"),
                           discord_configured=bool(db.setting("discord_webhook", config.DISCORD_WEBHOOK)),
                           discord_enabled=db.get_meta("discord_enabled", "1") == "1",
                           themes=list(notify.THEMES),
                           interval_hours=db.get_meta("suggest_interval_hours", str(config.SUGGEST_INTERVAL_HOURS)),
                           language=db.get_meta("language", config.TARGET_LANGUAGE),
                           languages=["english","german","spanish","french","italian","dutch","portuguese","japanese","any"],
                           smtp=notify.smtp_settings(),
                           conn={"abs_url": g("abs_url", config.ABS_URL),
                                 "abs_admin_token": g("abs_admin_token", config.ABS_ADMIN_TOKEN),
                                 "chaptarr_url": g("chaptarr_url", config.CHAPTARR_URL),
                                 "chaptarr_api_key": g("chaptarr_api_key", config.CHAPTARR_API_KEY),
                                 "chaptarr_root_folder": g("chaptarr_root_folder", config.CHAPTARR_ROOT_FOLDER),
                                 "chaptarr_quality_profile_id": g("chaptarr_quality_profile_id", str(config.CHAPTARR_QUALITY_PROFILE_ID)),
                                 "chaptarr_metadata_profile_id": g("chaptarr_metadata_profile_id", str(config.CHAPTARR_METADATA_PROFILE_ID)),
                                 "prowlarr_url": g("prowlarr_url", config.PROWLARR_URL),
                                 "prowlarr_api_key": g("prowlarr_api_key", config.PROWLARR_API_KEY)},
                           reading={"goodreads_rss": g("goodreads_rss", config.GOODREADS_RSS),
                                    "hardcover_token": g("hardcover_token", config.HARDCOVER_TOKEN)},
                           log_level=config.LOG_LEVEL,
                           is_admin=auth.current_user()["role"] == "admin")


# ------------------------------------------------------------------- api ---
def _owned(c, asin, title, author) -> bool:
    """True only if the book is really in the library — ASIN match, or exact
    title AND author match. Title-only matching gives false positives on
    common one-word titles (e.g. 'Emergence')."""
    if asin and c.execute("SELECT 1 FROM library WHERE gone_at IS NULL AND asin=? AND asin<>''",
                          (asin,)).fetchone():
        return True
    a = (author or "").split(",")[0].strip().lower()
    if not a:
        return False
    return bool(c.execute(
        "SELECT 1 FROM library WHERE gone_at IS NULL AND lower(title)=? AND lower(author) LIKE ?",
        ((title or "").strip().lower(), f"%{a}%")).fetchone())


def _state_for(asin, title, author):
    with db.conn() as c:
        owned = _owned(c, asin, title, author)
        req = c.execute("SELECT status FROM requests WHERE asin=? AND asin<>'' ORDER BY id DESC LIMIT 1",
                        (asin,)).fetchone()
    return "available" if owned else (req["status"] if req else "none")


@bp.route("/api/discover")
@auth.login_required
def api_discover():
    pg = int(request.args.get("page", "0") or 0)
    books = discover.page(pg)
    for b in books:
        b["state"] = _state_for(b["asin"], b["title"], b["author"])
    return jsonify(books)


@bp.route("/api/suggest")
@auth.login_required
def api_suggest():
    """Typeahead for the top search box — titles/authors/series as you type."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    return jsonify([{"asin": x["asin"], "title": x["title"], "author": x["author"],
                     "series": x.get("series", ""), "cover": x["cover"]}
                    for x in audible.search(q, num=7) if x.get("asin")])


@bp.route("/api/ignore", methods=["POST"])
@auth.login_required
def api_ignore():
    u = auth.current_user()
    body = request.get_json(force=True)
    asin = body.get("asin", "")
    with db.conn() as c:
        if asin:
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", asin, -3, f"ignored: {body.get('title','')}"))
            c.execute("UPDATE suggestions SET status='rejected' WHERE user_id=? AND asin=? AND status='pending'",
                      (u["id"], asin))
    return jsonify({"ok": True})


@bp.route("/api/markread-book", methods=["POST"])
@auth.login_required
def api_markread_book():
    """Mark-as-read by asin from the detail page: positive seed + drop from queue."""
    u = auth.current_user()
    body = request.get_json(force=True)
    asin, title, author = body.get("asin", ""), body.get("title", ""), body.get("author", "")
    with db.conn() as c:
        if author:
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "author", author.split(",")[0], 3, f"already read: {title}"))
        if asin:
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", asin, -1, f"already read: {title}"))
            c.execute("UPDATE suggestions SET status='rejected' WHERE user_id=? AND asin=? AND status='pending'",
                      (u["id"], asin))
    return jsonify({"ok": True, "matched": title})


@bp.route("/api/search")
@auth.login_required
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    books = audible.search(q)
    for b in books:
        b["state"] = _state_for(b["asin"], b["title"], b["author"])
    return jsonify(books)


def _hand_to_chaptarr(user_id, book, source):
    res = chaptarr.add_and_search(book["title"], book.get("author", ""), book.get("asin", ""))
    status = "handed" if res["ok"] else "failed"
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,asin,title,author,cover,status,detail,chaptarr_ref,source) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (user_id, book.get("asin", ""), book["title"], book.get("author", ""),
                   book.get("cover", ""), status, res.get("detail", ""), res.get("ref", ""), source))
    return res


@bp.route("/api/request", methods=["POST"])
@auth.login_required
def api_request():
    u = auth.current_user()
    book = request.get_json(force=True)
    if not book.get("title"):
        return jsonify({"error": "title required"}), 400
    return jsonify(_hand_to_chaptarr(u["id"], book, "manual"))


@bp.route("/api/suggestion/<int:sid>/<verdict>", methods=["POST"])
@auth.login_required
def api_suggestion(sid, verdict):
    u = auth.current_user()
    if verdict not in ("approve", "reject", "read"):
        return jsonify({"error": "bad verdict"}), 400
    with db.conn() as c:
        row = c.execute("SELECT * FROM suggestions WHERE id=? AND user_id=?", (sid, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        new_status = "approved" if verdict == "approve" else "rejected"
        c.execute("UPDATE suggestions SET status=?,decided_at=datetime('now','localtime') WHERE id=?",
                  (new_status, sid))
        if verdict == "reject":
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", row["asin"], -3, f"passed: {row['title']}"))
        elif verdict == "read":
            # treat like the manual 'already read' seed: positive author/series,
            # never re-suggest this title, and drop it from the queue.
            if row.get("author"):
                c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                          (u["id"], "author", row["author"].split(",")[0], 3, f"already read: {row['title']}"))
            if row.get("series"):
                c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                          (u["id"], "series", row["series"], 2, f"already read: {row['title']}"))
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", row["asin"], -1, f"already read: {row['title']}"))
    if verdict == "approve":
        return jsonify(_hand_to_chaptarr(u["id"], row, "suggestion"))
    return jsonify({"status": new_status})


@bp.route("/api/mark-read", methods=["POST"])
@auth.login_required
def api_mark_read():
    """Manually tell Stackarr you've already read a title — positive taste
    seed, no download. Optionally writes finished to ABS if it's in library."""
    u = auth.current_user()
    body = request.get_json(force=True)
    title, authr = body.get("title", "").strip(), body.get("author", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    hit = audible.search(f"{title} {authr}", num=1)
    b = hit[0] if hit else {"asin": "", "title": title, "author": authr}
    with db.conn() as c:
        if b.get("author"):
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "author", b["author"].split(",")[0], 3, f"marked read: {b['title']}"))
        if b.get("series"):
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "series", b["series"], 2, f"marked read: {b['title']}"))
        # also a never-resuggest of the exact title
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                  (u["id"], "asin", b.get("asin", title), -1, f"already read: {b['title']}"))
    return jsonify({"ok": True, "matched": b.get("title", title)})


@bp.route("/api/rate", methods=["POST"])
@auth.login_required
def api_rate():
    """Rate a read book 1-5 stars to sharpen suggestions."""
    u = auth.current_user()
    body = request.get_json(force=True)
    asin, stars = body.get("asin", ""), int(body.get("stars", 0))
    if not asin or not 1 <= stars <= 5:
        return jsonify({"error": "asin and stars 1-5 required"}), 400
    meta = audible.by_asin(asin) or {}
    with db.conn() as c:
        c.execute("INSERT INTO ratings (user_id,asin,title,author,stars) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(user_id,asin) DO UPDATE SET stars=excluded.stars",
                  (u["id"], asin, meta.get("title", ""), meta.get("author", ""), stars))
    return jsonify({"ok": True})


@bp.route("/api/request/<int:rid>/retry", methods=["POST"])
@auth.login_required
def api_retry(rid):
    u = auth.current_user()
    with db.conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=? AND user_id=?", (rid, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        c.execute("DELETE FROM requests WHERE id=?", (rid,))
    return jsonify(_hand_to_chaptarr(u["id"], row, row["source"]))


@bp.route("/api/request/<int:rid>", methods=["DELETE"])
@auth.login_required
def api_request_delete(rid):
    u = auth.current_user()
    with db.conn() as c:
        c.execute("DELETE FROM requests WHERE id=? AND user_id=?", (rid, u["id"]))
    return jsonify({"ok": True})


SETTING_KEYS = {
    "email_theme", "language", "email_frequency", "suggest_interval_hours",
    "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from", "smtp_to",
    "abs_url", "abs_admin_token",
    "chaptarr_url", "chaptarr_api_key", "chaptarr_root_folder",
    "chaptarr_quality_profile_id", "chaptarr_metadata_profile_id",
    "prowlarr_url", "prowlarr_api_key", "goodreads_rss", "hardcover_token",
}
BOOL_KEYS = {"email_enabled", "discord_enabled"}


@bp.route("/api/settings", methods=["POST"])
@auth.login_required
def api_settings():
    body = request.get_json(force=True)
    for k, v in body.items():
        if k in BOOL_KEYS:
            db.set_meta(k, "1" if v else "0")
        elif k in SETTING_KEYS:
            db.set_meta(k, str(v).strip())
    return jsonify({"ok": True})


@bp.route("/api/test/<service>", methods=["POST"])
@auth.admin_required
def api_test(service):
    body = request.get_json(silent=True) or {}
    # let the user test values typed in the form before saving them
    for k, v in body.items():
        if k in SETTING_KEYS:
            db.set_meta(k, str(v).strip())
    try:
        if service == "abs":
            libs = absclient.libraries()
            return jsonify({"ok": True, "detail": f"Connected — {len(libs)} book librar{'y' if len(libs)==1 else 'ies'}"})
        if service == "chaptarr":
            import requests as rq
            r = rq.get(f"{chaptarr.url()}/api/v1/system/status",
                       headers={"X-Api-Key": chaptarr.api_key()}, timeout=15)
            return jsonify({"ok": r.ok, "detail": "Connected" if r.ok else f"HTTP {r.status_code}"})
        if service == "prowlarr":
            from . import prowlarr
            return jsonify(prowlarr.test())
    except Exception as e:
        return jsonify({"ok": False, "detail": str(e)})
    return jsonify({"ok": False, "detail": "unknown service"}), 404


@bp.route("/api/email/preview/<theme>")
@auth.login_required
def api_email_preview(theme):
    if theme not in notify.THEMES:
        return "unknown theme", 404
    sample = [{"title": "The Way of Kings", "author": "Brandon Sanderson", "cover": "",
               "reason": "Next in The Stormlight Archive after “Words of Radiance”"},
              {"title": "Project Hail Mary", "author": "Andy Weir", "cover": "",
               "reason": "Listeners who finished “The Martian” also enjoyed this"}]
    return notify.render_digest(sample, theme=theme, base_url=request.host_url.rstrip("/"))


@bp.route("/api/run-now", methods=["POST"])
@auth.login_required
def api_run_now():
    """Manual history scan — kicks the recommender in the background so the
    loader animation can play, same as first login."""
    import threading
    from . import scheduler
    u = auth.current_user()
    threading.Thread(target=scheduler.run_for_user, args=(u["id"],),
                     kwargs={"force": True}, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@bp.route("/api/suggestions/status")
@auth.login_required
def api_suggestions_status():
    u = auth.current_user()
    with db.conn() as c:
        pending = c.execute("SELECT COUNT(*) n FROM suggestions WHERE user_id=? AND status='pending'",
                            (u["id"],)).fetchone()["n"]
    return jsonify({"pending": pending,
                    "running": db.get_meta(f"running_{u['id']}", "0") == "1"})


LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def _read_logs(min_level: str, limit: int) -> list[str]:
    import os, re
    if not os.path.exists(config.LOG_FILE):
        return []
    thresh = LOG_LEVELS.get(min_level, 20)
    out = []
    with open(config.LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.search(r"\[(\w+)\]", line)
            lvl = LOG_LEVELS.get(m.group(1), 20) if m else 20
            if lvl >= thresh:
                out.append(line.rstrip())
    return out[-limit:]


@bp.route("/api/logs")
@auth.admin_required
def api_logs():
    level = request.args.get("level", "INFO").upper()
    return jsonify({"lines": _read_logs(level, int(request.args.get("limit", "300")))})


@bp.route("/api/logs/download")
@auth.admin_required
def api_logs_download():
    from flask import Response
    level = request.args.get("level", "DEBUG").upper()
    body = "\n".join(_read_logs(level, 200000)) or "(no log entries)"
    return Response(body, mimetype="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=stackarr-{level.lower()}.log"})


@bp.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "app": config.APP_NAME,
                    "version": config.VERSION, "stage": config.RELEASE_STAGE})


# ------------------------------------------------------------------- pwa ---
@bp.route("/manifest.webmanifest")
def manifest():
    base = config.URL_BASE or ""
    body = {
        "name": config.APP_NAME, "short_name": config.APP_NAME,
        "description": "Audiobook recommendations from your listening history",
        "start_url": f"{base}/", "scope": f"{base}/", "display": "standalone",
        "background_color": "#0f172a", "theme_color": "#0f172a",
        "icons": [
            {"src": f"{base}/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            {"src": f"{base}/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": f"{base}/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return jsonify(body)


@bp.route("/sw.js")
def service_worker():
    base = config.URL_BASE or ""
    js = (
        "const C='stackarr-v1';\n"
        f"const SHELL=['{base}/','{base}/static/style.css','{base}/static/app.js','{base}/static/icon.svg'];\n"
        "self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()))});\n"
        "self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});\n"
        "self.addEventListener('fetch',e=>{const u=new URL(e.request.url);"
        "if(e.request.method!=='GET'||u.pathname.includes('/api/')){return}"
        "e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();caches.open(C).then(c=>c.put(e.request,cp));return r}).catch(()=>caches.match(e.request)))});\n"
    )
    from flask import Response
    return Response(js, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": f"{base}/"})
