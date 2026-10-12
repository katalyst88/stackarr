"""Routes: pages + the JSON API the front-end uses. Approval, manual
'already read', 5-star ratings, discover, settings, email preview, health."""
import logging
import re

from flask import (Blueprint, jsonify, make_response, redirect, render_template, request,
                   session, url_for)

from . import (absclient, audible, audnexus, auth, chaptarr, config, db,
               discover, ebookmeta, formats, notify, picks, recommend, seriesmap, status, tagging,
               titles, tz)

log = logging.getLogger("stackarr.routes")


def reading_history(be, u) -> list[dict]:
    """A backend's reading history, fetched at most ONCE per request.

    Calibre-Web has no API, so its history is a crawl of the paginated
    /opds/readbooks feed — a dozen HTTP calls. The home page asked two different
    backends loops for the same history and paid for that crawl twice on every
    render, about 1.6s of the page's time. Memoised on `g`, so it is per-request
    and cannot go stale between requests (marking a book read still shows up on
    the very next load)."""
    from flask import g
    cache = getattr(g, "_reading_history", None)
    if cache is None:
        cache = g._reading_history = {}
    key = (getattr(be, "id", str(be)), str((u or {}).get("id", "")))
    if key not in cache:
        cache[key] = be.reading_history(u)
    return cache[key]


def abs_history(u) -> list[dict]:
    """The user's Audiobookshelf listening history, fetched at most ONCE per
    request — Home reads it for the shelves, the goal ring and stale-pick
    retirement. Same per-request memo as reading_history(). Includes books
    that are only just begun (`reading`), not just the recommender's 25%+."""
    from flask import g
    cache = getattr(g, "_abs_history", None)
    if cache is None:
        cache = g._abs_history = {}
    uid = str((u or {}).get("id", ""))
    if uid not in cache:
        tok = (u or {}).get("abs_token")
        cache[uid] = absclient.listening_history(tok, started=True) if tok else []
    return cache[uid]


bp = Blueprint("main", __name__)

# Recommendation lanes — single source of truth for their display titles and the
# order they appear on the Suggestions page. To add a lane: give it a key here,
# emit suggestions with that `lane` value from the recommender, and (optionally)
# place it in LANE_ORDER. Both suggestions_page and lane_grid read these.
LANE_TITLES = {
    "series": "Series to finish", "author": "More from authors you love",
    "enjoyed": "Readers also enjoyed", "discover_author": "New authors to discover",
    "narrator": "Narrators you love", "genre": "More in your favourite genres",
    "mood": "Matches your mood", "hidden": "Off the beaten path", "awards": "Award winners",
    "short": "Short listens", "epic": "Epic listens", "upcoming": "New & upcoming",
    "importlist": "From your reading list", "discover": "Popular picks", "foryou": "For you",
}
LANE_ORDER = ["series", "enjoyed", "mood", "discover_author", "author", "narrator", "genre",
              "hidden", "awards", "short", "epic", "upcoming", "importlist", "foryou", "discover"]


# ------------------------------------------------------------------ auth ---
_LOGIN_FAILS = {}          # ip -> (count, first_ts); brute-force throttle
_LOCK_AFTER = 5
_LOCK_WINDOW = 900         # 15 min


@bp.route("/login", methods=["GET", "POST"])
def login():
    import time
    error = ""
    ip = request.remote_addr or "?"
    providers = auth.login_providers()
    first_run = db.user_count() == 0          # no accounts yet -> bootstrap admin
    ctx = {"providers": providers, "first_run": first_run,
           "allow_register": True}

    def _page(err="", code=200):
        return render_template("login.html", error=err, **ctx), code

    def _safe_next():
        # only a local, relative path — reject "//evil", "/\evil" (browsers
        # normalise \ → /), and absolute URLs (open redirect).
        nxt = request.args.get("next") or ""
        return nxt if re.match(r"^/[^/\\]", nxt) else url_for("main.index")

    if request.method == "POST":
        cnt, first = _LOGIN_FAILS.get(ip, (0, 0.0))
        if cnt >= _LOCK_AFTER and (time.time() - first) < _LOCK_WINDOW:
            return _page("Too many attempts — try again in a few minutes.", 429)
        if (time.time() - first) >= _LOCK_WINDOW:
            cnt, first = 0, time.time()

        action = request.form.get("action", "signin")
        provider = (request.form.get("provider") or "local").strip()
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if action == "register":
            invite = (request.form.get("invite") or "").strip()
            want = db.get_meta("invite_code", "")
            if not first_run and want and invite != want:
                error = "That invite code isn't right."
            elif not username or not password:
                error = "Pick a username and password."
            else:
                u = auth.register_local(username, password, request.form.get("email", "").strip())
                if u:
                    _LOGIN_FAILS.pop(ip, None)
                    return redirect(_safe_next())
                error = "That username is taken."
        else:
            if provider == "local":
                u = auth.do_login_local(username, password)
            else:
                u = auth.do_login_provider(provider, username, password)
            if u:
                _LOGIN_FAILS.pop(ip, None)
                return redirect(_safe_next())
            error = "Wrong username or password."

        _LOGIN_FAILS[ip] = (cnt + 1, first or time.time())
        return _page(error)
    return _page()


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
        hist = abs_history(u)
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
    return redirect(url_for("main.home_page"))


@bp.route("/home")
@auth.login_required
def home_page():
    """Dashboard hub: what you're reading, what's next, goal progress, a few
    fresh picks, and new from authors you follow — links out to everything."""
    import datetime
    u = auth.current_user()
    shelves, counts = _shelves_data(u)
    # don't offer a "fresh pick" that's since been added, requested or finished
    try:
        picks.retire(u["id"], picks.finished_works(u, abs_history(u)))
    except Exception as e:
        log.debug("home: retiring stale picks failed: %s", e)
    # a few fresh picks + new-from-upcoming + goal + up-next count
    with db.conn() as c:
        fresh = [dict(r) for r in c.execute(
            "SELECT id,title,author,cover,reason,format,asin FROM suggestions "
            "WHERE user_id=? AND status='pending' AND lane NOT IN ('upcoming') ORDER BY score DESC LIMIT 8", (u["id"],))]
        upcoming = [dict(r) for r in c.execute(
            "SELECT id,title,author,cover,reason,format,asin,extra FROM suggestions "
            "WHERE user_id=? AND lane='upcoming' AND status='pending' ORDER BY extra LIMIT 6", (u["id"],))]  # ASC: soonest first (match Upcoming page)
        want_n = c.execute("SELECT COUNT(*) n FROM shelf WHERE user_id=? AND state='want'", (u["id"],)).fetchone()["n"]
        avail_n = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=? AND status='available'", (u["id"],)).fetchone()["n"]
    year = tz.today().year
    read_year = sum(1 for d in _finish_dates_all(u) if d.startswith(str(year)))
    try:
        goal = int(db.get_meta(f"goal_{u['id']}", "0") or 0)
    except ValueError:
        goal = 0
    return render_template("home.html", fresh=fresh, upcoming=upcoming,
                           goal=goal, read_year=read_year, year=year, want_n=want_n, avail_n=avail_n,
                           shelves=shelves, counts=counts)


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
    lane_titles = LANE_TITLES
    lanes = {k: lanes[k] for k in LANE_ORDER if k in lanes}
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
        recent_rows = [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 80", (u["id"],))]
        rated_count = c.execute("SELECT COUNT(*) FROM ratings WHERE user_id=?", (u["id"],)).fetchone()[0]
        onboard_off = bool(c.execute(
            "SELECT 1 FROM signals WHERE user_id=? AND kind='onboard_dismissed'", (u["id"],)).fetchone())
    # quick-rate onboarding: only when taste is thin and not dismissed
    onboard_books = [] if (rated_count >= ONBOARD_THRESHOLD or onboard_off) else _onboarding_books(u)
    recent_requests = group_requests(recent_rows)[:14]      # one card per book, current status
    with db.conn() as c:
        vibes_done = bool(c.execute("SELECT 1 FROM signals WHERE user_id=? AND kind='vibes_done'", (u["id"],)).fetchone())
    show_vibes = not vibes_done and rated_count < ONBOARD_THRESHOLD
    return render_template("suggestions.html", lanes=lanes, lane_titles=lane_titles,
                           genres=discover.DEFAULT_GENRES, rec_authors=rec_authors,
                           abs_public=_abs_public_url(),
                           recently_added=recently_added, recent_requests=recent_requests,
                           onboard_books=onboard_books, onboard_target=ONBOARD_THRESHOLD,
                           show_vibes=show_vibes, all_moods=tagging.ALL_MOODS)


def _abs_public_url() -> str:
    """The browser-facing Audiobookshelf address, or "" when none is set. Only
    an http(s) URL — it lands in an href."""
    u = (db.setting("abs_public_url", config.ABS_PUBLIC_URL) or "").strip().rstrip("/")
    return u if re.match(r"^https?://[^\s\"'<>]+$", u) else ""


@bp.route("/lane/<lane>")
@auth.login_required
def lane_grid(lane):
    u = auth.current_user()
    titles = LANE_TITLES
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


def _work_key(r) -> tuple:
    """Which book a request row is about: its user + the title without edition
    or series decoration + first author. Bulk "Full series:" / "All books by"
    rows are their own book."""
    return (r.get("user_id"), titles.parts(r.get("title") or "")[0], _first(r.get("author")).lower())


def group_requests(rows: list[dict]) -> list[dict]:
    """Request rows as ONE entry per book per user, newest first: the latest
    row per format is current, older rows are its history (a failed try that
    a later request superseded, say). Each entry: {id, ids, title, author,
    cover, asin, username, formats, current, history, st, retry_id}, with
    `st` from status.for_request_group — the same answer the book page gives.
    Groups the rows already in the database too, so nothing is deleted."""
    groups = {}
    for r in sorted(rows, key=lambda x: x["id"], reverse=True):
        groups.setdefault(_work_key(r), []).append(r)
    out = []
    for rs in groups.values():
        latest, current, history = rs[0], {}, []
        for r in rs:                                  # newest first
            f = r.get("format") or "audiobook"
            if f not in current:
                current[f] = r
            else:
                history.append(r)
        cur = list(current.values())
        st = status.for_request_group(cur)
        failed = [r for r in cur if r["status"] in ("failed", "denied")]
        out.append({"id": latest["id"], "ids": [r["id"] for r in rs], "title": latest["title"],
                    "author": latest.get("author") or "", "asin": next((r["asin"] for r in rs if r.get("asin")), ""),
                    "cover": next((r["cover"] for r in rs if r.get("cover")), ""),
                    "username": latest.get("username") or "", "formats": sorted(current),
                    "current": cur, "history": history, "st": st,
                    # the status line's own words; nothing once it's in the library (the
                    # row's stored "Sent to Chaptarr…" is stale by then)
                    "detail": "" if st["state"] in ("available", "none") else (st.get("detail") or ""),
                    "retry_id": failed[0]["id"] if failed else None,
                    "approval": any(r["status"] == "pending_approval" for r in cur)})
    return out


@bp.route("/requests")
@auth.login_required
def requests_page():
    """Your requests — one entry per book, its formats and where each stands
    (status.py), earlier attempts folded under it. ?wanted=1 lists only books
    whose current attempt failed; admins add ?all=1 for everyone's."""
    u = auth.current_user()
    admin = u["role"] == "admin"
    wanted = bool(request.args.get("wanted"))            # Sonarr-style: couldn't-grab list
    with db.conn() as c:
        if admin and request.args.get("all"):
            rows = [dict(r) for r in c.execute("SELECT r.*, u.username FROM requests r "
                    "JOIN users u ON u.id=r.user_id ORDER BY r.id DESC LIMIT 600")]
        else:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 600", (u["id"],))]
        # admins get the approval queue (everyone's pending requests)
        approvals = []
        if admin:
            approvals = [dict(r) for r in c.execute(
                "SELECT r.*, u.username FROM requests r JOIN users u ON u.id=r.user_id "
                "WHERE r.status='pending_approval' ORDER BY r.id")]
    entries = group_requests(rows)
    if wanted:
        entries = [e for e in entries if e["retry_id"]]
    # a normal user shouldn't see their pending_approval rows twice; they already
    # appear in their own list with a 'Needs approval' status.
    return render_template("requests.html", requests=entries, admin=admin, wanted=wanted,
                           approvals=approvals)


_LEADING_NUM = re.compile(r"^\d{1,3}\s+(?=[A-Za-z])")       # "09 Bloodline" (a numbered rip)


def _finished_books(u) -> list[dict]:
    """Every book the user has FINISHED, each ONCE: {title, author, date
    (earliest, ISO), formats}. Combines Audiobookshelf listening history,
    connected ebook sources and the manual Read shelf.

    Once per WORK, not per library item: a library often holds the same book
    twice ("Unsouled (Cradle Book 1)" and "Unsouled: Cradle, Volume 1", or a
    numbered rip "09 Bloodline"), and a book finished in ABS and also marked
    Read must not count twice — Insights showed 30 finished for 13 books.
    Same author + titles.matches(), so two different "Star Wars: …" books
    never merge. Home's goal ring and Insights both read this."""
    import datetime
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in c.execute("SELECT item_id,title,author,asin,format FROM library")}
    books: list[dict] = []
    by_author: dict = {}

    def _add(title, author, date, fmt):
        t = _LEADING_NUM.sub("", (title or "").strip())
        if not t or not date:
            return
        a = _first(author).lower()
        same = by_author.setdefault(a, [])
        hit = titles.matches(same, t) if a else [b for b in same if b["title"].lower() == t.lower()]
        if hit:
            b = hit[0]
            b["date"] = min(b["date"], date)
            if fmt not in b["formats"]:
                b["formats"].append(fmt)
            return
        b = {"title": t, "author": author or "", "date": date, "formats": [fmt]}
        same.append(b)
        books.append(b)

    def _day(ms):
        return tz.day_of_ms(ms)              # the household's calendar day, not the container's

    try:
        for h in abs_history(u):
            m = lib.get(h["item_id"]) or {}
            if h.get("finished") and h.get("last_update") and m.get("title"):
                _add(m["title"], m.get("author", ""), _day(h["last_update"]), m.get("format") or "audiobook")
    except Exception as e:
        log.debug("finished books: ABS history failed: %s", e)
    if formats.show("ebook"):
        from . import backends
        for be in backends.sources("ebook"):
            try:
                for h in reading_history(be, u):
                    m = lib.get(h["item_id"]) or {}
                    if h.get("finished") and h.get("last_update") and m.get("title"):
                        _add(m["title"], m.get("author", ""), _day(h["last_update"]), "ebook")
            except Exception:
                pass
    for r in db.shelf_list(u["id"], "read"):           # manual Read-shelf finishes
        if r.get("finished_at"):
            _add(r.get("title"), r.get("author"), tz.db_day(r["finished_at"]), r.get("format") or "audiobook")
    return books


def _finish_dates_all(u) -> list[str]:
    """The finish date of every book the user has finished, once per book."""
    return [b["date"] for b in _finished_books(u)]


def _heatmap(dates: list[str]) -> dict:
    """Build a GitHub-style heatmap: 53 weeks x 7 days ending today, each cell a
    finish count. Returns {weeks: [[{date,count}]], total, max, months}."""
    import datetime
    from collections import Counter
    counts = Counter(d for d in dates if d)
    today = tz.today()
    start = today - datetime.timedelta(days=today.weekday() + 1 + 52 * 7)   # Sunday ~53wk ago
    weeks, cur = [], start
    mx = 0
    while cur <= today:
        week = []
        for _ in range(7):
            ds = cur.isoformat()
            n = counts.get(ds, 0)
            mx = max(mx, n)
            week.append({"date": ds, "count": n, "future": cur > today})
            cur += datetime.timedelta(days=1)
        weeks.append(week)
    return {"weeks": weeks, "total": sum(counts.values()), "max": mx}


@bp.route("/insights")
@auth.login_required
def insights_page():
    """Your reading, wrapped — from the same sources as Home, read live on
    every view: finishes once per book (_finished_books, so the year count
    matches Home's goal ring), in-progress from Audiobookshelf + ebook
    sources (a book started today counts), ratings and moods from Stackarr."""
    import datetime
    u = auth.current_user()
    hist = abs_history(u)
    stats = absclient.listening_stats(u["abs_token"])
    books = _finished_books(u)
    from . import backends
    ebook_hist = []
    if formats.show("ebook"):
        for be in backends.sources("ebook"):
            try:
                ebook_hist += reading_history(be, u)
            except Exception:
                pass
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in c.execute("SELECT item_id,title,author,format FROM library")}
        ratings = [dict(r) for r in c.execute("SELECT stars,asin,title,author,format FROM ratings WHERE user_id=?", (u["id"],))]
        # books Stackarr got you: distinct works, not one per format row, and
        # not the rows back-filled from a library import
        added = [dict(r) for r in c.execute(
            "SELECT title, author FROM requests WHERE user_id=? AND status='available' "
            "AND COALESCE(source,'') <> 'backfill'", (u["id"],))]
    req_avail = len({(titles.parts(r["title"])[0], _first(r["author"]).lower()) for r in added})
    finished = len(books)
    by_format = {"audiobook": 0, "ebook": 0}
    for b in books:
        for f in b["formats"]:
            by_format[f if f in by_format else "audiobook"] += 1
    reading = {}
    for h in hist:
        m = lib.get(h["item_id"]) or {}
        if h.get("reading") and m.get("title"):
            reading.setdefault((titles.parts(m["title"])[0], _first(m.get("author")).lower()), m)
    for h in ebook_hist:
        m = lib.get(h["item_id"]) or {}
        if not h.get("finished") and 0.02 < (h.get("progress") or 0) < 1 and m.get("title"):
            reading.setdefault((titles.parts(m["title"])[0], _first(m.get("author")).lower()), m)
    in_prog = len(reading)
    authors = {}
    for b in books:
        a = _first(b["author"]).split(" - ")[0].strip()
        if a:
            authors[a] = authors.get(a, 0) + 1
    explored = {a for a in authors} | {_first(m.get("author")) for m in reading.values() if m.get("author")}
    finish_dates = [b["date"] for b in books]
    # mood profile from cached tags of rated books (bounded, no fetch)
    from collections import Counter
    moodc = Counter()
    for r in ratings[:120]:
        rk = db.rating_key(r["asin"], r["title"], r["author"])
        for m in db.tags_for(rk).get("mood", []):
            moodc[m] += 1
    top_moods = moodc.most_common(6)
    heat = _heatmap(finish_dates)
    year = tz.today().year
    read_year = sum(1 for d in finish_dates if d.startswith(str(year)))
    try:
        goal = int(db.get_meta(f"goal_{u['id']}", "0") or 0)
    except ValueError:
        goal = 0
    hours = round(stats["total_seconds"] / 3600)
    star_vals = [r["stars"] for r in ratings]
    facts = []
    if hours:
        facts.append(("⏳", f"{hours:,} hours", "listened all-time" + (f" — about {round(hours/24):,} days" if hours >= 48 else "")))
    if top := (sorted(authors.items(), key=lambda x: x[1], reverse=True)[:1] or [None])[0]:
        facts.append(("✍️", top[0], f"your most-read author ({top[1]} books)"))
    if star_vals:
        facts.append(("⭐", f"{round(sum(star_vals)/len(star_vals),1)} avg", f"across {len(star_vals)} rated books"))
    if top_moods:
        facts.append(("🎭", top_moods[0][0], "your most-read mood"))
    if req_avail:
        facts.append(("📚", f"{req_avail}", "books added via Stackarr"))
    top_authors = sorted(authors.items(), key=lambda x: x[1], reverse=True)[:10]
    return render_template("insights.html", total=len(hist), finished=finished, in_progress=in_prog,
                           explored=len(explored),
                           hours=hours, req_avail=req_avail, facts=facts, top_authors=top_authors,
                           by_format=by_format, heat=heat, top_moods=top_moods,
                           goal=goal, read_year=read_year, year=year)


@bp.route("/history")
@auth.login_required
def history_page():
    """Books you've read — finished in Audiobookshelf, rated, or marked read —
    each with a 1-5 star rating control that feeds the recommender."""
    u = auth.current_user()
    hist = abs_history(u)
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

    def add(asin, title, author, cover, when, fmt="audiobook"):
        k = key(asin, title)
        if not k or k in seen:
            return
        seen.add(k)
        rk = db.rating_key(asin, title, author)
        if rk in hidden:
            return
        # no stored cover (ebook / marked-read item) -> resolve it lazily via /coverart
        if not cover:
            cover = url_for("main.coverart", asin=asin or "", title=title or "",
                            author=author or "", fmt=fmt)
        books.append({"asin": asin or "", "rkey": rk, "title": title or "Untitled",
                      "author": author or "", "cover": cover, "format": fmt,
                      "stars": (rated.get(rk) or {}).get("stars", 0), "when": when})

    # 1) finished in Audiobookshelf (the real listening history, with covers)
    for h in hist:
        if not h["finished"]:
            continue
        m = lib.get(h["item_id"]) or {}
        add((m.get("asin") or "").strip(), m.get("title", ""), m.get("author", ""),
            url_for("main.cover", item_id=h["item_id"]), h["last_update"], "audiobook")
    # 1b) finished ebooks from connected ebook sources (Kavita/Calibre-Web)
    if formats.show("ebook"):
        from . import backends
        with db.conn() as c:
            elib = {r["item_id"]: dict(r) for r in
                    c.execute("SELECT item_id,title,author FROM library WHERE format='ebook'")}
        for be in backends.sources("ebook"):
            try:
                for h in reading_history(be, u):
                    if not h.get("finished"):
                        continue
                    m = elib.get(h["item_id"]) or {}
                    if m.get("title"):
                        add("", m["title"], m.get("author", ""), "", h.get("last_update", 0), "ebook")
            except Exception:
                pass
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
    hide_rated = db.get_pref(u["id"], "hide_rated_history", "0") == "1"
    if hide_rated:
        books = [b for b in books if not b["stars"]]
    # unrated float to the top (the to-do pile); rated sink to the bottom.
    # within each group, most-recent first.
    books.sort(key=lambda b: (b["stars"] > 0, -(b["when"] or 0)))
    rated_n = sum(1 for b in books if b["stars"])
    return render_template("history.html", books=books, rated_n=rated_n, hide_rated=hide_rated)


_INFER_NON_SERIES = re.compile(
    r"\b(unabridged|abridged|edition|audiobook|novel|novella|boxset|box set|collection|complete)\b", re.I)


def _infer_series(title: str):
    """Recover a (series_name, sequence) from a title when the source left the
    series field blank. Two common shapes:
      * trailing parenthetical — "Leviathan Falls (The Expanse Book 9)"
      * leading numbered prefix — "The Expanse 05 Nemesis Games"
    Returns (None, None) when nothing confident matches (so unrelated standalone
    titles like "1984" never get grouped). Conservative on purpose."""
    t = (title or "").strip()
    m = re.search(r"\(([^()]+?)(?:[, ]+Book\s+(\d+(?:\.\d+)?))?\)\s*$", t, re.I)
    if m and not _INFER_NON_SERIES.search(m.group(1)):
        name = m.group(1).strip()
        if name and not name[0].isdigit() and len(name) > 2:
            return name, (float(m.group(2)) if m.group(2) else None)
    m = re.match(r"^(.+?)\s+(\d{1,3}(?:\.\d+)?)\s+([A-Za-z].*)$", t)
    if m and len(m.group(1).strip()) > 2 and any(ch.isalpha() for ch in m.group(1)):
        return m.group(1).strip(), float(m.group(2))
    return None, None


@bp.route("/series")
@auth.login_required
def series_page():
    """Up Next: series you're collecting, how far you are, and the next book
    (with its state) — built from your library + the engine's series picks."""
    u = auth.current_user()
    # which library items has the user actually FINISHED (read), so we can show
    # reading progress separately from what's downloaded.
    finished_ids, inprogress_ids = set(), set()
    try:
        for h in abs_history(u):
            if h.get("finished"):
                finished_ids.add(h["item_id"])
            elif h.get("reading"):
                inprogress_ids.add(h["item_id"])
    except Exception:
        pass
    if formats.show("ebook"):
        from . import backends
        for be in backends.sources("ebook"):
            try:
                for h in reading_history(be, u):
                    if h.get("finished"):
                        finished_ids.add(h["item_id"])
                    elif 0.02 < (h.get("progress") or 0) < 1:
                        inprogress_ids.add(h["item_id"])
            except Exception:
                pass
    with db.conn() as c:
        # Include books with NO series field — many audiobook rips encode the
        # series in the title ("The Expanse 05 …") and some ebook feeds drop the
        # series metadata. _infer_series recovers those so they still group.
        libr = [dict(r) for r in c.execute(
            "SELECT item_id,title,author,series,series_seq,asin,format FROM library "
            "WHERE gone_at IS NULL ORDER BY series, series_seq")]
        sugg = [dict(r) for r in c.execute(
            "SELECT id,title,author,series,asin,cover,reason FROM suggestions "
            "WHERE user_id=? AND lane='series' AND status='pending' ORDER BY score DESC", (u["id"],))]

    def norm(s):
        return (s or "").strip().lower()

    next_by_series = {}
    for s in sugg:
        next_by_series.setdefault(norm(s["series"]), s)

    def _series_key(name):
        # merge "The Expanse" / "Expanse" and casing variants into one group
        return re.sub(r"^the\s+", "", (name or "").strip(), flags=re.I).lower()

    # group by effective series: the stored field, else one inferred from the
    # title. Variants collapse by normalised key; the longest seen name displays.
    groups, display_name = {}, {}
    for b in libr:
        series = (b.get("series") or "").strip()
        if not series:
            series, inferred_seq = _infer_series(b["title"])
            if not series:
                continue
            if b.get("series_seq") is None and inferred_seq is not None:
                b["series_seq"] = inferred_seq
        key = _series_key(series)
        if key not in display_name or len(series) > len(display_name[key]):
            display_name[key] = series
        groups.setdefault(key, []).append(b)
    # relabel groups from the normalised key to the chosen display name
    groups = {display_name[k]: v for k, v in groups.items()}

    def _seq_from_title(t):
        # ABS often stores the series name without a number; recover it from the
        # title, e.g. "Cradle Book 5 - Ghostwater", "Reaper: Cradle, Book 10".
        m = re.search(r"\bbook\s+(\d+(?:\.\d+)?)\b", (t or ""), re.I) or re.search(r"#\s*(\d+(?:\.\d+)?)", t or "")
        try:
            return float(m.group(1)) if m else None
        except (ValueError, TypeError):
            return None

    both = formats.multi()
    cards = []
    for name, books in groups.items():
        # effective sequence (series field, else parsed from title) + read flag
        for b in books:
            b["seq"] = b["series_seq"] if b["series_seq"] is not None else _seq_from_title(b["title"])
            b["finished"] = b["item_id"] in finished_ids
            b["reading"] = b["item_id"] in inprogress_ids
        books.sort(key=lambda b: b["seq"] if b["seq"] is not None else 0)
        seqs = [b["seq"] for b in books if b["seq"] is not None]
        read_seqs = [b["seq"] for b in books if b["seq"] is not None and b["finished"]]
        audio_seqs = {round(b["seq"], 1) for b in books
                      if b["seq"] is not None and (b.get("format") or "audiobook") == "audiobook"}
        ebook_seqs = {round(b["seq"], 1) for b in books
                      if b["seq"] is not None and b.get("format") == "ebook"}
        all_seqs = audio_seqs | ebook_seqs
        nxt = next_by_series.get(norm(name))
        fmts = {b.get("format") or "audiobook" for b in books}
        cards.append({"name": name, "owned": len(books),
                      "highest": max(seqs) if seqs else None,
                      "read_to": max(read_seqs) if read_seqs else None,
                      "read_count": sum(1 for b in books if b["finished"]),
                      "reading_now": any(b["item_id"] in inprogress_ids for b in books),
                      "books": books,
                      "format": (books[0].get("format") or "audiobook") if len(fmts) == 1 else "both",
                      "missing_audio": (both and len(all_seqs - audio_seqs) > 0),
                      "missing_ebook": (both and len(all_seqs - ebook_seqs) > 0),
                      "author": (books[0].get("author") if books else ""),
                      "next": nxt,
                      "next_st": book_status(nxt.get("asin") or "", nxt["title"], nxt.get("author") or "") if nxt else None})
    cards = [x for x in cards if x["owned"] >= 2 or x["next"]]
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


_BOOK_META: dict = {}            # asin -> (fetched_at, audible row, audnexus row)
_BOOK_META_TTL = 600


def _catalogue_book(asin) -> tuple[dict, dict]:
    """(Audible product, Audnexus record) for an ASIN. The two lookups run side
    by side instead of one after the other (each is a few hundred ms), and a
    found book is kept for ten minutes, so going back to a book page — or
    opening it again from another card — doesn't pay for both again."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    hit = _BOOK_META.get(asin)
    if hit and time.time() - hit[0] < _BOOK_META_TTL:
        return dict(hit[1]), dict(hit[2])
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa, fx = ex.submit(audible.by_asin, asin), ex.submit(audnexus.book, asin)
        b, ax = fa.result() or {}, fx.result() or {}
    if b.get("title"):
        if len(_BOOK_META) > 500:
            _BOOK_META.clear()
        _BOOK_META[asin] = (time.time(), dict(b), dict(ax))
    return b, ax


@bp.route("/book/<path:asin>")
@auth.login_required
def book_page(asin):
    # ebook ids are "gb:…"/"ol:…" and resolve via the ebook catalogue; real
    # audiobook ASINs go to Audible + Audnexus as before.
    if asin.startswith(("gb:", "ol:")):
        from . import ebookmeta
        b = ebookmeta.by_id(asin) or {}
        b["format"] = "ebook"
        b["asin"] = asin            # keep the gb:/ol: id as the page identity for actions
    else:
        b, ax = _catalogue_book(asin)
        b.setdefault("asin", asin)
        b.setdefault("format", "audiobook")
        if ax.get("genres"):
            b["genres"] = ax["genres"]
        if ax.get("series") and not b.get("series"):
            b["series"], b["sequence"] = ax["series"], ax.get("sequence")
            b["series_asin"] = ax.get("series_asin") or ""
    # Catalogue lookups can transiently fail (rate-limit / timeout). Fall back to
    # what Stackarr already knows about this book so the page never shows a bare
    # "Unknown" — we usually have its title/author/cover cached from a suggestion.
    if not (b.get("title") or "").strip():
        cached = _cached_book(asin)
        b.update({k: v for k, v in cached.items() if v and not b.get(k)})
    b.setdefault("title", "Unknown")
    if not b.get("cover"):
        b["cover"] = _local_cover(asin, b.get("title", ""), b.get("author", ""), b.get("format", ""))
    st = book_status(asin, b.get("title", ""), b.get("author", ""), b.get("release_date") or "")
    b["state"] = st["state"]
    u = auth.current_user()
    key = db.rating_key(asin if not asin.startswith(("gb:", "ol:")) else "",
                        b.get("title", ""), b.get("author", "")) if asin.startswith(("gb:", "ol:")) else asin
    with db.conn() as c:
        my = c.execute("SELECT stars, review FROM ratings WHERE user_id=? AND asin=?",
                       (u["id"], key)).fetchone()
    b["req_detail"] = st["detail"] if st["state"] == "failed" else ""
    # Tags already known are read locally. Unknown ones are derived from the
    # catalogue categories we already hold, and the Open Library subject
    # fetch (a network call that used to run inline, on every first view)
    # goes to the background worker.
    try:
        if db.has_tags(key):
            tags = db.tags_for(key)
        else:
            tags = tagging.derive(b.get("categories") or [])
            from . import scheduler
            scheduler.queue_job("tags", key, b.get("title", ""), b.get("author", ""), list(b.get("categories") or []))
    except Exception:
        tags = {}
    # duplicate/upgrade: which formats of this title you already own
    owned_formats = []
    if formats.multi() and b.get("title"):
        with db.conn() as c:
            owned_formats = sorted({r["format"] for r in _library_works(c, b["title"], b.get("author", ""))})
    shelf = db.shelf_state(u["id"], key)
    # "Finished" comes from Audiobookshelf too, not only Stackarr's own Read
    # shelf — the Home "Read" shelf already shows ABS finishes, and the book page
    # used to disagree with it
    finished = shelf == "read"
    if not finished:
        try:
            finished = picks.is_finished(picks.finished_works(u, abs_history(u)), b.get("title", ""),
                                         b.get("author", ""), asin if asin.startswith("B0") else "")
        except Exception as e:
            log.debug("book page: finished check failed: %s", e)
    return render_template("book.html", b=b, st=st, rate_key=key, owned_formats=owned_formats,
                           community=db.community_rating(key), reviews=db.reviews_for(key),
                           my_stars=(my["stars"] if my else 0), my_review=(my["review"] if my else ""),
                           tags=tags, shelf=shelf, finished=finished)


@bp.route("/browse")
@auth.login_required
def browse_page():
    from . import discover
    genre = request.args.get("genre", "").strip()
    author = request.args.get("author", "").strip()
    mood = request.args.get("mood", "").strip().lower()
    # mood -> catalogue search terms (curated; deterministic, keyless)
    MOOD_TERMS = {
        "funny": "humorous comic novel", "dark": "grimdark dark fantasy",
        "romantic": "romance novel", "tense": "psychological thriller",
        "adventurous": "adventure novel", "reflective": "literary fiction",
        "epic": "epic fantasy saga", "cozy": "cozy mystery", "emotional": "emotional literary fiction",
        "whimsical": "whimsical fantasy", "mysterious": "mystery detective novel",
        "bleak": "dystopian fiction", "intense": "war thriller",
        "tender": "coming of age novel", "thought-provoking": "philosophical fiction",
        "fast-paced": "fast-paced thriller", "slow-paced": "literary slow burn novel",
    }
    if author:
        books, title, kind = audible.by_author(author, num=40), author, "author"
    elif mood:
        term = MOOD_TERMS.get(mood, mood)
        src = ebookmeta.search(term, 40) if (formats.show("ebook") and not formats.show("audiobook")) else audible.search(term, num=40)
        for b in src:
            b.setdefault("asin", b.get("id", ""))
        books, title, kind = src, mood, "mood"
    elif genre:
        books, title, kind = discover.genre_new([genre], num_per=40), genre, "genre"
    else:
        return redirect(url_for("main.discover_page"))
    uniq = [b for b in _clean_catalogue(books) if b.get("asin")]
    _annotate_status(uniq)
    return render_template("browse.html", books=uniq, title=title, kind=kind, author=author)


@bp.route("/api/library/refresh", methods=["POST"])
@auth.login_required
def api_library_refresh():
    """Re-scan all connected libraries (ABS/Kavita/Calibre/Komga/OPDS) so newly
    added books + series show up. Used by the 'Check library' buttons."""
    wait = _cooldown("library_refresh", 20)
    if wait:
        return jsonify({"ok": False, "detail": f"Just scanned — try again in {wait}s."}), 429
    from . import scheduler
    try:
        scheduler.refresh_library()
    except Exception as e:
        return jsonify({"ok": False, "detail": str(e)})
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM library WHERE gone_at IS NULL").fetchone()["n"]
    return jsonify({"ok": True, "detail": f"Library re-scanned — {n} books"})


@bp.route("/api/series/missing")
@auth.login_required
def api_series_missing():
    """For a series you own, fetch the full series from the catalogue and report
    which entries you're missing (the gaps), so you can complete it."""
    u = auth.current_user()
    name = (request.args.get("series") or "").strip()
    if not name:
        return jsonify({"error": "series required"}), 400
    from collections import Counter
    with db.conn() as c:
        owned = [dict(r) for r in c.execute(
            "SELECT title, series_seq, author FROM library WHERE gone_at IS NULL AND lower(series)=?",
            (name.lower(),))]
    owned_seqs = {round(o["series_seq"], 1) for o in owned if o["series_seq"] is not None}
    owned_titles = {recommend._norm(o["title"]) for o in owned}
    # The author who writes this series (the most common among the books you own).
    # Used to (a) scope a fuller catalogue fetch and (b) reject a same-named series
    # by a different author — CompleteSeries' "layered evidence" idea.
    authors = Counter((o.get("author") or "").split(",")[0].strip()
                      for o in owned if o.get("author"))
    series_author = authors.most_common(1)[0][0] if authors else ""
    nn = recommend._norm(name)

    def _author_ok(cand_author: str) -> bool:
        if not series_author or not cand_author:
            return True                      # can't disambiguate -> don't drop it
        sa, ca = series_author.lower(), cand_author.lower()
        return sa in ca or ca.split(",")[0] in sa

    # A single keyword search misses long series and entries the name search doesn't
    # surface. Pool the author's catalogue (most reliable for a series) with keyword
    # searches, then dedupe — far more complete than one 40-result query.
    pool = []
    if series_author:
        pool += audible.by_author(series_author, num=50)
    pool += audible.search(name, num=50)
    if series_author:
        pool += audible.search(f"{name} {series_author}", num=50)
    full = {}
    for b in _in_language(pool):
        if recommend._norm(b.get("series")) != nn or b.get("sequence") is None:
            continue
        if not _author_ok(b.get("author", "")):
            continue
        seq = round(b["sequence"], 1)
        if seq not in full or b.get("num_ratings", 0) > full[seq].get("num_ratings", 0):
            full[seq] = b
    entries = []
    for seq in sorted(full):
        b = full[seq]
        # owned if we hold that position OR a library title matches (series_seq is
        # often null in ABS, so a seq-only check would over-report missing books).
        owned_flag = seq in owned_seqs or recommend._norm(b["title"]) in owned_titles
        entries.append({"seq": seq, "title": b["title"], "asin": b["asin"], "cover": b.get("cover", ""),
                        "author": b.get("author", ""), "owned": owned_flag})
    missing = [e for e in entries if not e["owned"]]
    return jsonify({"ok": True, "series": name, "total": len(entries),
                    "owned": len(entries) - len(missing), "missing": missing, "entries": entries})


def _series_target(src) -> dict:
    """What a series request names: a book (asin, or title + author) and/or a
    series (series_asin, or series name + author). `seq` is the viewed book's
    position, the fallback start of the "Next in" strip."""
    t = {k: (src.get(k) or "").strip() for k in ("asin", "series_asin", "series", "author", "title")}
    t["author"] = t["author"].split(",")[0].strip()
    for k in ("asin", "series_asin"):            # ASIN-shaped or nothing — they go into a URL path
        if not re.fullmatch(r"[A-Za-z0-9]{10}", t[k]):
            t[k] = ""
    try:
        t["seq"] = float(src.get("seq")) if src.get("seq") not in (None, "") else None
    except (TypeError, ValueError):
        t["seq"] = None
    return t


def _series_view(u, t: dict) -> dict | None:
    """The series map for a target, annotated for this user, plus the strip:
    {ok, series, series_asin, source, author, after, after_title, books, next,
    formats, multi}. None when the series can't be placed."""
    data = seriesmap.resolve(asin=t["asin"], title=t["title"], author=t["author"],
                             series=t["series"], series_asin=t["series_asin"])
    if not data:
        return None
    finished = picks.finished_works(u, abs_history(u))
    books = seriesmap.annotate(data, finished, default_author=t["author"], viewer=u["id"])
    if not books:
        return None
    start, nxt = seriesmap.strip(books, after=t["seq"])
    after = next((b for b in books if b["seq"] == start), None)
    return {"ok": True, "series": data.get("name") or t["series"], "series_asin": data.get("series_asin", ""),
            "source": data.get("source", ""), "author": t["author"] or _first(books[0]["author"]),
            "after": start, "after_title": after["title"] if after else "", "books": books, "next": nxt,
            "formats": formats.available(), "multi": formats.multi(), "cap": seriesmap.MAX_REQUEST,
            "needs_approval": _needs_approval(u)}


def _first(author: str) -> str:
    return (author or "").split(",")[0].strip()


@bp.route("/api/series/next")
@auth.login_required
def api_series_next():
    """The "Next in <series>" strip for a book page (or any series target):
    the next books after the highest one you've finished, each with its state
    (finished / in library / requested / upcoming / missing). Cached series
    maps, so a book page view is normally no catalogue call at all."""
    u = auth.current_user()
    t = _series_target(request.args)
    if not (t["asin"] or t["series_asin"] or (t["series"] and t["author"]) or (t["title"] and t["author"])):
        return jsonify({"ok": False, "detail": "Name a book or a series."}), 400
    return jsonify(_series_view(u, t) or {"ok": False, "detail": "Couldn't place this series."})


CONTINUE_DAYS = 14         # how recent a finish the Home "Continue" card follows


@bp.route("/api/series/continue")
@auth.login_required
def api_series_continue():
    """Home's "Continue <series>" card: the series of the book you finished
    most recently in Audiobookshelf (within CONTINUE_DAYS) that still has books
    after it. Tries at most three recent finishes, so a run of standalones
    can't turn one Home view into a dozen catalogue lookups."""
    u = auth.current_user()
    tried, seen = 0, set()
    for r in picks.finished_works(u, abs_history(u)):
        # the window is calendar days in the household's timezone
        if tz.days_ago(r.get("last_update") or 0) > CONTINUE_DAYS or not r.get("last_update") or tried >= 3:
            break
        name = (r.get("series") or "").strip() or (_infer_series(r["title"])[0] or "")
        if name and name.lower() in seen:
            continue
        tried += 1
        v = _series_view(u, {"asin": r.get("asin") or "", "series_asin": "", "series": name,
                             "author": _first(r.get("author")), "title": r["title"], "seq": None})
        if not v:
            continue
        seen.add((v["series"] or name).lower())
        if v["next"]:
            v["finished_title"] = r["title"]
            v["finished_days"] = tz.days_ago(r["last_update"])      # 0 = today, 1 = yesterday
            return jsonify(v)
    return jsonify({"ok": False})


def _remaining_formats(f) -> list[str]:
    """Formats a "Request remaining" asks for: the dialog's pick when it's on
    offer ('both' only when both are), else every format this install offers."""
    f = (f or "").strip().lower()
    if f in formats.available():
        return [f]
    return formats.available()


@bp.route("/api/series/request-remaining", methods=["POST"])
@auth.login_required
def api_series_request_remaining():
    """Request the books of a series you don't have yet — exactly the ones the
    confirm dialog listed (`keys`), never anything else by the author.

    The server recomputes the plan (seriesmap.plan) and sends only books that
    are BOTH in it and confirmed: finished, owned, already-requested and
    not-yet-released books are skipped, at most MAX_REQUEST per click. Each book
    gets its own request row per format, so each flips to available on its own.
    Chaptarr monitors just those books (one search per format) — unlike the
    deprecated /api/series/add, which monitored every row in the series and
    fell back to the author's whole backlist. Non-admins go through the usual
    approval gate: one pending row per book, one notification."""
    u = auth.current_user()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "detail": "Bad request."}), 400
    keys = body.get("keys")
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        return jsonify({"ok": False, "detail": "Confirm which books to request."}), 400
    t = _series_target(body)
    if not (t["asin"] or t["series_asin"] or (t["series"] and t["author"]) or (t["title"] and t["author"])):
        return jsonify({"ok": False, "detail": "Name a book or a series."}), 400
    wait = _cooldown("series_remaining", 10)
    if wait:
        return jsonify({"ok": False, "detail": f"Just requested — try again in {wait}s."}), 429
    v = _series_view(u, t)
    if not v:
        return jsonify({"ok": False, "detail": "Couldn't place this series in the catalogue."})
    fmts = _remaining_formats(body.get("format"))
    want = set(keys)
    # confirmed AND in the plan, then capped: the dialog may tick a book past the
    # first ten (after unticking others), so cap after intersecting, not before
    todo = [b for b in seriesmap.plan(v["books"], fmts, cap=len(v["books"]))["todo"]
            if b["key"] in want][:seriesmap.MAX_REQUEST]
    name = v["series"]
    if not todo:
        return jsonify({"ok": False, "detail": f"Nothing left to request in “{name}” — those books are in the "
                                               "library, requested or not out yet."})
    for b in todo:
        b["author"] = _first(b.get("author")) or v["author"]

    if _needs_approval(u):
        with db.conn() as c:
            for b in todo:
                fmt = "both" if len(b["need"]) > 1 else b["need"][0]
                db.record_request(c, u["id"], b, fmt, "pending_approval", "Waiting for an admin to approve",
                                  source="series_book")
        try:     # one heads-up for the batch, not one per book
            notify.request_pending({"title": f"{len(todo)} books from {name}", "author": v["author"],
                                    "format": fmts[0]}, u.get("username", "A user"), db.admin_emails(), _base_url())
        except Exception as e:
            log.debug("pending-approval notify failed: %s", e)
        return jsonify({"ok": True, "pending": True, "count": len(todo),
                        "detail": f"Sent {len(todo)} book{'s' if len(todo) != 1 else ''} from “{name}” "
                                  "for an admin to approve."})

    rows, sent = [], 0
    for f in fmts:
        for author in dict.fromkeys(b["author"] for b in todo if f in b["need"]):
            group = [b for b in todo if f in b["need"] and b["author"] == author]
            res = chaptarr.add_books(author, [{"title": b["title"], "asin": b.get("asin", "")} for b in group], fmt=f)
            for b, r in zip(group, res.get("books") or []):
                status = "available" if r.get("available") else ("handed" if r.get("ok") else "failed")
                sent += status == "handed"
                rows.append((u["id"], b.get("asin", ""), b["title"], b["author"], b.get("cover", ""), status,
                             r.get("detail", ""), res.get("ref", ""), "series_book", f))
    with db.conn() as c:
        for r in rows:          # one row per book + format: a failed earlier try is updated, not duplicated
            db.record_request(c, r[0], {"asin": r[1], "title": r[2], "author": r[3], "cover": r[4]},
                              r[9], r[5], r[6], r[7], r[8])
    failed = [r for r in rows if r[5] == "failed"]
    if sent:
        detail = f"Requested {len(todo)} book{'s' if len(todo) != 1 else ''} from “{name}” — Chaptarr is searching now."
        if failed:
            detail += f" {len(failed)} couldn't be sent — see Requests."
    else:
        detail = failed[0][6] if failed else "Chaptarr already has these."
    return jsonify({"ok": bool(sent) or not failed, "count": len(todo), "detail": detail,
                    "results": [{"title": r[2], "format": r[9], "status": r[5], "detail": r[6]} for r in rows]})


@bp.route("/api/series/repair", methods=["POST"])
@auth.admin_required
def api_series_repair():
    """Find library books whose series field is blank but recoverable from the
    title, and (when apply=true) write the series back to Audiobookshelf so it's
    fixed everywhere — not just inferred on the Up Next display. Calibre-Web has
    no write API, so only ABS audiobook items can be repaired. Admin-only."""
    apply_now = bool((request.get_json(silent=True) or {}).get("apply"))
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT item_id,title,format FROM library WHERE gone_at IS NULL AND (series IS NULL OR series='')")]
    proposals = []
    for r in rows:
        # only ABS audiobook items have a writable, un-namespaced id
        if (r.get("format") or "audiobook") != "audiobook" or ":" in (r["item_id"] or ""):
            continue
        name, seq = _infer_series(r["title"])
        if name:
            proposals.append({"item_id": r["item_id"], "title": r["title"], "series": name, "seq": seq})
    if not apply_now:
        return jsonify({"ok": True, "count": len(proposals), "proposals": proposals[:60]})
    written = sum(1 for p in proposals if absclient.set_series(p["item_id"], p["series"], p["seq"]))
    if written:
        try:
            from . import scheduler
            scheduler.refresh_library()
        except Exception as e:
            log.debug("post-repair refresh failed: %s", e)
    return jsonify({"ok": True, "written": written, "count": len(proposals),
                    "detail": f"Repaired {written} of {len(proposals)} item(s) in Audiobookshelf."})


@bp.route("/api/series/add", methods=["POST"])
@auth.login_required
def api_series_add():
    """Get the rest of a series: hand its author to Chaptarr, which monitors and
    searches all their books (covering the remaining entries in the series).

    DEPRECATED — the UI uses /api/series/request-remaining. This monitors every
    row Chaptarr lists for the series (owned books included), falls back to the
    author's WHOLE backlist when Chaptarr doesn't list the series, and records
    one "Full series: X" request that never flips to available. Kept, unchanged,
    for API callers; responses carry a `Deprecation: true` header."""
    resp = make_response(_series_add())
    resp.headers["Deprecation"] = "true"
    return resp


def _series_add():
    u = auth.current_user()
    body = request.get_json(force=True)
    name = (body.get("series") or "").strip()
    author = (body.get("author") or "").split(",")[0].strip()
    fmt = body.get("format") or "audiobook"
    if not name or not author:
        return jsonify({"ok": False, "detail": "Series needs an author Stackarr can look up."}), 400
    # non-admins (non-trusted) can't bulk-grab a whole series past the approval gate
    if _needs_approval(u):
        return jsonify(_queue_for_approval(u, {"title": name, "author": author, "format": fmt}, "series"))
    fmts = ["audiobook", "ebook"] if fmt == "both" else [fmt]
    results = {f: chaptarr.add_and_search(name, author, fmt=f, series=name) for f in fmts}
    # one request row per format so a partial success (e.g. audiobook ok, ebook
    # fail) isn't overwritten and hidden by the last iteration's result.
    with db.conn() as c:
        for f in fmts:
            rf = results[f]
            c.execute("INSERT INTO requests (user_id,title,author,status,detail,source,format) VALUES (?,?,?,?,?,?,?)",
                      (u["id"], f"Full series: {name}", author,
                       "handed" if rf.get("ok") else "failed", rf.get("detail", ""), "series", f))
    oks = [f for f in fmts if results[f].get("ok")]
    if fmt == "both":
        res = {"ok": bool(oks),
               "detail": (f"Sent “{name}” to Chaptarr as {' + '.join(oks)}." if oks
                          else results[fmts[-1]].get("detail", "Couldn't add this series right now."))}
    else:
        res = results[fmts[0]]
    return jsonify(res)


def _shelves_data(u):
    """Want / Reading / Read shelves, with 'Reading' auto-populated from
    in-progress state across every connected library (ABS + ebook sources +
    Hardcover), merged with anything set manually. Returns (shelves, counts)."""
    shelves = {s: db.shelf_list(u["id"], s) for s in ("reading", "want", "read")}
    seen_titles = {(it.get("title") or "").strip().lower() for it in shelves["reading"]}
    # A book you've finished (on the 'read' shelf) must not resurface as
    # "currently reading" just because an external source still reports progress.
    # Match on the shelf's stored rkey AND the title+author slug, since the auto
    # sources key on the library asin while the read shelf often keys on the slug.
    read_keys = set()
    for r in shelves["read"]:
        if r.get("rkey"):
            read_keys.add(r["rkey"])
        if r.get("title"):
            read_keys.add(db.rating_key("", r["title"], r.get("author", "")))

    def _listed(shelf, title, author):
        """Is this work already on the shelf? Same author + titles.matches, so a
        library that holds a book twice ("The Lamplighter" and "The Lamplighter
        (The Ember Archive Book 2)", or "09 Bloodline") shows one card."""
        a = _first(author).lower()
        same = [{"title": _LEADING_NUM.sub("", x.get("title") or "")} for x in shelf
                if a and a in (x.get("author") or "").lower()]
        return bool(same and titles.matches(same, _LEADING_NUM.sub("", title)))

    def _fix(title, author, series, seq, rkey):
        """(title to show, id) for a library book: the cached series map can
        put a right title and ASIN on a mangled one ("Reape" → "Reaper")."""
        t = (title or "").strip()
        w = series_work(t, author, series, seq) if (series or seq is not None or not _real_book_id(rkey)) else None
        return display_title(t, w), (rkey if _real_book_id(rkey) else (w or {}).get("asin", "") or rkey)

    def _add_reading(title, author, cover, fmt, rkey="", series="", seq=None):
        title, rkey = _fix(title, author, series, seq, rkey)
        t = (title or "").strip()
        if not t or t.lower() in seen_titles or _listed(shelves["reading"], t, author):
            return
        if (rkey and rkey in read_keys) or db.rating_key("", t, author) in read_keys \
                or _listed(shelves["read"], t, author):
            return
        seen_titles.add(t.lower())
        shelves["reading"].append({"rkey": rkey or db.rating_key("", t, author), "title": t,
                                   "author": author or "", "cover": cover or "", "format": fmt, "auto": True})

    # The 'read' shelf mirrors 'reading': seed it from what you've actually
    # FINISHED across connected libraries, merged with anything shelved by hand.
    # Without this the Home "Read" shelf sat at 0 while the goal ring (which reads
    # finish dates) showed the right count — finished audiobooks never landed here.
    read_titles = {(it.get("title") or "").strip().lower() for it in shelves["read"]}

    def _add_read(title, author, cover, fmt, rkey="", series="", seq=None):
        title, rkey = _fix(title, author, series, seq, rkey)
        t = (title or "").strip()
        if not t or t.lower() in read_titles or _listed(shelves["read"], t, author):
            return
        read_titles.add(t.lower())
        shelves["read"].append({"rkey": rkey or db.rating_key("", t, author), "title": t,
                                "author": author or "", "cover": cover or "", "format": fmt, "auto": True})
    try:
        with db.conn() as c:
            lib = {r["item_id"]: dict(r) for r in c.execute(
                "SELECT item_id,title,author,asin,format,series,series_seq FROM library")}
        fetched = 0
        for h in abs_history(u):
            m = lib.get(h["item_id"]) or {}
            if not m.get("title") and h.get("reading") and fetched < 4:
                # started before the next library refresh has seen the item
                # (it was added today): ask ABS for it, a few per render at most
                fetched += 1
                m = absclient.item_detail(h["item_id"]) or {}
            if not m.get("title"):
                continue
            # pass the real ASIN as the key so the card links to the book page
            rk = (m.get("asin") or "").strip()
            ser = (m.get("series") or "", m.get("series_seq"))
            if h.get("finished"):
                _add_read(m["title"], m.get("author", ""), url_for("main.cover", item_id=h["item_id"]),
                          m.get("format") or "audiobook", rk, *ser)
            elif h.get("reading", (h.get("progress") or 0) > 0.02):
                _add_reading(m["title"], m.get("author", ""), url_for("main.cover", item_id=h["item_id"]),
                             m.get("format") or "audiobook", rk, *ser)
        if formats.show("ebook"):
            from . import backends
            for be in backends.sources("ebook"):
                try:
                    for h in reading_history(be, u):
                        m = lib.get(h["item_id"]) or {}
                        if not m.get("title"):
                            continue
                        ser = (m.get("series") or "", m.get("series_seq"))
                        if h.get("finished"):
                            _add_read(m["title"], m.get("author", ""), "", "ebook", (m.get("asin") or "").strip(), *ser)
                        elif 0.02 < (h.get("progress") or 0) < 1:
                            _add_reading(m["title"], m.get("author", ""), "", "ebook", (m.get("asin") or "").strip(), *ser)
                except Exception:
                    pass
        for it in ebookmeta.hardcover_reading(ebookmeta.hardcover_token()):
            _add_reading(it.get("title", ""), it.get("author", ""), "", "ebook")
    except Exception as e:
        log.debug("shelves auto-reading failed: %s", e)
    counts = dict(db.shelf_counts(u["id"]))
    counts["reading"] = len(shelves["reading"])
    counts["read"] = len(shelves["read"])
    return shelves, counts


@bp.route("/shelves")
@auth.login_required
def shelves_page():
    # Shelves are now part of the Home hub — keep the URL working for bookmarks.
    return redirect(url_for("main.home_page"))


@bp.route("/api/adventurousness", methods=["POST"])
@auth.login_required
def api_adventurousness():
    """Comfort (0) ↔ Discovery (100) dial — biases familiar vs new-author lanes."""
    u = auth.current_user()
    try:
        n = max(0, min(100, int(request.get_json(force=True).get("value", 50))))
    except (ValueError, TypeError):
        n = 50
    db.set_meta(f"adventurousness_{u['id']}", str(n))
    return jsonify({"ok": True, "value": n})


@bp.route("/api/vibes", methods=["POST"])
@auth.login_required
def api_vibes():
    """Mood-aware cold start: the user picks a few vibes; we store them as
    positive mood signals so even a brand-new account gets shaped picks."""
    u = auth.current_user()
    moods = request.get_json(force=True).get("moods") or []
    with db.conn() as c:
        for m in moods[:8]:
            m = str(m).strip().lower()
            if m:
                c.execute("INSERT INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?) "
                          "ON CONFLICT(user_id,kind,value) DO UPDATE SET weight=signals.weight+3",
                          (u["id"], "mood", m, 3, "vibe pick"))
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                  (u["id"], "vibes_done", "1", 0, "picked vibes"))
    return jsonify({"ok": True})


@bp.route("/api/account/password", methods=["POST"])
@auth.login_required
def api_account_password():
    u = auth.current_user()
    b = request.get_json(force=True)
    new = (b.get("password") or "").strip()
    if len(new) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    # if a password is already set, require the current one
    if u.get("password_hash") and not db.verify_local(u["username"], b.get("current") or ""):
        return jsonify({"error": "Current password is wrong."}), 403
    db.set_password(u["id"], new)
    return jsonify({"ok": True})


@bp.route("/api/account/email", methods=["POST"])
@auth.login_required
def api_account_email():
    u = auth.current_user()
    db.set_email(u["id"], (request.get_json(force=True).get("email") or "").strip())
    return jsonify({"ok": True})


@bp.route("/api/account/link", methods=["POST"])
@auth.login_required
def api_account_link():
    """Link an external sign-in method to the current account."""
    u = auth.current_user()
    b = request.get_json(force=True)
    ok = auth.link_provider(u, (b.get("provider") or "").strip(),
                            b.get("username", ""), b.get("password", ""))
    if not ok:
        return jsonify({"error": "Couldn't verify those credentials, or that account is already linked to someone else."}), 400
    return jsonify({"ok": True})


@bp.route("/api/account/unlink", methods=["POST"])
@auth.login_required
def api_account_unlink():
    u = auth.current_user()
    provider = (request.get_json(force=True).get("provider") or "").strip()
    # don't let someone strip their ONLY way back in
    links = db.links_for(u["id"])
    if not u.get("password_hash") and len(links) <= 1:
        return jsonify({"error": "Set a password first — this is your only way to sign in."}), 400
    db.link_remove(provider, u["id"])
    if provider == "abs":
        db.update_abs(u["id"], None, "")
    return jsonify({"ok": True})


@bp.route("/api/goal", methods=["POST"])
@auth.login_required
def api_goal():
    u = auth.current_user()
    try:
        n = max(0, int(request.get_json(force=True).get("goal", 0)))
    except (ValueError, TypeError):
        n = 0
    db.set_meta(f"goal_{u['id']}", str(n))
    return jsonify({"ok": True, "goal": n})


@bp.route("/upcoming")
@auth.login_required
def upcoming_page():
    """New & upcoming books from authors you read — a browsable radar."""
    u = auth.current_user()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM suggestions WHERE user_id=? AND lane='upcoming' AND status='pending' ORDER BY extra", (u["id"],))]
        for r in rows:
            r["available"] = _owned(c, r["asin"], r["title"], r["author"])
    today = str(tz.today())
    return render_template("upcoming.html", rows=rows, today=today)


@bp.route("/author/<path:name>")
@auth.login_required
def author_page(name):
    """An author hub: their catalogue, what you own, and a Follow toggle that
    drives the new-release radar."""
    name = name.strip()
    books = audible.by_author(name, num=40)
    if formats.show("ebook") and not formats.show("audiobook"):
        books = ebookmeta.by_author(name, num=40)
        for b in books:
            b["asin"] = b.get("id", "")
    uniq = [b for b in _clean_catalogue(books) if b.get("asin")]
    _annotate_status(uniq)
    uniq.sort(key=lambda b: b.get("release_date") or "", reverse=True)
    u = auth.current_user()
    following = bool(db.get_meta(f"follow_{u['id']}_{name.lower()}"))
    return render_template("author.html", author=name, books=uniq, following=following)


@bp.route("/api/follow", methods=["POST"])
@auth.login_required
def api_follow():
    u = auth.current_user()
    b = request.get_json(force=True)
    name = (b.get("author") or "").strip()
    if not name:
        return jsonify({"error": "author required"}), 400
    key = f"follow_{u['id']}_{name.lower()}"
    now = bool(db.get_meta(key))
    db.set_meta(key, "" if now else "1")
    # a follow is a positive author signal too (and seeds the radar)
    if not now:
        with db.conn() as c:
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "author", name, 4, f"followed: {name}"))
    return jsonify({"ok": True, "following": not now})


@bp.route("/narrator/<path:name>")
@auth.login_required
def narrator_page(name):
    """A narrator hub: audiobooks they narrate + what you own."""
    name = name.strip()
    uniq = [b for b in _clean_catalogue(audible.search(name, num=40))
            if name.lower() in (b.get("narrator") or "").lower() and b.get("asin")]
    _annotate_status(uniq)
    return render_template("narrator.html", narrator=name, books=uniq)


@bp.route("/api/author/add", methods=["POST"])
@auth.login_required
def api_author_add():
    u = auth.current_user()
    author = request.get_json(force=True).get("author", "").strip()
    if not author:
        return jsonify({"error": "author required"}), 400
    if _needs_approval(u):
        return jsonify(_queue_for_approval(u, {"title": f"All books by {author}", "author": author}, "author"))
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
    is_admin = auth.current_user()["role"] == "admin"
    # Instance-wide integration secrets (service creds, SMTP password, webhook
    # URLs). These only ever render inside `{% if is_admin %}` blocks, but we ALSO
    # withhold the values server-side so a single future ungated field can't dump
    # the ABS admin token / SMTP password to a non-admin who curls /settings.
    conn = {"abs_url": g("abs_url", config.ABS_URL),
            "abs_admin_token": g("abs_admin_token", config.ABS_ADMIN_TOKEN),
            "abs_public_url": g("abs_public_url", config.ABS_PUBLIC_URL),
            "chaptarr_url": g("chaptarr_url", config.CHAPTARR_URL),
            "chaptarr_api_key": g("chaptarr_api_key", config.CHAPTARR_API_KEY),
            "chaptarr_root_folder": g("chaptarr_root_folder", config.CHAPTARR_ROOT_FOLDER),
            "chaptarr_quality_profile_id": g("chaptarr_quality_profile_id", str(config.CHAPTARR_QUALITY_PROFILE_ID)),
            "chaptarr_metadata_profile_id": g("chaptarr_metadata_profile_id", str(config.CHAPTARR_METADATA_PROFILE_ID)),
            "chaptarr_webhook_token": db.get_meta("chaptarr_webhook_token") or _ensure_webhook_token(),
            "public_url": db.get_meta("public_url", ""),
            "kavita_url": g("kavita_url", config.KAVITA_URL),
            "kavita_api_key": g("kavita_api_key", config.KAVITA_API_KEY),
            "calibreweb_url": g("calibreweb_url", config.CALIBREWEB_URL),
            "calibreweb_user": g("calibreweb_user", config.CALIBREWEB_USER),
            "calibreweb_pass": g("calibreweb_pass", config.CALIBREWEB_PASS),
            "komga_url": g("komga_url", config.KOMGA_URL),
            "komga_user": g("komga_user", config.KOMGA_USER),
            "komga_pass": g("komga_pass", config.KOMGA_PASS),
            "opds_url": g("opds_url", config.OPDS_URL),
            "opds_user": g("opds_user", config.OPDS_USER),
            "opds_pass": g("opds_pass", config.OPDS_PASS)} if is_admin else {}
    return render_template("settings.html",
                           email_configured=notify.email_configured(),
                           email_enabled=db.get_meta("email_enabled", "0") == "1",
                           email_theme=notify.current_theme(),
                           email_frequency=db.get_meta("email_frequency", "immediate"),
                           discord_configured=bool(db.setting("discord_webhook", config.DISCORD_WEBHOOK)),
                           discord_webhook=db.setting("discord_webhook", config.DISCORD_WEBHOOK) if is_admin else "",
                           discord_enabled=db.get_meta("discord_enabled", "0") == "1",
                           notify_avail_enabled=db.get_meta("notify_avail_enabled", "0") == "1",
                           notify_newrelease_enabled=db.get_meta("notify_newrelease_enabled", "0") == "1",
                           custom_webhook=db.setting("custom_webhook", "") if is_admin else "",
                           themes=list(notify.THEMES),
                           auto_add_level=db.get_meta("auto_add_level", "off"),
                           interval_hours=db.get_meta("suggest_interval_hours", str(config.SUGGEST_INTERVAL_HOURS)),
                           language=db.get_meta("language", config.TARGET_LANGUAGE),
                           languages=["english","german","spanish","french","italian","dutch","portuguese","japanese","any"],
                           smtp=notify.smtp_settings() if is_admin else {},
                           conn=conn,
                           abs_ebooks=db.get_meta("abs_ebooks", "1" if config.ABS_EBOOKS else "0") == "1",
                           koreader_sync=db.get_meta("koreader_sync", "1" if config.KOREADER_SYNC else "0") == "1",
                           reading={"goodreads_rss": g("goodreads_rss", config.GOODREADS_RSS) if is_admin else "",
                                    "hardcover_token": g("hardcover_token", config.HARDCOVER_TOKEN) if is_admin else ""},
                           hide_rated_history=db.get_pref(auth.current_user()["id"], "hide_rated_history", "0") == "1",
                           format_mode=formats.mode(),
                           timezone=tz.name(), timezones=tz.zones(), timezone_default=tz.default_name(),
                           release_check=db.get_meta("release_check", "1") == "1",
                           cross_format_taste=db.get_pref(auth.current_user()["id"], "cross_format_taste", "0") == "1",
                           adventurousness=db.get_meta(f"adventurousness_{auth.current_user()['id']}", str(config.ADVENTUROUSNESS)),
                           log_level=config.LOG_LEVEL,
                           account=_account_ctx(auth.current_user()),
                           require_approval=db.get_meta("require_approval", "1") == "1",
                           user_sync=db.get_meta("user_sync", "0") == "1",
                           manage_users=_manage_users_ctx() if auth.current_user()["role"] == "admin" else [],
                           notify_prefs=_notify_prefs_ctx(auth.current_user()),
                           smtp_ready=notify.smtp_ready(),
                           is_admin=is_admin)


def _manage_users_ctx() -> list[dict]:
    """User list for the admin Users panel, with each user's trusted flag."""
    out = []
    for u in db.all_users():
        out.append({**u, "trusted": db.get_pref(u["id"], "trusted", "0") == "1"})
    return out


def _notify_prefs_ctx(u: dict) -> dict:
    return {k: db.get_pref(u["id"], f"notify_{k}", "1") == "1"
            for k in ("approved", "available", "denied")}


def _account_ctx(u: dict) -> dict:
    """Identity + linked sign-in methods for the Settings → Account section."""
    linked = {l["provider"] for l in db.links_for(u["id"])}
    provs = []
    for p in auth.login_providers():           # connected sources that can authenticate
        provs.append({"id": p["id"], "label": p["label"], "linked": p["id"] in linked})
    return {"username": u["username"], "email": u.get("email") or "",
            "role": u["role"], "has_password": bool(u.get("password_hash")),
            "providers": provs}


# ------------------------------------------------------------------- api ---
def _library_works(c, title, author, fmt=None) -> list[dict]:
    """The library rows (item_id, title, author, format) that are this book, by
    the same author — one per format/source it's held in. Library titles are
    often decorated where the catalogue's aren't ("The Captain: The Last
    Horizon, Book 1", "House of Blades (Unabridged)") and usually carry no
    ASIN, so titles are compared with titles.matches(), not exactly. No author,
    no match: title-only matching gives false positives on common one-word
    titles (e.g. 'Emergence')."""
    a = (author or "").split(",")[0].strip().lower()
    if not a or not (title or "").strip():
        return []
    fclause = " AND format=?" if fmt else ""
    rows = [dict(r) for r in c.execute(
        "SELECT item_id, title, author, format FROM library WHERE gone_at IS NULL "
        "AND lower(author) LIKE ?" + fclause, (f"%{a}%",) + ((fmt,) if fmt else ()))]
    return titles.matches(rows, title)


def _owned(c, asin, title, author, fmt=None) -> bool:
    """True only if the book is really in the library — ASIN match, or the same
    work by the same author (see _library_works). When `fmt` is given, the match
    is restricted to that format, so owning the audiobook doesn't count as
    owning the ebook (and the other format can still be grabbed)."""
    fclause = " AND format=?" if fmt else ""
    fargs = (fmt,) if fmt else ()
    if asin and c.execute("SELECT 1 FROM library WHERE gone_at IS NULL AND asin=? AND asin<>''" + fclause,
                          (asin,) + fargs).fetchone():
        return True
    return bool(_library_works(c, title, author, fmt))


def _ensure_webhook_token() -> str:
    """A stable per-install token for the Chaptarr Connect webhook URL."""
    import secrets
    t = db.get_meta("chaptarr_webhook_token", "")
    if not t:
        t = secrets.token_urlsafe(16)
        db.set_meta("chaptarr_webhook_token", t)
    return t


def _req_format(body) -> str:
    """The media format an action applies to: the explicit choice when both
    formats are active, else the single active format (assumed)."""
    f = (body or {}).get("format")
    if formats.multi() and f in ("audiobook", "ebook"):
        return f
    return formats.primary()


_BOOK_ID = re.compile(r"(?:[A-Z0-9]{10}|(?:gb|ol):\S+)")


def _real_book_id(v) -> str:
    """`v` if it names a catalogue page (an ASIN, or a gb:/ol: ebook id), else
    "" — "t-…" rating-key slugs and blanks don't."""
    v = (v or "").strip()
    return v if _BOOK_ID.fullmatch(v) else ""


def _series_rows() -> list[dict]:
    """Every book of every series map Stackarr has cached (meta "ser:a:…"), as
    {title, asin, author, series, seq, format} rows — fetched once per request.
    Up Next and the book pages fill these maps, so for a series the household
    reads, the ASIN and the catalogue's own title of each position are local."""
    from flask import g, has_request_context
    if has_request_context() and getattr(g, "_series_rows", None) is not None:
        return g._series_rows
    import json
    out = []
    with db.conn() as c:
        for r in c.execute("SELECT v FROM meta WHERE k LIKE 'ser:a:%'"):
            try:
                v = (json.loads(r["v"]) or {}).get("v") or {}
            except (ValueError, TypeError, AttributeError):
                continue
            for b in v.get("books") or []:
                if _real_book_id(b.get("asin")):
                    out.append({"title": b.get("title") or "", "asin": b["asin"], "author": b.get("author") or "",
                                "series": v.get("name") or "", "seq": b.get("seq"), "format": "audiobook"})
    if has_request_context():
        g._series_rows = out
    return out


def _series_key(name) -> str:
    return re.sub(r"^the\s+", "", titles.norm(name))


def series_work(title, author, series="", seq=None) -> dict | None:
    """The cached series-map entry for a library book: by its series + position
    when the library knows them, else by title within the author's maps. The
    library's own title can be mangled — Audiobookshelf holds Cradle #10 as
    "Reape" — so the position is the stronger evidence."""
    a = _first(author).lower()
    if not a:
        return None
    rows = [r for r in _series_rows() if a in (r["author"] or "").lower()]
    if series and seq is not None:
        try:
            pos = float(seq)
        except (TypeError, ValueError):
            pos = None
        hit = [r for r in rows if pos is not None and r["seq"] == pos and _series_key(r["series"]) == _series_key(series)]
        if hit:
            return hit[0]
    hit = titles.matches(rows, _LEADING_NUM.sub("", title or "")) if title else []
    return hit[0] if hit else None


def display_title(title, work: dict | None) -> str:
    """The title to show for a library book: the catalogue's when the library's
    is a truncation of it ("Reape" → "Reaper"), else the library's own."""
    t = (title or "").strip()
    if work and work.get("title"):
        nt, nw = titles.norm(t), titles.norm(work["title"])
        if len(nt) >= 3 and nw.startswith(nt) and nw != nt:
            return work["title"]
    return t


def _known_book_id(title, author, fmt="", series="", seq=None) -> str:
    """The catalogue id Stackarr already holds for (title, author), from its own
    tables — no network. Library rows rarely carry an ASIN, but the same work
    usually has one on a request, a suggestion, a rating or a shelf row, in a
    cached series map, or in the cache the /book/find resolver fills.
    Same-author, subtitle-tolerant title match (titles.matches), so "Unsouled
    (Cradle Book 1)" finds the "Unsouled" request's ASIN; `series` + `seq`
    place a book the library titles wrongly ("Reape", Cradle #10)."""
    a = _first(author).lower()
    if not a or not (title or "").strip():
        return ""
    cached = db.get_meta("bookid:" + db.rating_key("", titles.clean(title), a), "")
    if _real_book_id(cached):
        return cached
    like = (f"%{a}%",)
    with db.conn() as c:
        rows = []
        for q in ("SELECT title, asin, format FROM requests WHERE asin<>'' AND lower(author) LIKE ?",
                  "SELECT title, asin, format FROM suggestions WHERE asin<>'' AND lower(author) LIKE ?",
                  "SELECT title, asin, format FROM ratings WHERE asin<>'' AND lower(author) LIKE ?",
                  "SELECT title, rkey AS asin, format FROM shelf WHERE lower(author) LIKE ?",
                  "SELECT title, asin, format FROM library WHERE asin<>'' AND lower(author) LIKE ?"):
            rows += [dict(r) for r in c.execute(q, like) if _real_book_id(r["asin"])]
    hits = titles.matches(rows, _LEADING_NUM.sub("", title))
    if not hits:
        w = series_work(title, author, series, seq)
        return w["asin"] if w else ""
    # the page in the card's own format when there is one (an eBook card opens
    # the eBook page), else any id the work has
    same = [h for h in hits if (h.get("format") or "audiobook") == (fmt or "audiobook")]
    return (same or hits)[0]["asin"]


@bp.app_template_global("book_href")
def book_href(asin="", title="", author="", fmt="", series="", seq=None) -> str:
    """The Stackarr book page for a card — EVERY card, whatever it was built
    from. A real id links straight to /book/<id>; otherwise an id Stackarr
    already knows for the work (_known_book_id); otherwise /book/find, which
    resolves the title on click — and the background worker is asked to
    resolve it now, so the next render links straight to the book. Never
    "#": that sent 21 of the 23 Home "Read" cards back to Home."""
    bid = _real_book_id(asin) or _known_book_id(title, author, fmt, series, seq)
    if bid:
        return url_for("main.book_page", asin=bid)
    if not (title or "").strip():
        return url_for("main.discover_page")
    try:
        from . import scheduler
        scheduler.queue_job("bookid", title, _first(author), fmt or "")
    except Exception as e:
        log.debug("could not queue book id for %s: %s", title, e)
    return url_for("main.book_find", title=title, author=_first(author), fmt=fmt or "")


def resolve_book_id(title, author, fmt="") -> str:
    """ONE catalogue search for (title, author) — same author, same work, in
    the install's language — remembered in meta ("bookid:…"; a miss for a
    day). Used by /book/find on click and by the background worker."""
    import time
    author = _first(author)
    key = "bookid:" + db.rating_key("", titles.clean(title), author.lower())
    have = db.get_meta(key, "")
    if _real_book_id(have):
        return have
    if (have.startswith("none:") and time.time() - float(have[5:] or 0) < 86400) or not author:
        return ""
    bid = ""
    try:
        want = titles.clean(_LEADING_NUM.sub("", title))
        if fmt == "ebook" and not formats.show("audiobook"):
            cands = [dict(x, asin=x.get("id", "")) for x in ebookmeta.search(f"{want} {author}", num=5)]
        else:
            cands = audible.search(f"{want} {author}", num=8)
        cands = [x for x in _in_language(cands)
                 if _real_book_id(x.get("asin")) and author.lower() in (x.get("author") or "").lower()]
        hit = titles.matches(cands, want)
        bid = hit[0]["asin"] if hit else ""
    except Exception as e:
        log.debug("book id lookup %r failed: %s", title, e)
    db.set_meta(key, bid or f"none:{int(time.time())}")
    return bid


@bp.route("/book/find")
@auth.login_required
def book_find():
    """Open the book page for a title + author — the link target of a card with
    no catalogue id. What Stackarr knows first, then resolve_book_id(). When
    the catalogue can't place it, the Discover search for the title — a page
    about this book, never Home."""
    title = (request.args.get("title") or "").strip()
    author = _first(request.args.get("author"))
    fmt = (request.args.get("fmt") or "").strip()
    if not title:
        return redirect(url_for("main.discover_page"))
    bid = _known_book_id(title, author, fmt) or resolve_book_id(title, author, fmt)
    if bid:
        return redirect(url_for("main.book_page", asin=bid))
    return redirect(url_for("main.discover_page", q=f"{titles.clean(title)} {author}".strip()))


_ISO_LANG = {"en": "english", "de": "german", "es": "spanish", "fr": "french", "it": "italian",
             "nl": "dutch", "pt": "portuguese", "ja": "japanese"}


def _lang(b: dict) -> str:
    v = (b.get("language") or "").strip().lower()
    return _ISO_LANG.get(v.split("-")[0], v)


def _in_language(books: list[dict]) -> list[dict]:
    """Catalogue rows in the install's language (Settings → General →
    language, default English; "any" keeps everything). A row that names no
    language is kept. Audible's author catalogue for Matt Dinniman is 13
    English titles among 23 Spanish/French/German/Italian/Portuguese ones."""
    want = (db.get_meta("language", config.TARGET_LANGUAGE) or "english").lower()
    if want == "any":
        return list(books)
    return [b for b in books if not _lang(b) or _lang(b) == want]


def _clean_catalogue(books: list[dict]) -> list[dict]:
    """A catalogue list as the user should see it: their language only, and
    one card per work. A work's editions in the same language (a re-release,
    an "(Unabridged)" duplicate) collapse into the one with the most ratings;
    with "any" language each language keeps its own card. Order is kept."""
    out, at = [], {}
    for b in _in_language(books):
        k = (titles.parts(b.get("title") or "")[0], _first(b.get("author")).lower(), _lang(b))
        if not k[0]:
            continue
        if k in at:
            i = at[k]
            if (b.get("num_ratings") or 0) > (out[i].get("num_ratings") or 0):
                out[i] = b
            continue
        at[k] = len(out)
        out.append(b)
    return out


def _local_cover(asin, title, author, fmt="") -> str:
    """A cover URL for a book the catalogue gave no art for — never a network
    call on this request. Our own copy first (the library item's cover, by
    ASIN or title), else /coverart, which answers from the cover cache and
    queues anything unknown for the background worker. Dungeon Crawler Carl's
    US ASIN (B08V8B2CGV) isn't sold on the AU marketplace, so its book page
    had no cover although Audiobookshelf holds one."""
    real = asin if _real_book_id(asin) and not asin.startswith(("gb:", "ol:")) else ""
    ref = db.library_cover(title, author, real)
    if ref:
        return url_for("main.libcover", ref=ref) if ":" in ref else url_for("main.cover", item_id=ref)
    return url_for("main.coverart", asin=real, title=title or "", author=author or "", fmt=fmt or "")


def _cached_book(asin) -> dict:
    """Best-effort title/author/cover for a book Stackarr has seen, from the
    suggestions / requests / library tables — the fallback when a live catalogue
    lookup fails so the detail page degrades gracefully instead of 'Unknown'."""
    if not asin:
        return {}
    with db.conn() as c:
        # rating-key slug ("t-…") rows live keyed on rkey/asin in shelf + ratings,
        # so a 'currently reading' / rated book still resolves to a real page.
        for q in ("SELECT title, author, cover FROM suggestions WHERE asin=? ORDER BY id DESC LIMIT 1",
                  "SELECT title, author, cover FROM requests WHERE asin=? ORDER BY id DESC LIMIT 1",
                  "SELECT title, author, cover FROM shelf WHERE rkey=? LIMIT 1"):
            row = c.execute(q, (asin,)).fetchone()
            if row and (row["title"] or "").strip():
                return {"title": row["title"], "author": row["author"], "cover": row["cover"]}
        row = c.execute("SELECT title, author FROM ratings WHERE asin=? AND title<>'' LIMIT 1",
                        (asin,)).fetchone()
        if row:
            return {"title": row["title"], "author": row["author"]}
        row = c.execute("SELECT title, author FROM library WHERE asin=? AND asin<>'' LIMIT 1",
                        (asin,)).fetchone()
        if row:
            return {"title": row["title"], "author": row["author"]}
    return {}


def _state_for(asin, title, author):
    """The status key for one book (status.py) — kept for callers that only
    want the word."""
    return book_status(asin, title, author)["state"]


def _viewer():
    try:
        return auth.current_user()
    except RuntimeError:                     # outside a request (scheduler, tests)
        return None


def book_status(asin, title, author, release_date="") -> dict:
    """status.for_book() for the signed-in user."""
    u = _viewer()
    return status.for_book(asin, title, author, viewer=u["id"] if u else None, release_date=release_date)


def _annotate_status(books: list[dict]) -> list[dict]:
    """Give every catalogue row its status (status.py, one batch): `status`
    (the dict the templates' status_badge renders) and `state` (its key, which
    the front-end's cards read)."""
    u = _viewer()
    for b, st in zip(books, status.for_books(books, viewer=u["id"] if u else None)):
        b["status"], b["state"] = st, st["state"]
    return books


@bp.route("/api/discover")
@auth.login_required
def api_discover():
    try:
        pg = int(request.args.get("page", "0") or 0)
    except (ValueError, TypeError):
        pg = 0
    return jsonify(_annotate_status(discover.page(pg)))


def _search_catalog(q, num):
    """Search the catalogue of the active format(s). Ebook-only installs search
    the ebook catalogue (mapping its id into `asin` so the front-end's book
    links work); otherwise the audiobook catalogue. 'both' stays audiobook-led
    for the quick box — full ebook search lives in the format-aware lanes."""
    if formats.active() == ["ebook"]:
        from . import ebookmeta
        out = []
        for x in _clean_catalogue(ebookmeta.search(q, num=num)):
            x = dict(x, asin=x.get("id", ""))
            if x["asin"]:
                out.append(x)
        return out
    return [x for x in _clean_catalogue(audible.search(q, num=num)) if x.get("asin")]


_SUGGEST_CACHE: dict = {}


@bp.route("/api/suggest")
@auth.login_required
def api_suggest():
    """Typeahead for the top search box — titles/authors/series as you type."""
    import time
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    # typing re-asks for the same prefixes; each is a ~0.6 s catalogue search
    key = q.lower()
    hit = _SUGGEST_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return jsonify(hit[1])
    out = [{"asin": x["asin"], "title": x["title"], "author": x["author"],
            "series": x.get("series", ""), "cover": x["cover"]} for x in _search_catalog(q, 7)]
    if len(_SUGGEST_CACHE) > 400:
        _SUGGEST_CACHE.clear()
    _SUGGEST_CACHE[key] = (time.time(), out)
    return jsonify(out)


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
    # Propagate: mark finished in ABS (if in this user's library) + unmonitor in Chaptarr.
    abs_finished, chaptarr_unmonitored = False, False
    tok = u.get("abs_token")
    if tok and title:
        try:
            item_id = absclient.find_item(title, author)
            if item_id:
                abs_finished = absclient.set_finished(tok, item_id, True)
        except Exception:
            pass
    try:
        chaptarr_unmonitored = chaptarr.mark_read(title, author)
    except Exception:
        pass
    return jsonify({"ok": True, "matched": title,
                    "abs_finished": abs_finished, "chaptarr_unmonitored": chaptarr_unmonitored})


@bp.route("/api/search")
@auth.login_required
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    return jsonify(_annotate_status(_search_catalog(q, 12)))


def _needs_approval(user) -> bool:
    """Should this user's grab wait for an admin? Admins self-approve; a global
    'manual approval' switch (default on) requires it for everyone else, unless
    that user is marked trusted."""
    if not user or user.get("role") == "admin":
        return False
    if db.get_meta("require_approval", "1") != "1":
        return False
    return db.get_pref(user["id"], "trusted", "0") != "1"


def _base_url() -> str:
    try:
        return db.get_meta("public_url", "") or request.host_url.rstrip("/")
    except Exception:
        return db.get_meta("public_url", "")


# Lightweight in-process cooldown for the expensive endpoints (library re-scan,
# recommender run). Keyed per user so one account can't hammer the shared
# ABS/Chaptarr scans; in-memory is fine on single-process waitress.
_LAST_HEAVY: dict = {}


def _cooldown(action: str, seconds: int) -> int:
    """0 if `action` is allowed now (and stamps it), else the seconds to wait."""
    import time
    u = auth.current_user()
    key = (u["id"] if u else request.remote_addr, action)
    now, last = time.time(), _LAST_HEAVY.get((u["id"] if u else request.remote_addr, action), 0)
    if now - last < seconds:
        return int(seconds - (now - last)) + 1
    _LAST_HEAVY[key] = now
    return 0


def _queue_for_approval(user, item, source):
    """Record a request that needs an admin's approval and ping the admins.
    `item` has title/author and optionally asin/cover/format. Used by both
    single-book grabs and the bulk series/author 'add all' paths."""
    fmt = item.get("format", "audiobook")
    with db.conn() as c:
        if db.open_request(c, user["id"], item.get("asin", ""), item["title"], fmt):
            return {"ok": True, "pending": True, "duplicate": True,
                    "detail": "Already requested — it's waiting for an admin or with Chaptarr."}
        db.record_request(c, user["id"], item, fmt, "pending_approval", "Waiting for an admin to approve",
                          source=source)
    try:
        notify.request_pending(item, user.get("username", "A user"), db.admin_emails(), _base_url())
    except Exception as e:
        log.debug("pending-approval notify failed: %s", e)
    return {"ok": True, "pending": True, "detail": "Request sent — an admin will approve it shortly."}


def _request_format(book) -> str:
    """The media format(s) a request applies to — 'audiobook' | 'ebook' | 'both'.

    A catalogue row's `format` is Audible's EDITION string ("Unabridged",
    "Original_Recording"), NOT a Stackarr media type, and the media cards post
    the whole row. Taken verbatim it lands in requests.format and hands off a
    single format, so a 'both' install silently never grabbed the ebook half.
    Only a real, currently-offered media type is honoured (so an explicit pick
    from the detail page still wins); anything else falls back to the install's
    configured mode."""
    f = ((book or {}).get("format") or "").strip().lower()
    if f in formats.available():
        return f
    if f == "both" and formats.multi():
        return "both"
    return formats.mode()


def _hand_to_chaptarr(user_id, book, source):
    fmt = _request_format(book)
    user = db.get_user(user_id)
    # Hold non-admin, non-trusted requests for approval instead of grabbing.
    # A 'both' request stays ONE pending row — api_request_approve fans it out.
    if _needs_approval(user):
        return _queue_for_approval(user, dict(book, format=fmt), source)
    fmts = formats.available() if fmt == "both" else [fmt]
    if len(fmts) > 1:
        rmap = {f: _grab_one(user_id, book, f, source) for f in fmts}
        oks = [f for f in fmts if rmap[f].get("ok")]
        return {"ok": bool(oks), "ref": "",
                "detail": (f"Sent as {' + '.join(oks)}." if oks
                           else rmap[fmts[-1]].get("detail", "Couldn't add right now."))}
    return _grab_one(user_id, book, fmts[0], source)


def _grab_one(user_id, book, fmt, source):
    """Hand ONE media format to Chaptarr and record its request row."""
    # Bypass Chaptarr if the book is already in a connected library (the user may
    # have added it straight to Audiobookshelf / Kavita / Calibre-Web). Record it
    # as available rather than redundantly asking Chaptarr to grab it.
    with db.conn() as c:
        if _owned(c, book.get("asin", ""), book.get("title", ""), book.get("author", ""), fmt=fmt):
            db.record_request(c, user_id, book, fmt, "available", "Already in your library", source=source)
            return {"ok": True, "detail": "Already in your library — marked available."}
        # asked again while Chaptarr is still on it: no second row, no second search
        if db.open_request(c, user_id, book.get("asin", ""), book.get("title", ""), fmt):
            return {"ok": True, "duplicate": True, "detail": "Already requested — Chaptarr is on it."}
    res = chaptarr.add_and_search(book["title"], book.get("author", ""), book.get("asin", ""), fmt=fmt)
    # Chaptarr already holds a file for the work (on any of its rows): done
    st = "available" if res.get("available") else ("handed" if res["ok"] else "failed")
    with db.conn() as c:
        db.record_request(c, user_id, book, fmt, st, res.get("detail", ""), res.get("ref", ""), source)
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
        # a suggestion can have no ASIN (ebook/inferred picks) — fall back to the
        # title/author key so the negative signal is never silently dropped (the
        # signals.value column is NOT NULL) and the title sticks as "don't show".
        sig_val = row.get("asin") or db.rating_key("", row.get("title", ""), row.get("author", ""))
        # 'approve' is NOT committed here — we hand off first (below) and only
        # consume the suggestion on success, so a failed grab leaves it re-approvable.
        if verdict in ("reject", "read"):
            c.execute("UPDATE suggestions SET status='rejected',decided_at=datetime('now','localtime') WHERE id=?",
                      (sid,))
        if verdict == "reject":
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", sig_val, -3, f"passed: {row['title']}"))
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
                      (u["id"], "asin", sig_val, -1, f"already read: {row['title']}"))
    if verdict == "approve":
        res = _hand_to_chaptarr(u["id"], row, "suggestion")
        # consume the suggestion ONLY on a real handoff — not when it was merely
        # queued for admin approval (ok:True, pending:True); else a later denial
        # leaves the pick 'approved' and it silently vanishes from the user's lane.
        if res.get("ok") and not res.get("pending"):
            with db.conn() as c:
                c.execute("UPDATE suggestions SET status='approved',decided_at=datetime('now','localtime') WHERE id=?",
                          (sid,))
        return jsonify(res)
    return jsonify({"status": "rejected"})


@bp.route("/api/mark-read", methods=["POST"])
@auth.login_required
def api_mark_read():
    """Manually tell Stackarr you've already read a title — positive taste
    seed, no download. Also writes finished to ABS (if it's in this user's
    library) and unmonitors it in Chaptarr so it won't be grabbed."""
    u = auth.current_user()
    body = request.get_json(force=True)
    title, authr = body.get("title", "").strip(), body.get("author", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    fmt = _req_format(body)
    hit = ebookmeta.search(f"{title} {authr}", num=1) if fmt == "ebook" else audible.search(f"{title} {authr}", num=1)
    b = (hit[0] if hit else {"asin": "", "title": title, "author": authr})
    bid = b.get("asin") or b.get("id") or ""
    with db.conn() as c:
        if b.get("author"):
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?)",
                      (u["id"], "author", b["author"].split(",")[0], 3, f"marked read: {b['title']}", fmt))
        if b.get("series"):
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?)",
                      (u["id"], "series", b["series"], 2, f"marked read: {b['title']}", fmt))
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?)",
                  (u["id"], "asin", bid or title, -1, f"already read: {b['title']}", fmt))
    # also put it on the 'read' shelf so it counts toward goal + heatmap
    rk = db.rating_key(bid if not bid.startswith(("gb:", "ol:")) else "", b.get("title", title), b.get("author", authr))
    # prefer the on-screen cover the client sent over the (often coverless) re-search
    # hit, so the read shelf stores a real cover instead of needing a /coverart lookup.
    cover = b.get("cover") or body.get("cover", "")
    db.shelf_set(u["id"], rk, "read", b.get("title", title), b.get("author", authr), cover, fmt)
    # Propagate outward: mark finished in ABS (if it's in this user's library) and
    # unmonitor it in Chaptarr (don't grab what you've already read).
    abs_finished, chaptarr_unmonitored = False, False
    mt, ma = b.get("title", title), b.get("author", authr)
    tok = u.get("abs_token")
    if tok:
        try:
            item_id = absclient.find_item(mt, ma)
            if item_id:
                abs_finished = absclient.set_finished(tok, item_id, True)
        except Exception:
            pass
    try:
        chaptarr_unmonitored = chaptarr.mark_read(mt, ma)
    except Exception:
        pass
    return jsonify({"ok": True, "matched": mt,
                    "abs_finished": abs_finished, "chaptarr_unmonitored": chaptarr_unmonitored})


@bp.route("/api/rate", methods=["POST"])
@auth.login_required
def api_rate():
    """Rate a read book 1-5 stars to sharpen suggestions."""
    u = auth.current_user()
    body = request.get_json(force=True)
    asin = (body.get("asin") or "").strip()
    try:
        stars = int(body.get("stars", 0))
    except (TypeError, ValueError):
        stars = 0
    if not 1 <= stars <= 5:
        return jsonify({"error": "stars 1-5 required"}), 400
    # The client sends title/author for library books (which usually have no
    # real ASIN); only look them up on Audible for genuine ASINs. The author is
    # what the recommender boosts on, so we must capture it either way.
    title, author = (body.get("title") or "").strip(), (body.get("author") or "").strip()
    review = (body.get("review") or "").strip()[:1500]
    spoiler = 1 if body.get("spoiler") else 0
    fmt = body.get("format") or ("ebook" if asin.startswith(("gb:", "ol:")) else "audiobook")
    if (not title or not author) and asin and not asin.startswith(("t-", "gb:", "ol:")):
        meta = audible.by_asin(asin) or {}
        title = title or meta.get("title", "")
        author = author or meta.get("author", "")
    # Canonical rating key so writes and reads agree. When the client sends a
    # "t-…" slug it is ALREADY the exact key the read paths (History, onboarding,
    # shelves) rendered from — trust it verbatim. Recomputing the slug here from
    # title+author could produce a DIFFERENT key (e.g. an older row keyed without
    # the author, or a title that normalises differently), so the write landed
    # under a key History never reads back: it looked saved but the stars never
    # stuck. For real ASINs the key is the ASIN; ebook ids (gb:/ol:) fall through
    # to the title+author slug the ebook read paths use.
    if asin.startswith("t-"):
        key = asin
    else:
        real_asin = asin if (asin and not asin.startswith(("gb:", "ol:"))) else ""
        key = db.rating_key(real_asin, title, author)
    if not key or key == "t-":
        return jsonify({"error": "need an asin or title + author"}), 400
    with db.conn() as c:
        # a blank review on an update must not wipe an existing one
        c.execute("INSERT INTO ratings (user_id,asin,title,author,stars,review,spoiler,format,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime')) "
                  "ON CONFLICT(user_id,asin) DO UPDATE SET "
                  "stars=excluded.stars, title=excluded.title, author=excluded.author, "
                  "review=CASE WHEN excluded.review<>'' THEN excluded.review ELSE ratings.review END, "
                  "spoiler=excluded.spoiler, format=excluded.format, updated_at=datetime('now','localtime')",
                  (u["id"], key, title, author, stars, review, spoiler, fmt))
    return jsonify({"ok": True, "community": db.community_rating(key)})


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
    fmt = _req_format(body)
    hit = ebookmeta.search(f"{title} {authr}", num=1) if fmt == "ebook" else audible.search(f"{title} {authr}", num=1)
    b = hit[0] if hit else {"asin": "", "title": title, "author": authr}
    key = b.get("asin") or b.get("id") or db.rating_key("", b.get("title", title), b.get("author", authr))
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?)",
                  (u["id"], "asin", key, -2, f"dnf: {b.get('title', title)}", fmt))
        # propagate to moods: a DNF nudges down the moods that book carries
        for m in tagging.derive(b.get("categories") or []).get("mood", []):
            c.execute("INSERT INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT(user_id,kind,value) DO UPDATE SET weight=signals.weight-0.5",
                      (u["id"], "mood", m, -0.5, f"dnf mood: {b.get('title', title)}", fmt))
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


@bp.route("/api/requests/status")
@auth.login_required
def api_requests_status():
    """Live status of your requests that are still on their way, by Requests
    page entry id (the newest row of each book): {id: {label, cls, detail,
    formats: {fmt: {label, cls, detail}}}} — status.py, with Chaptarr's queue
    (cached 30 s). The page polls this to keep progress fresh."""
    u = auth.current_user()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 600", (u["id"],))]
    out = {}
    for e in group_requests(rows):
        if any(r["status"] in db.OPEN_REQUEST for r in e["current"]):
            st = e["st"]
            out[e["id"]] = {"state": st["state"], "label": st["label"], "cls": st["cls"], "detail": st["detail"],
                            "formats": {f: {k: v.get(k) for k in ("state", "label", "cls", "detail")}
                                        for f, v in st["formats"].items()}}
    return jsonify(out)


@bp.route("/api/webhook/chaptarr", methods=["POST"])
def api_webhook_chaptarr():
    """Chaptarr Connect webhook → real-time request updates. Token-protected via
    ?token= (set the same token in Settings). On an import/download event we run
    the library refresh, which flips a request to 'available' ONLY when the book
    actually appears in the library in that request's format, and notifies the
    real requester. Delegating here avoids the old fuzzy title-match that could
    flip the wrong user's or wrong-format request."""
    import secrets as _secrets
    token = db.get_meta("chaptarr_webhook_token", "")
    if not token or not _secrets.compare_digest(request.args.get("token") or "", token):
        return jsonify({"error": "bad token"}), 403
    body = request.get_json(silent=True) or {}
    event = (body.get("eventType") or "").lower()
    if event in ("download", "bookfileimported", "import"):
        try:
            from . import scheduler
            scheduler.refresh_library()
        except Exception as e:
            log.warning("webhook refresh failed: %s", e)
    return jsonify({"ok": True})


@bp.route("/api/requests/<int:rid>/approve", methods=["POST"])
@auth.admin_required
def api_request_approve(rid):
    with db.conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        if row["status"] != "pending_approval":
            return jsonify({"error": "not awaiting approval"}), 400
        # compare-and-swap: only the writer that flips it out of pending_approval
        # proceeds, so two concurrent admin approvals can't both grab it.
        claimed = c.execute("UPDATE requests SET status='approving' WHERE id=? AND status='pending_approval'",
                            (rid,)).rowcount
    if not claimed:
        return jsonify({"error": "already being processed"}), 409
    book = {"asin": row["asin"], "title": row["title"], "author": row["author"],
            "cover": row["cover"], "format": row["format"]}
    # a 'both' request must hand off BOTH formats — chaptarr maps 'both'→audiobook,
    # so the approval path silently dropped the ebook half (the direct path splits).
    fmts = ["audiobook", "ebook"] if book["format"] == "both" else [book["format"]]
    # bulk requests queued by api_series_add / api_author_add: the row's title is
    # the series name / "All books by X", not a book title to match
    if row["source"] == "series":
        grab = lambda f: chaptarr.add_and_search(book["title"], book["author"], fmt=f, series=book["title"])
    elif row["source"] == "author":
        grab = lambda f: chaptarr.add_and_search(book["author"], book["author"], fmt=f)
    else:
        grab = lambda f: chaptarr.add_and_search(book["title"], book["author"], book["asin"], fmt=f)
    rmap = {f: grab(f) for f in fmts}
    ok_any = any(r.get("ok") for r in rmap.values())
    oks = [f for f in fmts if rmap[f].get("ok")]
    res = (rmap[fmts[0]] if len(fmts) == 1
           else {"ok": ok_any, "ref": "", "detail": (f"Sent as {' + '.join(oks)}." if oks
                  else rmap[fmts[-1]].get("detail", "Couldn't add right now."))})
    # a transient Chaptarr outage (retry) must keep the row re-approvable, not
    # burn the admin's approval into a 'failed' state that can't be re-approved.
    retryable = not ok_any and any(r.get("retry") for r in rmap.values())
    status = "handed" if ok_any else ("pending_approval" if retryable else "failed")
    with db.conn() as c:
        c.execute("UPDATE requests SET status=?,detail=?,chaptarr_ref=?,updated_at=datetime('now','localtime') WHERE id=?",
                  (status, res.get("detail", ""), res.get("ref", ""), rid))
    requester = db.get_user(row["user_id"])
    if requester and requester.get("email") and db.get_pref(row["user_id"], "notify_approved", "1") == "1":
        try:
            notify.request_approved(book, requester["email"], _base_url())
        except Exception as e:
            log.debug("approved notify failed: %s", e)
    return jsonify({"ok": res["ok"], "status": status, "detail": res.get("detail", "")})


@bp.route("/api/requests/<int:rid>/deny", methods=["POST"])
@auth.admin_required
def api_request_deny(rid):
    reason = (request.get_json(silent=True) or {}).get("reason", "").strip()
    with db.conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        c.execute("UPDATE requests SET status='denied',detail=?,updated_at=datetime('now','localtime') WHERE id=?",
                  (reason or "Not approved", rid))
    requester = db.get_user(row["user_id"])
    if requester and requester.get("email") and db.get_pref(row["user_id"], "notify_denied", "1") == "1":
        try:
            notify.request_denied({"title": row["title"], "author": row.get("author", ""),
                                   "format": row.get("format", "audiobook")}, requester["email"], reason, _base_url())
        except Exception as e:
            log.debug("denied notify failed: %s", e)
    return jsonify({"ok": True})


@bp.route("/api/requests/check", methods=["POST"])
@auth.login_required
def api_requests_check():
    """Re-scan all connected libraries now and flip any request to 'available'
    if its book has appeared (e.g. the user added it outside Chaptarr). Returns
    how many flipped."""
    wait = _cooldown("requests_check", 20)
    if wait:
        return jsonify({"ok": False, "detail": f"Just checked — try again in {wait}s."}), 429
    from . import scheduler
    u = auth.current_user()
    before = after = 0
    with db.conn() as c:
        before = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=? AND status IN ('queued','handed','failed')",
                          (u["id"],)).fetchone()["n"]
    try:
        scheduler.refresh_library()
    except Exception as e:
        return jsonify({"ok": False, "detail": str(e)})
    with db.conn() as c:
        after = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=? AND status IN ('queued','handed','failed')",
                         (u["id"],)).fetchone()["n"]
    flipped = max(before - after, 0)
    return jsonify({"ok": True, "flipped": flipped,
                    "detail": f"{flipped} now available" if flipped else "No new matches in your libraries"})


@bp.route("/api/requests/retry-all", methods=["POST"])
@auth.login_required
def api_retry_all():
    """Re-send every failed request to Chaptarr in one go (the Wanted list)."""
    u = auth.current_user()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE user_id=? AND status='failed' ORDER BY id", (u["id"],))]
    ok = 0
    for row in rows:          # the retry updates each failed row in place (db.record_request)
        res = _hand_to_chaptarr(u["id"], row, row.get("source", "manual"))
        if res.get("ok"):
            ok += 1
    return jsonify({"ok": True, "retried": len(rows), "succeeded": ok,
                    "detail": f"Retried {len(rows)} — {ok} sent to Chaptarr" if rows else "Nothing to retry"})


@bp.route("/api/request/<int:rid>/retry", methods=["POST"])
@auth.login_required
def api_retry(rid):
    u = auth.current_user()
    with db.conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=? AND user_id=?", (rid, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        # only failed/denied requests are retryable — don't clobber an available
        # or pending-approval row.
        if row["status"] not in ("failed", "denied"):
            return jsonify({"error": "Only failed requests can be retried."}), 400
        was_denied = row["status"] == "denied"
    # a denial is an explicit admin decision — re-requesting it goes BACK through
    # approval (unless the requester is admin), never straight to a grab.
    if was_denied and u["role"] != "admin":
        return jsonify(_queue_for_approval(u, row, row.get("source", "manual")))
    return jsonify(_hand_to_chaptarr(u["id"], row, row["source"]))


@bp.route("/api/request/<int:rid>", methods=["DELETE"])
@auth.login_required
def api_request_delete(rid):
    u = auth.current_user()
    with db.conn() as c:
        c.execute("DELETE FROM requests WHERE id=? AND user_id=?", (rid, u["id"]))
    return jsonify({"ok": True})


@bp.route("/api/shelf", methods=["POST"])
@auth.login_required
def api_shelf():
    """Set (or clear) a book's personal shelf: want | reading | read | ''."""
    u = auth.current_user()
    b = request.get_json(force=True)
    rkey = (b.get("key") or b.get("asin") or "").strip()
    state = (b.get("state") or "").strip()
    if not rkey or state not in ("", "want", "reading", "read"):
        return jsonify({"error": "key + valid state required"}), 400
    db.shelf_set(u["id"], rkey, state, b.get("title", ""), b.get("author", ""),
                 b.get("cover", ""), b.get("format") or "audiobook")
    synced = None
    if state == "read":
        synced = _push_read_to_source(u, b.get("title", ""), b.get("author", ""),
                                      b.get("format") or "audiobook")
    return jsonify({"ok": True, "state": state, "counts": db.shelf_counts(u["id"]),
                    "synced": synced})


def _push_read_to_source(u, title, author, fmt):
    """Best-effort: tell the originating library app (Audiobookshelf, Komga,
    Kavita…) that this book is finished, so 'Read' in Stackarr propagates back.
    Returns the source's label if it synced, else None — marking locally never
    depends on the push succeeding."""
    with db.conn() as c:
        rows = _library_works(c, title, author)
    if not rows:
        return None
    # prefer the copy in the format being marked read
    rows.sort(key=lambda r: (r.get("format") or "audiobook") == fmt, reverse=True)
    row = rows[0]
    item_id, rfmt = row["item_id"], (row.get("format") or "audiobook")
    try:
        if rfmt == "audiobook":
            if absclient.set_finished(u.get("abs_token"), item_id):
                return "Audiobookshelf"
        else:
            from . import backends
            for be in backends.sources("ebook"):
                if getattr(be, "can_write_progress", False) and be.mark_read(u, item_id):
                    return be.label
    except Exception as e:
        log.debug("mark-read push failed: %s", e)
    return None


@bp.route("/api/feedback", methods=["POST"])
@auth.login_required
def api_feedback():
    """Lightweight 'more like this' / 'less like this' — nudges the engine via
    author (+ mood) signals without a full rating."""
    u = auth.current_user()
    b = request.get_json(force=True)
    author = (b.get("author") or "").split(",")[0].strip()
    direction = b.get("direction")          # "more" | "less"
    fmt = _req_format(b)
    if not author or direction not in ("more", "less"):
        return jsonify({"error": "author + direction required"}), 400
    w = 2 if direction == "more" else -2
    with db.conn() as c:
        c.execute("INSERT INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?) "
                  "ON CONFLICT(user_id,kind,value) DO UPDATE SET weight=signals.weight+excluded.weight",
                  (u["id"], "author", author, w, f"{direction} like: {b.get('title','')}", fmt))
    return jsonify({"ok": True})


@bp.route("/api/review/vote", methods=["POST"])
@auth.login_required
def api_review_vote():
    u = auth.current_user()
    try:
        rid = int(request.get_json(force=True).get("rating_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "rating_id required"}), 400
    return jsonify({"ok": True, "votes": db.review_vote(u["id"], rid)})


@bp.route("/api/get-other-format", methods=["POST"])
@auth.login_required
def api_get_other_format():
    """Own/seen a title in one format → request the other format (audiobook⇄ebook)."""
    u = auth.current_user()
    b = request.get_json(force=True)
    title, author = (b.get("title") or "").strip(), (b.get("author") or "").strip()
    other = "ebook" if b.get("format") == "audiobook" else "audiobook"
    if not title:
        return jsonify({"error": "title required"}), 400
    hit = (ebookmeta.search if other == "ebook" else audible.search)(f"{title} {author}", num=1)
    pick = hit[0] if hit else {"title": title, "author": author}
    pick["format"] = other
    pick["asin"] = pick.get("asin") or pick.get("id", "")
    return jsonify(_hand_to_chaptarr(u["id"], pick, "manual"))


@bp.route("/api/surprise")
@auth.login_required
def api_surprise():
    """One strong pick on demand — the highest-scored pending suggestion (of the
    active/echosen format), or a popular fallback. Optional ?mood= filter."""
    u = auth.current_user()
    fmt = request.args.get("format") or ""
    where = "AND format=?" if fmt in ("audiobook", "ebook") else ""
    args = [u["id"]] + ([fmt] if where else [])
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM suggestions WHERE user_id=? AND status='pending' {where} "
            "ORDER BY score DESC LIMIT 40", args)]
    mood = request.args.get("mood", "").lower().strip()
    if mood:
        rows = [r for r in rows if mood in [m.lower() for m in db.tags_for(
            db.rating_key(r["asin"], r["title"], r["author"])).get("mood", [])]] or rows
    if not rows:
        pop = discover.popular(8)
        rows = [{"asin": x["asin"], "title": x["title"], "author": x["author"],
                 "cover": x.get("cover", ""), "reason": "A popular pick to get you started",
                 "format": "audiobook"} for x in pop if x.get("asin")]
    if not rows:
        return jsonify({"ok": False})
    import hashlib
    # deterministic-but-varied: rotate by minute so repeated taps differ
    idx = int(hashlib.md5(request.args.get("n", "0").encode()).hexdigest(), 16) % len(rows)
    return jsonify({"ok": True, "book": rows[idx]})


SETTING_KEYS = {
    "email_theme", "language", "email_frequency", "suggest_interval_hours",
    "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from", "smtp_to",
    "abs_url", "abs_admin_token", "abs_public_url",
    "chaptarr_url", "chaptarr_api_key", "chaptarr_root_folder",
    "chaptarr_quality_profile_id", "chaptarr_metadata_profile_id",
    "goodreads_rss", "hardcover_token", "discord_webhook", "custom_webhook",
    "auto_add_level", "formats", "timezone",
    "kavita_url", "kavita_api_key",
    "calibreweb_url", "calibreweb_user", "calibreweb_pass",
    "komga_url", "komga_user", "komga_pass",
    "opds_url", "opds_user", "opds_pass",
    "chaptarr_webhook_token",
}
BOOL_KEYS = {"email_enabled", "discord_enabled",
             "notify_avail_enabled", "notify_newrelease_enabled",
             "abs_ebooks", "koreader_sync", "release_check"}
# per-user boolean preferences (written via /api/prefs, never the global endpoint)
USER_BOOL_PREFS = {"cross_format_taste", "hide_rated_history"}
# per-user email notification toggles (also via /api/prefs)
NOTIFY_PREF_KEYS = {"notify_approved", "notify_available", "notify_denied"}


@bp.route("/api/settings", methods=["POST"])
@auth.admin_required
def api_settings():
    """Install-wide configuration — admin only (server URLs, API keys, SMTP,
    format mode, …). Per-user preferences go through /api/prefs."""
    body = request.get_json(force=True)
    if "timezone" in body and not tz.valid(str(body["timezone"]).strip()):
        return jsonify({"ok": False, "error": "Unknown timezone — pick one from the list."}), 400
    for k, v in body.items():
        if k in BOOL_KEYS:
            db.set_meta(k, "1" if v else "0")
        elif k in SETTING_KEYS:
            db.set_meta(k, str(v).strip())
    return jsonify({"ok": True})


@bp.route("/api/prefs", methods=["POST"])
@auth.login_required
def api_prefs():
    """Per-user preferences any signed-in user may set for themselves."""
    u = auth.current_user()
    body = request.get_json(force=True)
    for k, v in body.items():
        if k in USER_BOOL_PREFS or k in NOTIFY_PREF_KEYS:
            db.set_pref(u["id"], k, "1" if v else "0")
    return jsonify({"ok": True})


@bp.route("/api/admin/approval-mode", methods=["POST"])
@auth.admin_required
def api_admin_approval_mode():
    """Toggle global manual-approval vs auto-approve for non-admin requests."""
    require = bool(request.get_json(force=True).get("require_approval"))
    db.set_meta("require_approval", "1" if require else "0")
    return jsonify({"ok": True, "require_approval": require})


@bp.route("/api/admin/import-users", methods=["POST"])
@auth.admin_required
def api_admin_import_users():
    """Import accounts from the connected sources (e.g. Audiobookshelf) as linked
    local accounts. Also flips the daily-sync setting when asked."""
    from . import scheduler
    body = request.get_json(silent=True) or {}
    if "sync" in body:
        db.set_meta("user_sync", "1" if body["sync"] else "0")
        return jsonify({"ok": True, "sync": bool(body["sync"])})
    res = scheduler.import_users()
    return jsonify({"ok": True, **res,
                    "detail": f"{res['created']} new account(s) imported from {res['seen']} source user(s)."})


@bp.route("/api/admin/user/<int:uid>", methods=["POST"])
@auth.admin_required
def api_admin_user(uid):
    """Admin: set a user's role or trusted flag."""
    me = auth.current_user()
    body = request.get_json(force=True)
    target = db.get_user(uid)
    if not target:
        return jsonify({"error": "no such user"}), 404
    if "role" in body:
        # don't let an admin demote themselves into a lockout
        if uid == me["id"] and body["role"] != "admin":
            return jsonify({"error": "You can't remove your own admin role."}), 400
        db.set_role(uid, "admin" if body["role"] == "admin" else "user")
    if "trusted" in body:
        db.set_pref(uid, "trusted", "1" if body["trusted"] else "0")
    return jsonify({"ok": True})


@bp.route("/api/test/<service>", methods=["POST"])
@auth.admin_required
def api_test(service):
    body = request.get_json(silent=True) or {}
    # Apply the values typed in the form ONLY for the duration of the test, then
    # restore them — a failed or abandoned test must not silently overwrite the
    # working saved credentials/URLs (the user still clicks Save to persist).
    overrides = {k: str(v).strip() for k, v in body.items() if k in SETTING_KEYS}
    prev = {k: db.get_meta(k, "") for k in overrides}
    for k, v in overrides.items():
        db.set_meta(k, v)
    try:
        try:
            if service == "chaptarr":
                import requests as rq
                r = rq.get(f"{chaptarr.url()}/api/v1/system/status",
                           headers={"X-Api-Key": chaptarr.api_key()}, timeout=15)
                if not r.ok:
                    return jsonify({"ok": False, "detail": f"HTTP {r.status_code}"})
                warns = chaptarr.health()
                msg = "Connected"
                if warns:
                    msg += " — heads up: " + "; ".join(w["message"][:80] for w in warns[:2])
                return jsonify({"ok": True, "detail": msg, "warnings": warns})
            # every library source backend (abs / kavita / calibreweb) self-tests
            from . import backends
            b = backends.by_id(service)
            if b is not None:
                return jsonify(b.test())
        except Exception as e:
            return jsonify({"ok": False, "detail": str(e)})
        return jsonify({"ok": False, "detail": "unknown service"}), 404
    finally:
        for k, v in prev.items():
            db.set_meta(k, v)


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
    wait = _cooldown("run_now", 30)
    if wait:
        return jsonify({"ok": False, "detail": f"A refresh just ran — try again in {wait}s.", "started": False}), 429
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
    out, last_lvl = [], 20
    with open(config.LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"^[\d\-]+ [\d:]+ \[(\w+)\]", line)   # the leading "date time [LEVEL]"
            if m:
                last_lvl = LOG_LEVELS.get(m.group(1), 20)
            # unbracketed continuation/traceback lines inherit the prior level so a
            # filtered view keeps the traceback body, not just the bare message.
            if last_lvl >= thresh:
                out.append(line.rstrip())
    return out[-limit:]


@bp.route("/api/logs")
@auth.admin_required
def api_logs():
    level = request.args.get("level", "INFO").upper()
    try:
        limit = int(request.args.get("limit", "300"))
    except (ValueError, TypeError):
        limit = 300
    return jsonify({"lines": _read_logs(level, limit)})


@bp.route("/api/logs/download")
@auth.admin_required
def api_logs_download():
    from flask import Response
    level = request.args.get("level", "DEBUG").upper()
    body = "\n".join(_read_logs(level, 200000)) or "(no log entries)"
    return Response(body, mimetype="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=stackarr-{level.lower()}.log"})


_COVER_CACHE = {"Cache-Control": "private, max-age=604800"}
COVER_CACHE_DAYS = 3
_COVER_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_COVER_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
                "image/svg+xml": "svg"}


def _cover_dir() -> str:
    import os
    d = os.path.join(config.DATA_DIR, "covers")
    os.makedirs(d, exist_ok=True)
    return d


def _cover_cache_get(item_id):
    """(bytes, content type) of a cached ABS cover younger than
    COVER_CACHE_DAYS, else None. Only safe ids reach the filesystem."""
    import os
    import time
    if not _COVER_ID.fullmatch(item_id or ""):
        return None
    try:
        d = _cover_dir()
        for ctype, ext in _COVER_TYPES.items():
            p = os.path.join(d, f"{item_id}.{ext}")
            if os.path.exists(p) and time.time() - os.path.getmtime(p) < COVER_CACHE_DAYS * 86400:
                with open(p, "rb") as f:
                    return f.read(), ctype
    except OSError:
        pass
    return None


def _cover_cache_put(item_id, data: bytes, ctype: str):
    import os
    ext = _COVER_TYPES.get((ctype or "").split(";")[0].strip())
    if not ext or not _COVER_ID.fullmatch(item_id or "") or len(data) > 5_000_000:
        return
    try:
        p = os.path.join(_cover_dir(), f"{item_id}.{ext}")
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
    except OSError as e:
        log.debug("cover cache write failed: %s", e)


@bp.route("/libcover/<path:ref>")
@auth.login_required
def libcover(ref):
    """Serve the cover of a book we OWN, from the service that holds it.

    Refs are the ones stored on the library row: "cw:161" (Calibre-Web),
    "kv:127" (Kavita). Proxied rather than linked because the browser can't
    reach host.docker.internal, and it keeps the backend credentials server side.

    This exists so an owned book's art is a local fetch. It used to be resolved
    by searching Google Books for the title, which made every poster tile depend
    on a third party's rate limit, and put that call on the request path."""
    from flask import Response
    import requests as rq
    kind, _, ident = (ref or "").partition(":")
    try:
        if kind == "cw" and ident.isdigit():
            from .backends import calibreweb as cw
            r = rq.get(f"{cw._url()}/opds/cover/{ident}", auth=cw._auth(), timeout=10)
            if r.ok and r.content:
                return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                                headers=_COVER_CACHE)
        elif kind == "kv" and ident.isdigit():
            from . import backends
            be = backends.by_id("kavita")
            if be:
                r = be._get(f"/api/image/series-cover?seriesId={ident}")
                if r.ok and r.content:
                    return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                                    headers=_COVER_CACHE)
    except Exception as e:
        log.debug("libcover %s failed: %s", ref, e)
    # A cover we can't fetch is a missing picture, never a slow page: fall through
    # to the placeholder immediately rather than trying to source art elsewhere.
    return redirect(url_for("static", filename="cover-placeholder.svg"))


@bp.route("/cover/<item_id>")
@auth.login_required
def cover(item_id):
    """Proxy an Audiobookshelf cover through Stackarr so the browser (which
    can't reach host.docker.internal) gets it same-origin, token hidden.

    Served from a small disk cache (COVER_CACHE_DAYS) after the first fetch.
    A Home render asks for ~30 of these at once, and each used to hold a
    waitress thread for an ABS round trip — the "Task queue depth" warnings
    in the field log were these bursts."""
    from flask import Response
    import requests as rq
    hit = _cover_cache_get(item_id)
    if hit:
        return Response(hit[0], mimetype=hit[1], headers=_COVER_CACHE)
    try:
        r = rq.get(f"{absclient.abs_url()}/api/items/{item_id}/cover",
                   headers={"Authorization": f"Bearer {absclient.admin_token()}"}, timeout=8)
        if r.ok and r.content:
            ctype = r.headers.get("Content-Type", "image/jpeg")
            _cover_cache_put(item_id, r.content, ctype)
            return Response(r.content, mimetype=ctype, headers=_COVER_CACHE)
    except Exception:
        pass
    # ABS has no art for this item. This used to ask ABS for the item and then
    # search Audible for it, inline, per <img> — two network calls on a worker
    # thread for every coverless card (the /coverart rule: an image endpoint
    # never calls a third party). Hand it to /coverart instead, which answers
    # from the cover cache or queues the lookup for the background worker.
    with db.conn() as c:
        m = c.execute("SELECT title, author, asin FROM library WHERE item_id=?", (item_id,)).fetchone()
    if m and m["title"]:
        # nolib: this IS the library copy, so /coverart mustn't send us back here
        return redirect(url_for("main.coverart", title=m["title"], author=m["author"] or "",
                                asin=m["asin"] or "", fmt="audiobook", nolib=1))
    return redirect(url_for("static", filename="cover-placeholder.svg"))


@bp.route("/coverart")
@auth.login_required
def coverart():
    """Cover art for a book with no cover stored on the row it was rendered from.

    This endpoint is the target of an <img> tag, so it MUST answer immediately.
    It never calls an external service. It answers from, in order: the local
    library (a book we own, served by whichever of our own services holds it),
    then the resolved-cover cache, then the placeholder.

    A book we do NOT own and have never resolved is handed to the background
    worker and gets the placeholder for now; its art appears on a later render.

    ⛔ It used to do the lookup inline — Audible or Google Books, on the request
    thread, per image. That is what made this page unusable on 2026-09-21: the
    home page emits ~59 of these, a miss was deliberately never cached so every
    render retried all of them, and Google Books' keyless daily quota was
    exhausted (429) so each one burned seconds before failing. With 8 worker
    threads the whole app starved, so the PAGE crawled, not just the covers.
    Whatever is wrong with a cover, it must never cost the page a thread."""
    asin = (request.args.get("asin") or "").strip()
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    fmt = (request.args.get("fmt") or "").strip()
    placeholder = url_for("static", filename="cover-placeholder.svg")
    if not title and not asin.startswith("B0"):
        return redirect(placeholder)
    # 1) Do we own it? Then the art is on one of our own services.
    try:
        ref = "" if request.args.get("nolib") else db.library_cover(title, author, asin)
        if ref:
            return redirect(url_for("main.libcover", ref=ref) if ":" in ref
                            else url_for("main.cover", item_id=ref))
    except Exception as e:
        log.debug("library cover lookup failed for %s: %s", title, e)
    # 2) Resolved earlier by the background worker?
    cache_key = "cart:" + db.rating_key(asin if asin.startswith("B0") else "", title, author)
    cached = db.get_meta(cache_key, "")
    if cached:
        return redirect(placeholder if cached == "none" else cached)
    # 3) Unknown: queue it and show the placeholder NOW.
    try:
        from . import scheduler          # local import: scheduler imports routes' siblings
        scheduler.queue_cover(asin, title, author, fmt)
    except Exception as e:
        log.debug("could not queue cover for %s: %s", title, e)
    return redirect(placeholder)


# ---- KOReader progress sync (kosync protocol) ----------------------------
# Experimental: lets KOReader e-readers sync reading progress to Stackarr.
# kosync only carries a document *hash* (no title), so it can't feed
# recommendations — it's a sync relay. Enable with KOREADER_SYNC.
def _kosync_on() -> bool:
    return db.get_meta("koreader_sync", "1" if config.KOREADER_SYNC else "0") == "1"


def _kosync_key(user: str, doc: str) -> str:
    """Storage key for a user's progress on a document. Both segments are hashed
    to fixed-length hex so the user/doc boundary is unambiguous — the old
    `kosync_{user}_{doc}` form could be made to collide with another kosync
    account's row via a crafted `document` value. (Off-by-default feature; any
    pre-existing progress simply re-syncs from the device on next open.)"""
    import hashlib
    h = lambda s: hashlib.sha1((s or "").encode("utf-8", "replace")).hexdigest()
    return f"kosync_{h(user)}_{h(doc)}"


def _kosync_check():
    """The kosync username when the x-auth-user/x-auth-key headers match a
    registered user, else None. KOReader sends the key as an md5 of the password,
    so we store and compare that key verbatim (constant-time)."""
    import secrets
    user = request.headers.get("x-auth-user", "")
    key = request.headers.get("x-auth-key", "")
    if not user:
        return None
    stored = db.get_meta(f"kosync_user_{user}", "")
    return user if (stored and secrets.compare_digest(stored, key)) else None


@bp.route("/users/create", methods=["POST"])
def kosync_create():
    if not _kosync_on():
        return jsonify({"message": "disabled"}), 403
    b = request.get_json(silent=True) or {}
    user = (b.get("username") or "").strip()
    pw = b.get("password") or ""
    # require a non-empty password — an empty key would be both unusable to auth
    # with AND silently overwritable later (the "already registered" guard treats
    # an empty stored value as "not registered").
    if not user or not pw:
        return jsonify({"message": "Invalid request"}), 400
    if db.get_meta(f"kosync_user_{user}", ""):
        return jsonify({"message": "Username is already registered."}), 402
    db.set_meta(f"kosync_user_{user}", pw)
    return jsonify({"username": user}), 201


@bp.route("/users/auth")
def kosync_auth():
    if not _kosync_on():
        return jsonify({"message": "disabled"}), 403
    if not _kosync_check():
        return jsonify({"message": "Unauthorized"}), 401
    return jsonify({"authorized": "OK"})


@bp.route("/syncs/progress", methods=["PUT"])
def kosync_put():
    if not _kosync_on():
        return jsonify({"message": "disabled"}), 403
    user = _kosync_check()
    if not user:
        return jsonify({"message": "Unauthorized"}), 401
    b = request.get_json(silent=True) or {}
    doc = b.get("document", "")
    if not doc:
        return jsonify({"message": "Invalid request"}), 400
    import json as _json
    db.set_meta(_kosync_key(user, doc), _json.dumps({
        "progress": b.get("progress", ""), "percentage": b.get("percentage", 0),
        "device": b.get("device", ""), "device_id": b.get("device_id", "")}))
    return jsonify({"document": doc, "timestamp": 0})


@bp.route("/syncs/progress/<doc>")
def kosync_get(doc):
    if not _kosync_on():
        return jsonify({"message": "disabled"}), 403
    user = _kosync_check()
    if not user:
        return jsonify({"message": "Unauthorized"}), 401
    import json as _json
    raw = db.get_meta(_kosync_key(user, doc), "")
    if not raw:
        return jsonify({})
    d = _json.loads(raw)
    return jsonify({"document": doc, "progress": d.get("progress", ""),
                    "percentage": d.get("percentage", 0), "device": d.get("device", ""),
                    "device_id": d.get("device_id", ""), "timestamp": 0})


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
        f"const C='stackarr-{config.VERSION}';\n"      # tie cache to version so app.js/css auto-bust each release
        # Only static assets are precached/cached. Authenticated HTML pages (home,
        # settings, requests — which contain per-user data) are NEVER cached, so a
        # shared device can't serve one user's page to the next.
        f"const SHELL=['{base}/static/style.css','{base}/static/app.js','{base}/static/icon.svg'];\n"
        "self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()))});\n"
        "self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});\n"
        "self.addEventListener('fetch',e=>{const u=new URL(e.request.url);"
        "if(e.request.method!=='GET'||!u.pathname.includes('/static/')){return}"   # static assets only
        # NETWORK-FIRST: always try the live asset (so a deploy is instantly picked
        # up and a stale SW can never serve a broken old app.js), falling back to
        # cache only when offline. Cache-first previously made the app look "down"
        # after a deploy until the SW happened to update.
        "e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();caches.open(C).then(c=>c.put(e.request,cp));return r}).catch(()=>caches.match(e.request)))});\n"
    )
    from flask import Response
    return Response(js, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": f"{base}/"})
