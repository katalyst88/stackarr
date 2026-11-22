"""Inline handlers that take a `|tojson` value must sit in a SINGLE-quoted
attribute. tojson emits a double-quoted JSON string, so
`onclick="Stackarr.rate({{ k|tojson }},1,this)"` renders as
`onclick="Stackarr.rate("B0…",1,this)"`: the attribute ends at the second `"`,
the handler is the syntax error `Stackarr.rate(`, and the click does nothing.
That was every star on History, the book page and the Suggestions quick-rate
card (plus Find missing / Clear / Remove / Add-all-by-author)."""
import glob
import json
import os
import re
from html.parser import HTMLParser

from stackarr import absclient, audible, audnexus, db, tagging

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "stackarr", "templates")


def test_no_tojson_inside_double_quoted_attributes():
    bad = []
    for path in glob.glob(os.path.join(TEMPLATES, "*.html")):
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r'\s[\w-]+="[^"]*\|\s*tojson[^"]*"', src):
            bad.append(f"{os.path.basename(path)}: {m.group(0).strip()[:90]}")
    assert not bad, "tojson inside a double-quoted attribute:\n" + "\n".join(bad)


class _Handlers(HTMLParser):
    def __init__(self):
        super().__init__()
        self.onclick = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "onclick" in a and "star" in (a.get("class") or "").split():
            self.onclick.append(a["onclick"])


def _stars(html):
    p = _Handlers()
    p.feed(html)
    return p.onclick


def _assert_rate_calls(calls, key):
    assert len(calls) == 5, calls
    for n, h in enumerate(calls, 1):
        m = re.fullmatch(r"Stackarr\.rate\((.+),(\d),this\)", h)
        assert m, h
        assert json.loads(m.group(1)) == key and int(m.group(2)) == n


def test_history_stars_are_callable(admin_client, monkeypatch):
    with db.conn() as c:
        c.execute("INSERT INTO ratings (user_id,asin,title,author,stars) VALUES (?,?,?,?,?)",
                  (admin_client.uid, "t-the-captain-will-wight", "The Captain", "Will Wight", 4))
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: [])
    html = admin_client.get("/history").get_data(as_text=True)
    _assert_rate_calls(_stars(html), "t-the-captain-will-wight")


def test_book_page_stars_are_callable(admin_client, monkeypatch):
    monkeypatch.setattr(audible, "by_asin", lambda a: {"asin": a, "title": "The Pilot", "author": "Will Wight"})
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    html = admin_client.get("/book/B0F8CJQZM3").get_data(as_text=True)
    _assert_rate_calls(_stars(html), "B0F8CJQZM3")
