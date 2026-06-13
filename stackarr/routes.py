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
            owned = c.execute("SELECT 1 FROM library WHERE gone_at IS NULL AND lower(title) LIKE ?",
                              (f"%{(r['title'] or '').lower()[:40]}%",)).fetchone()
            r["available"] = bool(owned)
    lanes = {}
    for r in rows:
        lanes.setdefault(r["lane"], []).append(r)
    lane_titles = {"series": "Next in your series", "foryou": "For you",
                   "narrator": "From narrators you love", "discover": "Popular picks",
                   "importlist": "From your reading list"}
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
    with db.conn() as c:
        if admin and request.args.get("all"):
            rows = [dict(r) for r in c.execute("SELECT r.*, u.username FROM requests r "
                    "JOIN users u ON u.id=r.user_id ORDER BY r.id DESC LIMIT 200")]
        else:
            rows = [dict(r) for r in c.execute("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 200", (u["id"],))]
    return render_template("requests.html", requests=rows, admin=admin)


@bp.route("/insights")
@auth.login_required
def insights_page():
    u = auth.current_user()
    hist = absclient.listening_history(u["abs_token"])
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in c.execute("SELECT item_id,title,author FROM library")}
    authors, finished = {}, 0
    for h in hist:
        if h["finished"]:
            finished += 1
        m = lib.get(h["item_id"])
        if m and m["author"]:
            authors[m["author"].split(",")[0]] = authors.get(m["author"].split(",")[0], 0) + 1
    top_authors = sorted(authors.items(), key=lambda x: x[1], reverse=True)[:10]
    return render_template("insights.html", total=len(hist), finished=finished,
                           top_authors=top_authors)


@bp.route("/settings")
@auth.login_required
def settings_page():
    return render_template("settings.html",
                           email_configured=notify.email_configured(),
                           email_enabled=db.get_meta("email_enabled", "1") == "1",
                           email_theme=notify.current_theme(),
                           discord_configured=bool(config.DISCORD_WEBHOOK),
                           discord_enabled=db.get_meta("discord_enabled", "1") == "1",
                           themes=list(notify.THEMES),
                           interval_hours=db.get_meta("suggest_interval_hours", str(config.SUGGEST_INTERVAL_HOURS)),
                           language=db.get_meta("language", config.TARGET_LANGUAGE),
                           languages=["english","german","spanish","french","italian","dutch","portuguese","japanese","any"],
                           is_admin=auth.current_user()["role"] == "admin")


# ------------------------------------------------------------------- api ---
def _state_for(asin, title, author):
    with db.conn() as c:
        owned = c.execute("SELECT 1 FROM library WHERE gone_at IS NULL AND lower(title) LIKE ?",
                          (f"%{(title or '').lower()[:40]}%",)).fetchone()
        req = c.execute("SELECT status FROM requests WHERE asin=? ORDER BY id DESC LIMIT 1", (asin,)).fetchone()
    return "available" if owned else (req["status"] if req else "none")


@bp.route("/api/discover")
@auth.login_required
def api_discover():
    genre = request.args.get("genre", "")
    books = discover.genre_new([genre] if genre else discover.DEFAULT_GENRES, num_per=8)
    for b in books:
        b["state"] = _state_for(b["asin"], b["title"], b["author"])
    return jsonify(books)


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
    if verdict not in ("approve", "reject"):
        return jsonify({"error": "bad verdict"}), 400
    with db.conn() as c:
        row = c.execute("SELECT * FROM suggestions WHERE id=? AND user_id=?", (sid, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        c.execute("UPDATE suggestions SET status=?,decided_at=datetime('now','localtime') WHERE id=?",
                  ("approved" if verdict == "approve" else "rejected", sid))
        if verdict == "reject":
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", row["asin"], -3, f"passed: {row['title']}"))
    if verdict == "approve":
        # admins auto-hand; regular users' approvals also hand (their approval IS the gate)
        return jsonify(_hand_to_chaptarr(u["id"], row, "suggestion"))
    return jsonify({"status": "rejected"})


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


@bp.route("/api/settings", methods=["POST"])
@auth.login_required
def api_settings():
    body = request.get_json(force=True)
    if "email_enabled" in body:
        db.set_meta("email_enabled", "1" if body["email_enabled"] else "0")
    if "discord_enabled" in body:
        db.set_meta("discord_enabled", "1" if body["discord_enabled"] else "0")
    if body.get("email_theme") in notify.THEMES:
        db.set_meta("email_theme", body["email_theme"])
    if "suggest_interval_hours" in body:
        try:
            db.set_meta("suggest_interval_hours", str(max(int(body["suggest_interval_hours"]), 1)))
        except (ValueError, TypeError):
            pass
    if body.get("language"):
        db.set_meta("language", str(body["language"]).lower())
    return jsonify({"ok": True})


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
