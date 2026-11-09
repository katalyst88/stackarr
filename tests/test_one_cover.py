"""One book, one cover, in every section. The Home series strip ("Keep
going") drew the catalogue's Audible art (banner and all) from the cached
series map, while the shelves drew the Audiobookshelf cover, so the same book
looked different two sections apart. Every section now asks covers.for_book:
the library's own cover when the household owns the book (Audiobookshelf
first, where covers are curated), the catalogue's only when it doesn't. And a
cover fixed in Audiobookshelf shows at once: its URL carries the item's update
time and the disk cache treats an older copy as stale."""
import os
import time

import pytest

from stackarr import absclient, audible, audnexus, covers, db, routes, scheduler, seriesmap, tagging

AUDIBLE = "https://m.media-amazon.com/images/I/81banner._SL500_.jpg"
ITEM = "6437a2f9"
GATE = "The Gate of the Feral Gods"


def _own(item=ITEM, title=GATE, source="abs", cover_at=1_700_000_000, fmt="audiobook"):
    with db.conn() as c:
        c.execute("INSERT INTO library (item_id,title,author,asin,format,source,cover_at) VALUES (?,?,?,?,?,?,?)",
                  (item, title, "Matt Dinniman", "", fmt, source, cover_at))
    db.invalidate_cover_index()


@pytest.fixture
def series(admin_client, monkeypatch):
    """A cached series map whose art is Audible's, the way the strip gets it."""
    books = [{"seq": float(i), "asin": f"B0SERIES{i:02d}", "title": t, "author": "Matt Dinniman",
              "cover": AUDIBLE, "release_date": "2021-01-01"}
             for i, t in enumerate(["Dungeon Crawler Carl", "Carl's Doomsday Scenario", "The Dungeon Anarchist's Cookbook",
                                    GATE, "The Butcher's Masquerade"], start=1)]
    monkeypatch.setattr(seriesmap, "resolve", lambda **kw: {"name": "Dungeon Crawler Carl", "series_asin": "",
                                                           "source": "audible", "books": [dict(b) for b in books]})
    monkeypatch.setattr(routes, "abs_history", lambda u: [])
    return books


def _strip_cover(admin_client, title):
    d = admin_client.get("/api/series/next?series=Dungeon+Crawler+Carl&author=Matt+Dinniman").get_json()
    assert d["ok"], d
    return next(b["cover"] for b in d["books"] if b["title"] == title)


def test_the_series_strip_and_the_shelves_show_the_same_cover(admin_client, series):
    _own()
    want = f"/cover/{ITEM}?v=1700000000"
    assert _strip_cover(admin_client, GATE) == want                         # "Keep going"
    with admin_client.application.test_request_context():
        # a shelf / request card for the same book, however its row stored the cover
        assert covers.bookcover({"title": GATE, "author": "Matt Dinniman", "cover": AUDIBLE}) == want
        assert covers.bookcover({"title": GATE, "author": "Matt Dinniman", "cover": f"/cover/{ITEM}"}) == want
        assert covers.bookcover({"title": GATE, "author": "Matt Dinniman", "cover": ""}) == want


def test_the_book_page_shows_the_library_cover_too(admin_client, monkeypatch):
    _own()
    monkeypatch.setattr(audible, "by_asin", lambda a: {"asin": a, "title": GATE, "author": "Matt Dinniman",
                                                       "cover": AUDIBLE})
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    html = admin_client.get("/book/B09GCYRMLS").get_data(as_text=True)
    cover = html.split('class="book-cover"', 1)[1].split("</div>", 1)[0]
    assert f'src="/cover/{ITEM}?v=1700000000"' in cover and "media-amazon" not in cover


def test_audiobookshelf_wins_over_another_ebook_server(admin_client, series):
    _own(item="cw-row", source="calibreweb", fmt="ebook")
    with db.conn() as c:
        c.execute("UPDATE library SET cover='cw:161' WHERE item_id='cw-row'")
    _own(item="abs-ebook", source="abs_ebooks", fmt="ebook", cover_at=5)
    assert _strip_cover(admin_client, GATE) == "/cover/abs-ebook?v=5"
    _own(item=ITEM)                                                          # the ABS audiobook beats both
    assert _strip_cover(admin_client, GATE) == f"/cover/{ITEM}?v=1700000000"


def test_a_book_we_dont_own_keeps_the_catalogue_cover(admin_client, series):
    # same title, someone else's book: never borrow its cover
    with db.conn() as c:
        c.execute("INSERT INTO library (item_id,title,author,format,source) "
                  "VALUES ('other','The Butcher''s Masquerade','Someone Else','audiobook','abs')")
    db.invalidate_cover_index()
    assert _strip_cover(admin_client, "The Butcher's Masquerade") == AUDIBLE.replace("_SL500_", "_SL1500_")


# -- a cover fixed in Audiobookshelf shows without waiting --------------------

class _Raw:
    def __init__(self, body):
        self.ok, self.status_code, self.content, self.headers = True, 200, body, {"Content-Type": "image/jpeg"}


def test_a_cover_changed_in_audiobookshelf_is_fetched_again(admin_client, monkeypatch):
    import requests
    monkeypatch.setattr(absclient, "abs_url", lambda: "http://abs")
    monkeypatch.setattr(absclient, "admin_token", lambda: "t")
    served = {"body": b"\xff\xd8old"}
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Raw(served["body"]))
    _own(cover_at=int(time.time()) - 3600)
    assert admin_client.get(f"/cover/{ITEM}").data == b"\xff\xd8old"          # fetched and cached
    served["body"] = b"\xff\xd8new"
    assert admin_client.get(f"/cover/{ITEM}").data == b"\xff\xd8old"          # unchanged item: the cache answers
    # the owner fixes the cover in ABS; the next library refresh sees a newer updatedAt
    with db.conn() as c:
        c.execute("UPDATE library SET cover_at=? WHERE item_id=?", (int(time.time()) + 5, ITEM))
    db.invalidate_cover_index()
    assert admin_client.get(f"/cover/{ITEM}").data == b"\xff\xd8new"
    path = os.path.join(routes._cover_dir(), f"{ITEM}.jpg")
    assert open(path, "rb").read() == b"\xff\xd8new"


def test_the_library_refresh_records_the_cover_version(tmp_db, monkeypatch):
    m = absclient.item_meta({"id": ITEM, "updatedAt": 1_731_100_000_123,
                             "media": {"metadata": {"title": GATE, "authorName": "Matt Dinniman"}}})
    assert m["cover_at"] == 1_731_100_000

    class _Src:
        id = "abs"

        def library_items(self):
            return [dict(m, format="audiobook", source="abs")]
    monkeypatch.setattr(scheduler.backends, "sources", lambda mode: [_Src()])
    scheduler.refresh_library()
    assert db.cover_version(ITEM) == 1_731_100_000
