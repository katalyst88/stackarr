"""Routes: pages + the JSON API the front-end uses. Approval, manual
'already read', 5-star ratings, discover, settings, email preview, health."""
import logging
import re

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from . import (absclient, audible, audnexus, auth, chaptarr, config, db,
               discover, formats, notify)

log = logging.getLogger("stackarr.routes")
bp = Blueprint("main", __name__)


# ------------------------------------------------------------------ auth ---
_LOGIN_FAILS = {}          # ip -> (count, first_ts); brute-force throttle
_LOCK_AFTER = 5
_LOCK_WINDOW = 900         # 15 min


@bp.route("/login", methods=["GET", "POST"])
def login():
    import time
    error = ""
    ip = request.remote_addr or "?"
    if request.method == "POST":
        cnt, first = _LOGIN_FAILS.get(ip, (0, 0.0))
        if cnt >= _LOCK_AFTER and (time.time() - first) < _LOCK_WINDOW:
            return render_template("login.html", error="Too many attempts — try again in a few minutes."), 429
        if (time.time() - first) >= _LOCK_WINDOW:
            cnt, first = 0, time.time()
        u = auth.do_login(request.form.get("username", ""), request.form.get("password", ""))
        if u:
            _LOGIN_FAILS.pop(ip, None)
            return redirect(request.args.get("next") or url_for("main.index"))
        _LOGIN_FAILS[ip] = (cnt + 1, first or time.time())
        error = "Wrong Audiobookshelf username or password"
    return render_template("login.html", error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# ----------------------------------------------------------------- pages ---
ONBOARD_THRESHOLD = 5      # below this many ratings, nudge the quick-rate flow


def _onboarding_books(u, limit=12):
    """Finished-in-ABS books the user hasn't rated yet — the seed set for the
    quick-rate onboarding card. Most have no ASIN, so we key on rating_key."""
    try:
        hist = absclient.listening_history(u["abs_token"])
    except Exception:
        return []
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in
               c.execute("SELECT item_id,title,author,asin FROM library")}
        rated = {r["asin"] for r in c.execute(
            "SELECT asin FROM ratings WHERE user_id=? AND asin<>''", (u["id"],))}
        hidden = {r["value"] for r in c.execute(
            "SELECT value FROM signals WHERE user_id=? AND kind='hist_hidden'", (u["id"],))}
    out, seen = [], set()
    for h in hist:
        if not h.get("finished"):
            continue
        m = lib.get(h["item_id"]) or {}
        title, author, asin = m.get("title", ""), m.get("author", ""), (m.get("asin") or "").strip()
        rk = db.rating_key(asin, title, author)
        if not title or rk in rated or rk in hidden or rk in seen:
            continue
        seen.add(rk)
        out.append({"rkey": rk, "title": title, "author": author,
                    "cover": url_for("main.cover", item_id=h["item_id"])})
        if len(out) >= limit:
            break
    return out


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
    # authors to feature as browse cards — only SUGGESTED authors (new discoveries /
    # readers-also-enjoyed), NOT the "authors you love" backlist lane.
    seen_a, rec_authors = set(), []
    for r in sorted(rows, key=lambda x: x["score"], reverse=True):
        if r["lane"] == "author":
            continue
        a = (r["author"] or "").split(",")[0].strip()
        if a and a.lower() not in seen_a:
            seen_a.add(a.lower())
            rec_authors.append(a)
    rec_authors = rec_authors[:14]
    from . import discover
    # dashboard rows (only render if content exists)
    try:
        recently_added = absclient.recent_added(14)
    except Exception:
        recently_added = []
    with db.conn() as c:
        recent_requests = [dict(r) for r in c.execute(
            "SELECT title, author, cover, status, asin FROM requests WHERE user_id=? "
            "ORDER BY id DESC LIMIT 14", (u["id"],))]
        rated_count = c.execute("SELECT COUNT(*) FROM ratings WHERE user_id=?", (u["id"],)).fetchone()[0]
        onboard_off = bool(c.execute(
            "SELECT 1 FROM signals WHERE user_id=? AND kind='onboard_dismissed'", (u["id"],)).fetchone())
    # quick-rate onboarding: only when taste is thin and not dismissed
    onboard_books = [] if (rated_count >= ONBOARD_THRESHOLD or onboard_off) else _onboarding_books(u)
    recently_rated = db.recent_ratings(14)
    return render_template("suggestions.html", lanes=lanes, lane_titles=lane_titles,
                           genres=discover.DEFAULT_GENRES, rec_authors=rec_authors,
                           abs_base=absclient.abs_url(),
                           recently_added=recently_added, recent_requests=recent_requests,
                           recently_rated=recently_rated,
                           onboard_books=onboard_books, onboard_target=ONBOARD_THRESHOLD)


@bp.route("/lane/<lane>")
@auth.login_required
def lane_grid(lane):
    u = auth.current_user()
    titles = {"series": "Series to finish", "author": "More from authors you love",
              "enjoyed": "Readers also enjoyed", "discover_author": "New authors to discover",
              "narrator": "Narrators you love", "genre": "More in your favourite genres",
              "hidden": "Hidden gems", "awards": "Award winners", "short": "Short listens",
              "epic": "Epic listens", "upcoming": "New & upcoming", "importlist": "From your reading list",
              "discover": "Popular picks", "foryou": "For you"}
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM suggestions WHERE user_id=? AND lane=? AND status='pending' ORDER BY score DESC",
            (u["id"], lane))]
        for r in rows:
            r["available"] = _owned(c, r["asin"], r["title"], r["author"])
    return render_template("lane.html", rows=rows, lane=lane, title=titles.get(lane, lane))


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
            a = m["author"].split(",")[0].split(" - ")[0].strip()   # drop "- illustrator/translator" noise
            if a:
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


@bp.route("/history")
@auth.login_required
def history_page():
    """Books you've read — finished in Audiobookshelf, rated, or marked read —
    each with a 1-5 star rating control that feeds the recommender."""
    u = auth.current_user()
    hist = absclient.listening_history(u["abs_token"])
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in
               c.execute("SELECT item_id,title,author,asin FROM library")}
        rated = {r["asin"]: dict(r) for r in c.execute(
            "SELECT asin,title,author,stars FROM ratings WHERE user_id=? AND asin<>''", (u["id"],))}
        read_sig = c.execute(
            "SELECT value,why FROM signals WHERE user_id=? AND kind='asin' "
            "AND (why LIKE 'already read:%' OR why LIKE 'marked read:%')", (u["id"],)).fetchall()
        # books the user explicitly removed from history (stays gone even though
        # it's still finished in Audiobookshelf)
        hidden = {r["value"] for r in c.execute(
            "SELECT value FROM signals WHERE user_id=? AND kind='hist_hidden'", (u["id"],))}

    books, seen = [], set()

    def key(asin, title):
        return (asin or "").lower() or (title or "").strip().lower()

    def add(asin, title, author, cover, when):
        k = key(asin, title)
        if not k or k in seen:
            return
        seen.add(k)
        rk = db.rating_key(asin, title, author)
        if rk in hidden:
            return
        books.append({"asin": asin or "", "rkey": rk, "title": title or "Untitled",
                      "author": author or "", "cover": cover,
                      "stars": (rated.get(rk) or {}).get("stars", 0), "when": when})

    # 1) finished in Audiobookshelf (the real listening history, with covers)
    for h in hist:
        if not h["finished"]:
            continue
        m = lib.get(h["item_id"]) or {}
        add((m.get("asin") or "").strip(), m.get("title", ""), m.get("author", ""),
            url_for("main.cover", item_id=h["item_id"]), h["last_update"])
    # 2) books you've rated that aren't already listed (stored key is either a
    #    real ASIN or a "t-…" slug; only the former is a usable book ASIN)
    for stored, r in rated.items():
        add(stored if stored.startswith("B0") else "", r["title"], r["author"], "", 0)
    # 3) titles you marked read in Stackarr
    for s in read_sig:
        val = s["value"] or ""
        title = s["why"].split(":", 1)[1].strip() if ":" in s["why"] else val
        add(val if val.startswith("B0") else "", title, "", "", 0)

    # optionally drop already-rated books entirely (a "rate it and it's gone"
    # workflow) — otherwise keep them, sunk to the bottom.
    hide_rated = db.get_meta("hide_rated_history", "0") == "1"
    if hide_rated:
        books = [b for b in books if not b["stars"]]
    # unrated float to the top (the to-do pile); rated sink to the bottom.
    # within each group, most-recent first.
    books.sort(key=lambda b: (b["stars"] > 0, -(b["when"] or 0)))
    rated_n = sum(1 for b in books if b["stars"])
    return render_template("history.html", books=books, rated_n=rated_n, hide_rated=hide_rated)


@bp.route("/series")
@auth.login_required
def series_page():
    """Up Next: series you're collecting, how far you are, and the next book
    (with its state) — built from your library + the engine's series picks."""
    u = auth.current_user()
    with db.conn() as c:
        libr = [dict(r) for r in c.execute(
            "SELECT title,author,series,series_seq,asin FROM library "
            "WHERE gone_at IS NULL AND series<>'' ORDER BY series, series_seq")]
        sugg = [dict(r) for r in c.execute(
            "SELECT id,title,author,series,asin,cover,reason FROM suggestions "
            "WHERE user_id=? AND lane='series' AND status='pending' ORDER BY score DESC", (u["id"],))]
        reqs = [dict(r) for r in c.execute(
            "SELECT title,status FROM requests WHERE user_id=?", (u["id"],))]

    def norm(s):
        return (s or "").strip().lower()

    next_by_series = {}
    for s in sugg:
        next_by_series.setdefault(norm(s["series"]), s)   # highest-scored next book per series

    def req_status(title):
        nt = norm(title)
        for rq in reqs:
            rt = norm(rq["title"])
            if rt and nt and (rt[:30] in nt or nt[:30] in rt):
                return rq["status"]
        return None

    groups = {}
    for b in libr:
        groups.setdefault(b["series"], []).append(b)

    cards = []
    for name, books in groups.items():
        books.sort(key=lambda b: b["series_seq"] if b["series_seq"] is not None else 0)
        seqs = [b["series_seq"] for b in books if b["series_seq"] is not None]
        nxt = next_by_series.get(norm(name))
        cards.append({"name": name, "owned": len(books),
                      "highest": max(seqs) if seqs else None, "books": books,
                      "next": nxt, "next_status": req_status(nxt["title"]) if nxt else None})
    # "Up Next" is for series you're actually collecting — 2+ books, or one with
    # a next pick queued. Drops single-book noise and mislabelled one-offs.
    cards = [x for x in cards if x["owned"] >= 2 or x["next"]]
    # most-invested series first; then alphabetical
    cards.sort(key=lambda x: (-x["owned"], x["name"].lower()))
    have_next = sum(1 for c in cards if c["next"])
    return render_template("series.html", series=cards, have_next=have_next)


@bp.route("/taste")
@auth.login_required
def taste_page():
    """See and undo everything that shapes your recommendations: ratings,
    did-not-finish, passed/ignored, already-read seeds, and removed books."""
    u = auth.current_user()
    with db.conn() as c:
        ratings = [dict(r) for r in c.execute(
            "SELECT asin,title,author,stars FROM ratings WHERE user_id=? ORDER BY stars DESC, title",
            (u["id"],))]
        sigs = [dict(r) for r in c.execute(
            "SELECT id,kind,value,weight,why FROM signals WHERE user_id=? ORDER BY id DESC", (u["id"],))]

    def labelled(s):
        why = s.get("why") or ""
        s = dict(s)
        s["label"] = why.split(":", 1)[1].strip() if ":" in why else (s.get("value") or "")
        return s

    def why_is(s, *prefixes):
        return s["kind"] == "asin" and any((s["why"] or "").startswith(p) for p in prefixes)

    passed = [labelled(s) for s in sigs if why_is(s, "passed:")]
    dnf = [labelled(s) for s in sigs if (s["why"] or "").startswith("dnf:")]
    readseed = [labelled(s) for s in sigs if why_is(s, "already read:", "marked read:")]
    removed = [labelled(s) for s in sigs if s["kind"] == "hist_hidden"]
    return render_template("taste.html", ratings=ratings, passed=passed, dnf=dnf,
                           readseed=readseed, removed=removed)


@bp.route("/book/<path:asin>")
@auth.login_required
def book_page(asin):
    # ebook ids are "gb:…"/"ol:…" and resolve via the ebook catalogue; real
    # audiobook ASINs go to Audible + Audnexus as before.
    if asin.startswith(("gb:", "ol:")):
        from . import ebookmeta
        b = ebookmeta.by_id(asin) or {"title": "Unknown", "author": "", "format": "ebook"}
        b["format"] = "ebook"
        b["asin"] = asin            # keep the gb:/ol: id as the page identity for actions
    else:
        b = audible.by_asin(asin) or {"asin": asin, "title": "Unknown", "author": ""}
        b.setdefault("format", "audiobook")
        ax = audnexus.book(asin) or {}
        if ax.get("genres"):
            b["genres"] = ax["genres"]
        if ax.get("series") and not b.get("series"):
            b["series"], b["sequence"] = ax["series"], ax.get("sequence")
    b["state"] = _state_for(asin, b.get("title", ""), b.get("author", ""))
    u = auth.current_user()
    key = db.rating_key(asin if not asin.startswith(("gb:", "ol:")) else "",
                        b.get("title", ""), b.get("author", "")) if asin.startswith(("gb:", "ol:")) else asin
    with db.conn() as c:
        req = c.execute("SELECT status, detail FROM requests WHERE asin=? AND asin<>'' ORDER BY id DESC LIMIT 1",
                        (asin,)).fetchone()
        my = c.execute("SELECT stars, review FROM ratings WHERE user_id=? AND asin=?",
                       (u["id"], key)).fetchone()
    b["req_detail"] = (req["detail"] if req else "") or ""
    return render_template("book.html", b=b, rate_key=key,
                           community=db.community_rating(key), reviews=db.reviews_for(key),
                           my_stars=(my["stars"] if my else 0), my_review=(my["review"] if my else ""))


@bp.route("/browse")
@auth.login_required
def browse_page():
    from . import discover
    genre = request.args.get("genre", "").strip()
    author = request.args.get("author", "").strip()
    if author:
        books, title, kind = audible.by_author(author, num=40), author, "author"
    elif genre:
        books, title, kind = discover.genre_new([genre], num_per=40), genre, "genre"
    else:
        return redirect(url_for("main.discover_page"))
    seen, uniq = set(), []
    for b in books:
        if b.get("asin") and b["asin"] not in seen:
            seen.add(b["asin"])
            b["state"] = _state_for(b["asin"], b["title"], b["author"])
            uniq.append(b)
    return render_template("browse.html", books=uniq, title=title, kind=kind, author=author)


@bp.route("/api/author/add", methods=["POST"])
@auth.login_required
def api_author_add():
    u = auth.current_user()
    author = request.get_json(force=True).get("author", "").strip()
    if not author:
        return jsonify({"error": "author required"}), 400
    res = chaptarr.add_and_search(author, author)
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,title,author,status,detail,source) VALUES (?,?,?,?,?,?)",
                  (u["id"], f"All books by {author}", author,
                   "handed" if res["ok"] else "failed", res.get("detail", ""), "manual"))
    return jsonify(res)


@bp.route("/settings")
@auth.login_required
def settings_page():
    g = db.setting
    return render_template("settings.html",
                           email_configured=notify.email_configured(),
                           email_enabled=db.get_meta("email_enabled", "0") == "1",
                           email_theme=notify.current_theme(),
                           email_frequency=db.get_meta("email_frequency", "immediate"),
                           discord_configured=bool(db.setting("discord_webhook", config.DISCORD_WEBHOOK)),
                           discord_webhook=db.setting("discord_webhook", config.DISCORD_WEBHOOK),
                           discord_enabled=db.get_meta("discord_enabled", "0") == "1",
                           notify_avail_enabled=db.get_meta("notify_avail_enabled", "0") == "1",
                           notify_newrelease_enabled=db.get_meta("notify_newrelease_enabled", "0") == "1",
                           custom_webhook=db.setting("custom_webhook", ""),
                           themes=list(notify.THEMES),
                           auto_add_level=db.get_meta("auto_add_level", "off"),
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
                                 "kavita_url": g("kavita_url", config.KAVITA_URL),
                                 "kavita_api_key": g("kavita_api_key", config.KAVITA_API_KEY),
                                 "calibreweb_url": g("calibreweb_url", config.CALIBREWEB_URL),
                                 "calibreweb_user": g("calibreweb_user", config.CALIBREWEB_USER),
                                 "calibreweb_pass": g("calibreweb_pass", config.CALIBREWEB_PASS)},
                           reading={"goodreads_rss": g("goodreads_rss", config.GOODREADS_RSS),
                                    "hardcover_token": g("hardcover_token", config.HARDCOVER_TOKEN)},
                           hide_rated_history=db.get_meta("hide_rated_history", "0") == "1",
                           format_mode=formats.mode(),
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
    fmt = book.get("format") or "audiobook"
    res = chaptarr.add_and_search(book["title"], book.get("author", ""), book.get("asin", ""), fmt=fmt)
    status = "handed" if res["ok"] else "failed"
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,asin,title,author,cover,status,detail,chaptarr_ref,source,format) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (user_id, book.get("asin", ""), book["title"], book.get("author", ""),
                   book.get("cover", ""), status, res.get("detail", ""), res.get("ref", ""), source, fmt))
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
    # The client sends title/author for library books (which usually have no
    # real ASIN); only look them up on Audible for genuine ASINs. The author is
    # what the recommender boosts on, so we must capture it either way.
    title, author = (body.get("title") or "").strip(), (body.get("author") or "").strip()
    review = (body.get("review") or "").strip()[:1500]
    fmt = body.get("format") or ("ebook" if asin.startswith(("gb:", "ol:")) else "audiobook")
    if (not title or not author) and not asin.startswith(("t-", "gb:", "ol:")):
        meta = audible.by_asin(asin) or {}
        title = title or meta.get("title", "")
        author = author or meta.get("author", "")
    with db.conn() as c:
        # a blank review on an update must not wipe an existing one
        c.execute("INSERT INTO ratings (user_id,asin,title,author,stars,review,format,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,datetime('now','localtime')) "
                  "ON CONFLICT(user_id,asin) DO UPDATE SET "
                  "stars=excluded.stars, title=excluded.title, author=excluded.author, "
                  "review=CASE WHEN excluded.review<>'' THEN excluded.review ELSE ratings.review END, "
                  "format=excluded.format, updated_at=datetime('now','localtime')",
                  (u["id"], asin, title, author, stars, review, fmt))
    return jsonify({"ok": True, "community": db.community_rating(asin)})


@bp.route("/api/history/remove", methods=["POST"])
@auth.login_required
def api_history_remove():
    """Remove a book from History & ratings for good. Drops any rating and
    records a 'hist_hidden' marker keyed on the rating key, so the book stays
    gone even though it's still finished in Audiobookshelf."""
    u = auth.current_user()
    body = request.get_json(force=True)
    key = (body.get("key") or "").strip()
    title = (body.get("title") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    why = f"removed: {title}" if title else "removed from history"
    with db.conn() as c:
        c.execute("DELETE FROM ratings WHERE user_id=? AND asin=?", (u["id"], key))
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) "
                  "VALUES (?,?,?,?,?)", (u["id"], "hist_hidden", key, 0, why))
    return jsonify({"ok": True})


@bp.route("/api/signal/<int:sid>/delete", methods=["POST"])
@auth.login_required
def api_signal_delete(sid):
    """Undo a taste signal (un-hide, un-pass, drop a read-seed or DNF)."""
    u = auth.current_user()
    with db.conn() as c:
        c.execute("DELETE FROM signals WHERE id=? AND user_id=?", (sid, u["id"]))
    return jsonify({"ok": True})


@bp.route("/api/rating/delete", methods=["POST"])
@auth.login_required
def api_rating_delete():
    """Clear a rating (book returns to unrated)."""
    u = auth.current_user()
    key = (request.get_json(force=True).get("key") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    with db.conn() as c:
        c.execute("DELETE FROM ratings WHERE user_id=? AND asin=?", (u["id"], key))
    return jsonify({"ok": True})


@bp.route("/api/dnf", methods=["POST"])
@auth.login_required
def api_dnf():
    """Mark a book you didn't finish — a negative taste signal that stops it
    (and exact-title re-suggestions) from coming back."""
    u = auth.current_user()
    body = request.get_json(force=True)
    title, authr = (body.get("title") or "").strip(), (body.get("author") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    hit = audible.search(f"{title} {authr}", num=1)
    b = hit[0] if hit else {"asin": "", "title": title, "author": authr}
    key = b.get("asin") or db.rating_key("", b.get("title", title), b.get("author", authr))
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                  (u["id"], "asin", key, -2, f"dnf: {b.get('title', title)}"))
    return jsonify({"ok": True, "matched": b.get("title", title)})


@bp.route("/api/onboard/dismiss", methods=["POST"])
@auth.login_required
def api_onboard_dismiss():
    """Hide the quick-rate onboarding card for good."""
    u = auth.current_user()
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) "
                  "VALUES (?,?,?,?,?)", (u["id"], "onboard_dismissed", "1", 0, "dismissed onboarding"))
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
    "goodreads_rss", "hardcover_token", "discord_webhook", "custom_webhook",
    "auto_add_level", "formats",
    "kavita_url", "kavita_api_key",
    "calibreweb_url", "calibreweb_user", "calibreweb_pass",
}
BOOL_KEYS = {"email_enabled", "discord_enabled", "hide_rated_history",
             "notify_avail_enabled", "notify_newrelease_enabled"}


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
        if service == "chaptarr":
            import requests as rq
            r = rq.get(f"{chaptarr.url()}/api/v1/system/status",
                       headers={"X-Api-Key": chaptarr.api_key()}, timeout=15)
            return jsonify({"ok": r.ok, "detail": "Connected" if r.ok else f"HTTP {r.status_code}"})
        # every library source backend (abs / kavita / calibreweb) self-tests
        from . import backends
        b = backends.by_id(service)
        if b is not None:
            return jsonify(b.test())
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


@bp.route("/cover/<item_id>")
@auth.login_required
def cover(item_id):
    """Proxy an Audiobookshelf cover through Stackarr so the browser (which
    can't reach host.docker.internal) gets it same-origin, token hidden."""
    from flask import Response
    import requests as rq
    try:
        r = rq.get(f"{absclient.abs_url()}/api/items/{item_id}/cover",
                   headers={"Authorization": f"Bearer {absclient.admin_token()}"}, timeout=20)
        if r.ok and r.content:
            return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                            headers={"Cache-Control": "max-age=86400"})
    except Exception:
        pass
    # ABS has no art for this item -> fall back to the Audible cover by title/author
    try:
        m = absclient.item_detail(item_id)
        hits = audible.search(f"{m.get('title','')} {m.get('author','')}", num=1)
        if hits and hits[0].get("cover"):
            return redirect(hits[0]["cover"])
    except Exception:
        pass
    return redirect(url_for("static", filename="icon.svg"))


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
        "const C='stackarr-v3';\n"
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
