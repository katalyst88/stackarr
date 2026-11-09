"""Work-first book page and cards: the page is the book — title, author,
series — then a Formats block with an Audiobook and an eBook row, each with
its own status and action. It used to be the Audible audiobook record with
an "Add" pair bolted on."""
import json
import re
import time

from stackarr import absclient, audible, audnexus, db, tagging, titles

DCC = {"asin": "B08V7VS8H2", "title": "Dungeon Crawler Carl (Unabridged)", "author": "Matt Dinniman",
       "narrator": "Jeff Hays", "runtime_hours": 13.5, "series": "Dungeon Crawler Carl", "sequence": 1.0,
       "series_asin": "B0937J612G", "cover": "", "format": "audiobook"}


def _page(client, monkeypatch, mode="both", public=""):
    db.set_meta("formats", mode)
    if public:
        db.set_meta("abs_public_url", public)
    monkeypatch.setattr(audible, "by_asin", lambda a: dict(DCC))
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    with db.conn() as c:
        c.execute("INSERT INTO library (item_id,title,author,asin,format,source) VALUES "
                  "('7930852b','Dungeon Crawler Carl (Unabridged)','Matt Dinniman','B08V8B2CGV','audiobook','abs')")
        c.execute("INSERT INTO requests (user_id,asin,title,author,status,format) VALUES "
                  "(?,'B08V8B2CGV','Dungeon Crawler Carl','Matt Dinniman','handed','ebook')", (client.uid,))
    return client.get("/book/B08V7VS8H2").get_data(as_text=True)


def _rows(html):
    sec = html.split('class="formats"', 1)[1].split("</section>", 1)[0]
    return re.findall(r'class="format-row" data-fmt="(\w+)">(.*?)(?=<div class="format-row"|$)', sec, re.S)


def test_header_is_the_work(admin_client, monkeypatch):
    html = _page(admin_client, monkeypatch)
    assert '<h1 class="book-title">Dungeon Crawler Carl</h1>' in html          # no "(Unabridged)"
    assert 'href="/author/Matt%20Dinniman"' in html
    series = html.split('class="book-series"', 1)[1].split("</p>", 1)[0]
    assert "/series/view?" in series and "series_asin=B0937J612G" in series and "Book 1" in series


def test_formats_block_has_a_row_per_format(admin_client, monkeypatch):
    rows = dict(_rows(_page(admin_client, monkeypatch, public="https://abs.example.com")))
    assert list(rows) == ["audiobook", "ebook"]
    a, e = rows["audiobook"], rows["ebook"]
    assert "In library" in a and "13.5 h" in a and "Jeff Hays" in a
    assert 'href="https://abs.example.com/item/7930852b"' in a and "Request</button>" not in a
    assert "Requested" in e and "Request</button>" not in e                   # already on its way


def test_missing_format_offers_request(admin_client, monkeypatch):
    _page(admin_client, monkeypatch)
    with db.conn() as c:
        c.execute("DELETE FROM requests")            # nothing on order for the eBook now
    html = admin_client.get("/book/B08V7VS8H2").get_data(as_text=True)
    rows = dict(_rows(html))
    assert 'Stackarr.grabFormat(' in rows["ebook"] and "Request</button>" in rows["ebook"]
    assert "Request</button>" not in rows["audiobook"]


def test_single_format_install_shows_one_row(admin_client, monkeypatch):
    assert [f for f, _ in _rows(_page(admin_client, monkeypatch, mode="audiobook"))] == ["audiobook"]


def test_series_page_lists_the_series_in_order(admin_client, monkeypatch):
    db.set_meta("formats", "audiobook")
    db.set_meta("ser:a:B0937J612G", json.dumps({"at": int(time.time()), "v": {
        "name": "Dungeon Crawler Carl", "series_asin": "B0937J612G", "source": "audible", "books": [
            {"seq": 1.0, "asin": "B08V7VS8H2", "title": "Dungeon Crawler Carl", "author": "Matt Dinniman", "cover": ""},
            {"seq": 2.0, "asin": "B0934VH42B", "title": "Carl's Doomsday Scenario", "author": "Matt Dinniman", "cover": ""}]}}))
    with db.conn() as c:
        c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES "
                  "('li_1','Dungeon Crawler Carl (Unabridged)','Matt Dinniman','','audiobook')")
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: [])
    html = admin_client.get("/series/view?series_asin=B0937J612G&series=Dungeon+Crawler+Carl&author=Matt+Dinniman").get_data(as_text=True)
    titles_ = re.findall(r'media-title" title="[^"]*">([^<]*)<', html)
    assert titles_ == ["#1 · Dungeon Crawler Carl", "#2 · Carl&#39;s Doomsday Scenario"]
    assert "In library" in html and "Request remaining" in html


def test_cards_show_the_work_title_and_series(admin_client, monkeypatch):
    db.set_meta("formats", "audiobook")
    with db.conn() as c:
        c.execute("UPDATE users SET abs_token='tok' WHERE id=?", (admin_client.uid,))
        c.execute("INSERT INTO library (item_id,title,author,asin,series,series_seq,format) VALUES "
                  "('li_b','Cradle Book 9 - Bloodline','Will Wight','','Cradle',9,'audiobook')")
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: [
        {"item_id": "li_b", "finished": True, "progress": 1, "last_update": 1, "reading": False}])
    html = admin_client.get("/home").get_data(as_text=True)
    sec = html.split('id="shelf-read"', 1)[1].split("</section>", 1)[0]
    assert '>Bloodline</div>' in sec and 'class="media-series">Cradle · #9</div>' in sec


def test_display_titles():
    for raw, series, want in (("Reaper: Cradle, Book 10", "Cradle", "Reaper"),
                              ("Cradle Book 9 - Bloodline", "Cradle", "Bloodline"),
                              ("09 Bloodline", "", "Bloodline"),
                              ("The Captain: The Last Horizon, Book 1", "The Last Horizon", "The Captain"),
                              ("Project Hail Mary: A Novel", "", "Project Hail Mary: A Novel"),
                              ("Star Wars: Thrawn", "", "Star Wars: Thrawn")):
        assert titles.display(raw, series) == want, raw


def test_book_page_says_what_the_download_needs(admin_client, monkeypatch):
    """A held-back format shows "What this download needs" in plain words."""
    with db.conn() as c:                                   # _page's ebook request is the next row
        nxt = (c.execute("SELECT COALESCE(MAX(id), 0) FROM requests").fetchone()[0] or 0) + 1
    db.set_meta(f"relcheck:{nxt}", json.dumps({"kind": "needs", "at": int(time.time()), "needs": [
        {"key": "freeleech", "label": "Needs freeleech", "text": "Only freeleech releases are allowed."}]}))
    rows = dict(_rows(_page(admin_client, monkeypatch)))
    e = rows["ebook"]
    assert "What this download needs" in e and "Needs freeleech" in e and "Only freeleech releases" in e
    assert "What this download needs" not in rows["audiobook"]
