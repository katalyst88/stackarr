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
# dummy backend creds so create_app() boots with no real services
os.environ.setdefault("ABS_URL", "http://demo.invalid")
os.environ.setdefault("ABS_ADMIN_TOKEN", "demo")
os.environ.setdefault("CHAPTARR_URL", "http://demo.invalid")
os.environ.setdefault("CHAPTARR_API_KEY", "demo")

import sys; sys.path.insert(0, ROOT)
from stackarr import create_app, config            # noqa: E402
from flask import render_template, render_template_string, url_for  # noqa: E402

OUT = os.path.join(ROOT, "docs")
BOOKS = json.load(open(os.path.join(ROOT, "audit", "demo_books.json"), encoding="utf-8"))
for i, b in enumerate(BOOKS):
    b["_id"] = i + 1
app = create_app()

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
            "author": b["author"], "reason": reason_for(lane, b),
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
                   "author": b["author"], "cover": b["cover"]} for b in BOOKS[3:12]]
cover_map = {r["item_id"]: r["cover"] for r in recently_added}

_st = ["available", "queued", "handed", "failed", "available", "queued"]
recent_requests = [{"asin": b["asin"], "cover": b["cover"], "title": b["title"],
                    "author": b["author"], "status": _st[k % len(_st)]}
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
insights_ctx = dict(
    hours=412, finished=87, in_progress=4, req_avail=23, top_authors=top_authors,
    facts=[("🎧", "412 hours", "about 17 full days of audio"),
           ("📚", "87 books", "finished cover to cover"),
           ("🏆", top_authors[0][0], "your most-listened author"),
           ("⚡", "11 series", "followed to the latest book")],
)


# ---- render -----------------------------------------------------------------
def base_ctx():
    return dict(app_name=config.APP_NAME, accent=config.ACCENT, version="1.0",
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
    repl = {url_for("main.suggestions_page"): "index.html",
            url_for("main.insights_page"): "insights.html",
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
    for a in all_authors:
        repl[url_for("main.browse_page", author=a)] = f"author-{slug(a)}.html"
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
write("index.html", render("suggestions.html", "/suggestions", lanes=lanes,
      lane_titles=LANE_TITLES, genres=home_genres, rec_authors=rec_authors,
      recently_added=recently_added, recent_requests=recent_requests, abs_base="#")); pages += 1
write("insights.html", render("insights.html", "/insights", **insights_ctx)); pages += 1
write("requests.html", render("browse.html", "/requests", kind="genre",
      title="Your requests", author=None, books=genre_books(all_genres[0]))); pages += 1
write("demo-info.html", render_info("/settings")); pages += 1
for lane, cards in lanes.items():
    write(f"lane-{lane}.html", render("lane.html", f"/lane/{lane}",
          title=LANE_TITLES[lane], lane=lane, rows=cards)); pages += 1
states = {}
for k, b in enumerate(BOOKS):
    st = ["new", "new", "new", "available", "new", "queued", "new", "failed"][k % 8]
    write(f"book-{b['asin']}.html", render("book.html", f"/book/{b['asin']}",
          b=book_detail(b, st))); pages += 1
for g in all_genres:
    write(f"genre-{slug(g)}.html", render("browse.html", "/browse", kind="genre",
          title=g, author=None, books=genre_books(g))); pages += 1
for a in all_authors:
    write(f"author-{slug(a)}.html", render("browse.html", "/browse", kind="author",
          title=a, author=a, books=author_books(a))); pages += 1

# minimal PWA manifest for the demo
json.dump({"name": "Stackarr (demo)", "short_name": "Stackarr", "start_url": "index.html",
           "display": "standalone", "background_color": "#0b1120", "theme_color": "#0b1120",
           "icons": [{"src": "static/icon-180.png", "sizes": "180x180", "type": "image/png"},
                     {"src": "static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]},
          open(os.path.join(OUT, "manifest.webmanifest"), "w"), indent=1)

print(f"wrote {pages} pages + manifest to {OUT}")
