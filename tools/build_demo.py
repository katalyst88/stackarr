#!/usr/bin/env python3
"""Render Stackarr's templates to a static site for GitHub Pages.

No backend needed at runtime: every page is pre-rendered with sample data and
all internal links are rewritten to flat relative .html files. Covers load from
Amazon's public CDN, so the hosted demo depends on nothing local.

Usage:  python tools/build_demo.py   (writes ./docs)
"""
import os, re, json, shutil
from markupsafe import escape as _mesc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("STACKARR_DATA", os.path.join(ROOT, "audit", "_demo_data"))
os.makedirs(os.environ["STACKARR_DATA"], exist_ok=True)
os.environ.setdefault("STACKARR_NO_SCHED", "1")
os.environ.setdefault("STACKARR_FORMATS", "both")   # demo shows the full multi-format UI
# dummy backend creds so create_app() boots with no real services
os.environ.setdefault("ABS_URL", "http://demo.invalid")
os.environ.setdefault("ABS_ADMIN_TOKEN", "demo")
os.environ.setdefault("CHAPTARR_URL", "http://demo.invalid")
os.environ.setdefault("CHAPTARR_API_KEY", "demo")

import sys; sys.path.insert(0, ROOT)
from stackarr import create_app, config            # noqa: E402
from stackarr import tagging as _tagging           # noqa: E402
from flask import render_template, render_template_string, url_for  # noqa: E402

OUT = os.path.join(ROOT, "docs")
BOOKS = json.load(open(os.path.join(ROOT, "audit", "demo_books.json"), encoding="utf-8"))
for i, b in enumerate(BOOKS):
    b["_id"] = i + 1
app = create_app()
# stamp a format on every sample book so the 'both'-mode UI (badges/filters)
# has real content — alternate so audiobooks + ebooks both appear.
for b in BOOKS:
    b["format"] = "ebook" if (b["_id"] % 3 == 0) else "audiobook"

GH = "https://github.com/katalyst88/stackarr"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "x"


# ---- build sample data ------------------------------------------------------
LANE_TITLES = {
    "series": "Continue your series", "author": "More from authors you love",
    "enjoyed": "Listeners also enjoyed", "narrator": "Narrators you love",
    "genre": "More in your favourite genres", "discover_author": "New authors to discover",
    "hidden": "Hidden gems", "awards": "Award winners", "short": "Short listens",
    "epic": "Epic listens", "popular": "Popular right now", "upcoming": "New & upcoming",
}
LANE_ORDER = list(LANE_TITLES)


def first_author(b):
    return (b["author"] or "").split(",")[0].strip()


def reason_for(lane, b):
    g = (b["genres"] or ["Fiction"])[0]
    rt = b.get("runtime_hours")
    return {
        "series": f"Next in {b['series']}" if b.get("series") else "Next in the series",
        "author": f"More from {first_author(b)}",
        "enjoyed": "Listeners also enjoyed this",
        "narrator": f"Narrated by {b['narrator']}" if b.get("narrator") else "A narrator you love",
        "genre": f"More {g}",
        "discover_author": f"Discover {first_author(b)}",
        "hidden": "Hidden gem — loved, lesser known",
        "awards": "Award winner",
        "short": f"Short listen · {rt}h" if rt else "A shorter listen",
        "epic": f"Epic listen · {rt}h" if rt else "A long, deep listen",
        "popular": "Popular right now",
        "upcoming": "Coming soon",
    }[lane]


def card(b, lane, available=False, extra=None):
    return {"id": b["_id"], "asin": b["asin"], "cover": b["cover"], "title": b["title"],
            "author": b["author"], "reason": reason_for(lane, b), "format": b.get("format", "audiobook"),
            "available": available, "extra": extra}


lanes = {}
chunk = max(6, len(BOOKS) // len(LANE_ORDER))
for idx, lane in enumerate(LANE_ORDER):
    seg = BOOKS[idx * chunk:(idx + 1) * chunk] or BOOKS[:chunk]
    cards = []
    for j, b in enumerate(seg):
        avail = (lane not in ("upcoming",)) and (b["_id"] % 11 == 0)
        extra = "2026" if lane == "upcoming" else None
        cards.append(card(b, lane, available=avail, extra=extra))
    lanes[lane] = cards

# genres + authors
gfreq = {}
for b in BOOKS:
    for g in (b["genres"] or []):
        gfreq[g] = gfreq.get(g, 0) + 1
all_genres = sorted(gfreq, key=lambda g: -gfreq[g])
home_genres = all_genres[:12]

all_authors = []
for b in BOOKS:
    for a in (b["author"] or "").split(","):
        a = a.strip()
        if a and a not in all_authors:
            all_authors.append(a)
rec_authors = []
for b in sorted(BOOKS, key=lambda x: (x.get("rating") or 0) * (x.get("num_ratings") or 0), reverse=True):
    a = first_author(b)
    if a and a not in rec_authors:
        rec_authors.append(a)
rec_authors = rec_authors[:14]

recently_added = [{"item_id": f"demo{b['_id']}", "asin": b["asin"], "title": b["title"],
                   "author": b["author"], "cover": b["cover"], "format": b.get("format", "audiobook")}
                  for b in BOOKS[3:12]]
cover_map = {r["item_id"]: r["cover"] for r in recently_added}

_st = ["available", "queued", "handed", "failed", "available", "queued"]
recent_requests = [{"asin": b["asin"], "cover": b["cover"], "title": b["title"],
                    "author": b["author"], "status": _st[k % len(_st)], "format": b.get("format", "audiobook")}
                   for k, b in enumerate(BOOKS[:6])]


def book_detail(b, state="new"):
    d = dict(b)
    d["state"] = state
    d["req_detail"] = ("Chaptarr couldn't find a copy from your indexers yet — it'll keep trying."
                       if state == "failed" else None)
    d.pop("_id", None)
    return d


def author_books(name):
    out = []
    for b in BOOKS:
        if name.lower() in (b["author"] or "").lower():
            out.append({"asin": b["asin"], "cover": b["cover"], "title": b["title"],
                        "author": b["author"], "series": b.get("series"),
                        "state": "available" if b["_id"] % 11 == 0 else "new"})
    return out


def genre_books(g):
    out = []
    for b in BOOKS:
        if g in (b["genres"] or []):
            out.append({"asin": b["asin"], "cover": b["cover"], "title": b["title"],
                        "author": b["author"], "series": b.get("series"),
                        "state": "available" if b["_id"] % 11 == 0 else "new"})
    return out


# insights
afreq = {}
for b in BOOKS:
    a = first_author(b)
    afreq[a] = afreq.get(a, 0) + 1
top_authors = sorted(afreq.items(), key=lambda kv: -kv[1])[:8]
# a sample activity heatmap (53 weeks x 7 days) with a believable pattern
import datetime as _dt
_today = _dt.date(2026, 6, 14)
_start = _today - _dt.timedelta(days=_today.weekday() + 1 + 52 * 7)
_weeks, _cur = [], _start
for _w in range(53):
    _week = []
    for _d in range(7):
        _n = (_cur.toordinal() * 7 + _cur.day) % 13
        _cnt = 0 if _n < 7 else (_n - 6)
        _week.append({"date": _cur.isoformat(), "count": _cnt, "future": _cur > _today})
        _cur += _dt.timedelta(days=1)
    _weeks.append(_week)
insights_ctx = dict(
    hours=412, finished=87, in_progress=4, req_avail=23, top_authors=top_authors,
    by_format={"audiobook": 61, "ebook": 26}, year=2026, goal=40, read_year=26,
    heat={"weeks": _weeks, "total": 87, "max": 6},
    top_moods=[("epic", 22), ("dark", 18), ("adventurous", 14), ("fast-paced", 11), ("reflective", 7)],
    facts=[("🎧", "412 hours", "about 17 full days of audio"),
           ("📚", "87 books", "finished cover to cover"),
           ("🏆", top_authors[0][0], "your most-read author"),
           ("🎭", "epic", "your most-read mood")],
)


# ---- render -----------------------------------------------------------------
def base_ctx():
    return dict(app_name=config.APP_NAME, accent=config.ACCENT, version=config.VERSION,
                stage="demo", url_base="", user={"username": "demo"})


def render(template, path, **ctx):
    with app.test_request_context(path):
        return render_template(template, **base_ctx(), **ctx)


DEMO_INFO = """{% extends "base.html" %}{% block content %}
<div class="page-head"><h2 class="page-title">Live demo</h2></div>
<div class="panel" style="max-width:680px">
<p style="font-size:1.05rem;line-height:1.6">This is a <strong>static demo</strong> of Stackarr, served
from GitHub Pages with sample data. Sign-in, settings, search and the add/ignore
buttons are disabled here.</p>
<p style="font-size:1.05rem;line-height:1.6">Run the real thing — it connects to your own
Audiobookshelf and Chaptarr — in two minutes with Docker.</p>
<p style="margin-top:18px"><a class="btn" href="{{ gh }}">Get Stackarr on GitHub ›</a></p>
</div>{% endblock %}{% block scripts %}<script>Stackarr.boot&&Stackarr.boot();</script>{% endblock %}"""


def render_info(path):
    with app.test_request_context(path):
        return render_template_string(DEMO_INFO, gh=GH, **base_ctx())


# build replacement map
with app.test_request_context("/"):
    repl = {url_for("main.home_page"): "index.html",
            url_for("main.suggestions_page"): "suggestions.html",
            url_for("main.insights_page"): "insights.html",
            url_for("main.history_page"): "history.html",
            url_for("main.series_page"): "series.html",
            url_for("main.shelves_page"): "shelves.html",
            url_for("main.upcoming_page"): "upcoming.html",
            url_for("main.taste_page"): "taste.html",
            url_for("main.requests_page"): "requests.html",
            url_for("main.settings_page"): "demo-info.html",
            url_for("main.discover_page"): "demo-info.html",
            url_for("main.logout"): "demo-info.html"}
    try:
        repl[url_for("main.manifest")] = "manifest.webmanifest"
    except Exception:
        pass
    for b in BOOKS:
        repl[url_for("main.book_page", asin=b["asin"])] = f"book-{b['asin']}.html"
    for g in all_genres:
        repl[url_for("main.browse_page", genre=g)] = f"genre-{slug(g)}.html"
    for m in _tagging.ALL_MOODS:
        repl[url_for("main.browse_page", mood=m)] = f"mood-{slug(m)}.html"
    for a in all_authors:
        repl[url_for("main.browse_page", author=a)] = f"author-{slug(a)}.html"
        repl[url_for("main.author_page", name=a)] = f"author-{slug(a)}.html"
    _narrators = []
    for b in BOOKS:
        for nm in (b.get("narrator") or "").split(","):
            nm = nm.strip()
            if nm and nm not in _narrators:
                _narrators.append(nm)
                repl[url_for("main.narrator_page", name=nm)] = f"narrator-{slug(nm)}.html"
    for lane in lanes:
        repl[url_for("main.lane_grid", lane=lane)] = f"lane-{lane}.html"
    for it, cov in cover_map.items():
        repl[url_for("main.cover", item_id=it)] = cov

keys = sorted(repl, key=len, reverse=True)


def rewrite(htmlstr):
    for k in keys:
        tgt = repl[k]
        for variant in (k, str(_mesc(k))):
            if variant in htmlstr:
                htmlstr = htmlstr.replace(f'"{variant}"', f'"{tgt}"').replace(f"'{variant}'", f"'{tgt}'")
    htmlstr = htmlstr.replace('"/static/', '"static/').replace("'/static/", "'static/")
    htmlstr = htmlstr.replace('src="static/app.js"></script>',
                              'src="static/app.js"></script>\n<script src="static/demo.js"></script>')
    return htmlstr


# ---- write ------------------------------------------------------------------
if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)
shutil.copytree(os.path.join(ROOT, "stackarr", "static"), os.path.join(OUT, "static"))
open(os.path.join(OUT, ".nojekyll"), "w").close()

# the demo has no service worker; drop its registration so the console stays clean
_ajs = os.path.join(OUT, "static", "app.js")
_src = open(_ajs, encoding="utf-8").read()
_src = re.sub(r'\nif \("serviceWorker" in navigator\).*register.*\n?', "\n", _src)
open(_ajs, "w", encoding="utf-8").write(_src)


def write(name, htmlstr):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(rewrite(htmlstr))


pages = 0
# Home dashboard (the landing)
_reading = [{"rkey": b["asin"], "title": b["title"], "author": b["author"], "cover": b["cover"],
             "format": b.get("format", "audiobook")} for b in BOOKS[:5]]
_fresh = [card(b, "enjoyed") for b in BOOKS[5:13]]
_home_up = [{"asin": b["asin"], "title": b["title"], "author": b["author"], "cover": b["cover"],
             "format": b.get("format", "audiobook")} for b in BOOKS[13:19]]
write("index.html", render("home.html", "/home", reading=_reading, fresh=_fresh, upcoming=_home_up,
      goal=40, read_year=26, year=2026, want_n=8, avail_n=23)); pages += 1
write("suggestions.html", render("suggestions.html", "/suggestions", lanes=lanes,
      lane_titles=LANE_TITLES, genres=home_genres, rec_authors=rec_authors,
      recently_added=recently_added, recent_requests=recent_requests, abs_base="#",
      show_vibes=True, all_moods=_tagging.ALL_MOODS)); pages += 1
write("insights.html", render("insights.html", "/insights", **insights_ctx)); pages += 1

# My shelves (with reading goal ring) + Upcoming
def _shelfitem(b, state):
    return {"rkey": b["asin"], "title": b["title"], "author": b["author"],
            "cover": b["cover"], "format": b.get("format", "audiobook"), "state": state}
_shelves = {"reading": [_shelfitem(b, "reading") for b in BOOKS[:3]],
            "want": [_shelfitem(b, "want") for b in BOOKS[3:11]],
            "read": [_shelfitem(b, "read") for b in BOOKS[11:23]]}
write("shelves.html", render("shelves.html", "/shelves", shelves=_shelves,
      counts={"reading": 3, "want": 8, "read": 26}, goal=40, read_this_year=26, year=2026)); pages += 1
_upcoming = [card(b, "upcoming", extra="2026-1%d" % (k + 1)) for k, b in enumerate(BOOKS[:8])]
write("upcoming.html", render("upcoming.html", "/upcoming", rows=_upcoming, today="2026-06-14")); pages += 1
_read = [{"asin": b["asin"], "rkey": b["asin"], "title": b["title"], "author": b["author"],
          "cover": b["cover"], "stars": [5, 4, 5, 3, 4, 5, 4][k % 7]} for k, b in enumerate(BOOKS[:24])]
write("history.html", render("history.html", "/history", books=_read,
      rated_n=sum(1 for b in _read if b["stars"]))); pages += 1
_req_rows = [{"id": k + 1, "asin": b["asin"], "cover": b["cover"], "title": b["title"],
              "author": b["author"], "format": b.get("format", "audiobook"), "username": None,
              "status": ["available", "handed", "queued", "failed", "available", "handed"][k % 6],
              "detail": ("No release found yet — Chaptarr will keep trying." if k % 6 == 3 else "")}
             for k, b in enumerate(BOOKS[:9])]
write("requests.html", render("requests.html", "/requests", requests=_req_rows,
      admin=False, wanted=False)); pages += 1

# Up Next (series tracker) — group sample books by series
_series_groups = {}
for b in BOOKS:
    if b.get("series"):
        _series_groups.setdefault(b["series"], []).append(b)
_series_cards = []
for _i, (name, bks) in enumerate(sorted(_series_groups.items(), key=lambda kv: -len(kv[1]))):
    if len(bks) < 2:
        continue
    books_c = [{"title": x["title"], "author": x["author"], "series": name,
                "series_seq": j + 1, "asin": x["asin"]} for j, x in enumerate(bks)]
    nxt = None
    if _i % 2 == 0 and len(bks) >= 2:        # show a "next up" on some, caught-up on others
        nb = bks[-1]
        nxt = {"id": nb["_id"], "title": nb["title"], "author": nb["author"],
               "asin": nb["asin"], "cover": nb["cover"], "reason": ""}
    _series_cards.append({"name": name, "owned": len(bks), "highest": len(bks),
                          "read_to": max(1, len(bks) - 2), "read_count": max(1, len(bks) - 2),
                          "missing_audio": (_i % 3 == 0), "missing_ebook": (_i % 4 == 0),
                          "author": first_author(bks[0]), "format": bks[0].get("format", "audiobook"),
                          "books": books_c, "next": nxt, "next_status": None})
write("series.html", render("series.html", "/series", series=_series_cards,
      have_next=sum(1 for c in _series_cards if c["next"]))); pages += 1

# Taste — sample ratings + a few signals
_taste_sig = lambda i, t: {"id": i, "label": t}
write("taste.html", render("taste.html", "/taste",
      ratings=[{"asin": b["asin"], "title": b["title"], "author": b["author"],
                "stars": [5, 4, 5, 3, 4][k % 5]} for k, b in enumerate(BOOKS[:10])],
      dnf=[_taste_sig(1, "A Slow Start"), _taste_sig(2, "Not For Me")],
      passed=[_taste_sig(3, "Wrong Vibe")],
      readseed=[_taste_sig(4, b["title"]) for b in BOOKS[10:13]],
      removed=[_taste_sig(5, "An Old Favourite")])); pages += 1
write("demo-info.html", render_info("/settings")); pages += 1
for lane, cards in lanes.items():
    write(f"lane-{lane}.html", render("lane.html", f"/lane/{lane}",
          title=LANE_TITLES[lane], lane=lane, rows=cards)); pages += 1
_demo_reviews = [
    {"id": 1, "username": "alex", "stars": 5, "review": "Could not put it down — the worldbuilding is unreal.", "spoiler": 0, "votes": 12, "created_at": "2026-05-30"},
    {"id": 2, "username": "sam", "stars": 4, "review": "Slow start but the payoff is worth it.", "spoiler": 0, "votes": 5, "created_at": "2026-05-22"},
    {"id": 3, "username": "jo", "stars": 5, "review": "That twist near the end!", "spoiler": 1, "votes": 8, "created_at": "2026-05-18"},
]
_demo_tags = {"genre": ["Epic Fantasy", "Adventure"], "mood": ["epic", "dark", "adventurous"],
              "pace": ["fast-paced"], "warning": ["Violence", "Death/grief"]}
for k, b in enumerate(BOOKS):
    st = ["new", "new", "new", "available", "new", "queued", "new", "failed"][k % 8]
    write(f"book-{b['asin']}.html", render("book.html", f"/book/{b['asin']}",
          b=book_detail(b, st), rate_key=b["asin"], tags=_demo_tags, shelf=("read" if k % 5 == 0 else ""),
          community={"avg": round(4.1 + (k % 9) * 0.1, 1), "count": 12 + (k % 40)},
          reviews=(_demo_reviews if k % 2 == 0 else []),
          my_stars=([0, 5, 4, 0, 3][k % 5]), my_review="")); pages += 1
for g in all_genres:
    write(f"genre-{slug(g)}.html", render("browse.html", "/browse", kind="genre",
          title=g, author=None, books=genre_books(g))); pages += 1
for m in _tagging.ALL_MOODS:
    write(f"mood-{slug(m)}.html", render("browse.html", "/browse", kind="mood",
          title=m, author=None, books=genre_books(all_genres[hash(m) % len(all_genres)]))); pages += 1
for a in all_authors:
    write(f"author-{slug(a)}.html", render("author.html", f"/author/{a}",
          author=a, books=author_books(a), following=False)); pages += 1
for nm in _narrators:
    _nbooks = [{"asin": b["asin"], "cover": b["cover"], "title": b["title"], "author": b["author"],
                "state": "available" if b["_id"] % 11 == 0 else "new"}
               for b in BOOKS if nm.lower() in (b.get("narrator") or "").lower()]
    write(f"narrator-{slug(nm)}.html", render("narrator.html", f"/narrator/{nm}",
          narrator=nm, books=_nbooks[:24])); pages += 1

# minimal PWA manifest for the demo
json.dump({"name": "Stackarr (demo)", "short_name": "Stackarr", "start_url": "index.html",
           "display": "standalone", "background_color": "#0b1120", "theme_color": "#0b1120",
           "icons": [{"src": "static/icon-180.png", "sizes": "180x180", "type": "image/png"},
                     {"src": "static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]},
          open(os.path.join(OUT, "manifest.webmanifest"), "w"), indent=1)

print(f"wrote {pages} pages + manifest to {OUT}")
