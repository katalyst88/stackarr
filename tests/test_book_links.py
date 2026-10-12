"""Every card opens a Stackarr book page.

Home's "Read" shelf linked only the two books that had an ASIN; the other 21
(library books, which rarely carry one) were href="#" and reloaded Home.
Suggestions → "Recently added" linked to http://audiobookshelf:80/item/…, the
server's Docker-internal ABS address, which no browser can open."""
import re
from urllib.parse import parse_qs, urlparse

from stackarr import absclient, audible, db

FINISHED = [{"item_id": "li_pen", "finished": True, "progress": 1, "last_update": 3, "reading": False},
            {"item_id": "li_uns", "finished": True, "progress": 1, "last_update": 2, "reading": False},
            {"item_id": "li_odd", "finished": True, "progress": 1, "last_update": 1, "reading": False}]


def _seed(uid):
    with db.conn() as c:
        c.execute("UPDATE users SET abs_token='tok' WHERE id=?", (uid,))
        for iid, title, author, asin in (("li_pen", "Pendragon", "D. J. MacHale", "B0FN5ZCB81"),
                                         ("li_uns", "Unsouled (Cradle Book 1)", "Will Wight", ""),
                                         ("li_odd", "A Very Obscure Book", "Nobody Known", "")):
            c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES (?,?,?,?,?)",
                      (iid, title, author, asin, "audiobook"))
        # the same work Unsouled has an ASIN on a request row
        c.execute("INSERT INTO requests (user_id,asin,title,author,status,format) VALUES (?,?,?,?,?,?)",
                  (uid, "B07XLNMNK4", "Unsouled", "Will Wight", "available", "audiobook"))


def _read_shelf_hrefs(html):
    sec = html.split('id="shelf-read"', 1)[1].split("</section>", 1)[0]
    return re.findall(r'<a class="media-card"[^>]*href="([^"]*)"', sec)


def test_every_read_card_links_to_a_book_page(admin_client, monkeypatch):
    _seed(admin_client.uid)
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: FINISHED)
    hrefs = _read_shelf_hrefs(admin_client.get("/home").get_data(as_text=True))
    assert len(hrefs) == 3 and "#" not in hrefs
    assert hrefs[0] == "/book/B0FN5ZCB81"
    assert hrefs[1] == "/book/B07XLNMNK4"                       # resolved locally, no network
    q = parse_qs(urlparse(hrefs[2].replace("&amp;", "&")).query)
    assert hrefs[2].startswith("/book/find?") and q["title"] == ["A Very Obscure Book"]


def test_find_resolves_by_catalogue_in_the_install_language(admin_client, monkeypatch):
    calls = []

    def search(q, num=12, page=0):
        calls.append(q)
        return [{"asin": "B0FVXFFNKF", "title": "Dungeon Crawler Carl (Italian Edition)",
                 "author": "Matt Dinniman", "language": "Italian"},
                {"asin": "B08V7VS8H2", "title": "Dungeon Crawler Carl", "author": "Matt Dinniman",
                 "language": "English"}]
    monkeypatch.setattr(audible, "search", search)
    r = admin_client.get("/book/find?title=Dungeon+Crawler+Carl+(Unabridged)&author=Matt+Dinniman")
    assert r.status_code == 302 and r.headers["Location"].endswith("/book/B08V7VS8H2")
    # remembered: the next click is a local lookup
    r = admin_client.get("/book/find?title=Dungeon+Crawler+Carl+(Unabridged)&author=Matt+Dinniman")
    assert r.headers["Location"].endswith("/book/B08V7VS8H2") and len(calls) == 1


def test_find_falls_back_to_a_search_not_home(admin_client, monkeypatch):
    monkeypatch.setattr(audible, "search", lambda *a, **k: [])
    r = admin_client.get("/book/find?title=A+Very+Obscure+Book&author=Nobody+Known")
    loc = r.headers["Location"]
    assert "/discover?q=" in loc and "Obscure" in loc


def _suggestions(admin_client, monkeypatch):
    monkeypatch.setattr(absclient, "recent_added", lambda n=14: [
        {"item_id": "li_cap", "title": "The Captain: The Last Horizon, Book 1", "author": "Will Wight", "asin": ""}])
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: [])
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,asin,title,author,status,format) VALUES (?,?,?,?,?,?)",
                  (admin_client.uid, "B0BVMW49D1", "The Captain", "Will Wight", "available", "audiobook"))
        c.execute("INSERT INTO suggestions (user_id,asin,title,author,lane,status,score) VALUES (?,?,?,?,?,?,?)",
                  (admin_client.uid, "B0XXXXXXXX", "Some Pick", "Someone", "author", "pending", 1))
    return admin_client.get("/suggestions").get_data(as_text=True)


def test_recently_added_opens_the_stackarr_book_page(admin_client, monkeypatch):
    db.set_meta("abs_url", "http://audiobookshelf:80")
    html = _suggestions(admin_client, monkeypatch)
    assert "audiobookshelf:80" not in html
    assert 'href="/book/B0BVMW49D1"' in html
    assert "Open in Audiobookshelf" not in html                # no public ABS URL configured


def test_recently_added_offers_abs_only_with_a_public_url(admin_client, monkeypatch):
    db.set_meta("abs_public_url", "https://abs.example.com/")
    html = _suggestions(admin_client, monkeypatch)
    assert 'href="https://abs.example.com/item/li_cap"' in html


def test_public_url_must_be_http(admin_client, monkeypatch):
    db.set_meta("abs_public_url", "javascript:alert(1)")
    assert "Open in Audiobookshelf" not in _suggestions(admin_client, monkeypatch)
