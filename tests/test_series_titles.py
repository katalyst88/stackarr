"""Library books the library titles badly still open their real book page.

Live (2026-10-12): Audiobookshelf holds Cradle #10's ebook as "Reape"
(series "Cradle", position 10) and #5's as "Ghostwater (Cradle Book 5)";
neither has an ASIN, nor does any request row, so the Home Read cards showed
"Reape" and linked to the /book/find search. The Cradle series map Stackarr
already cached names both books with their ASINs."""
import json
import re
import time

from stackarr import absclient, db, routes, scheduler

CRADLE = {"name": "Cradle", "series_asin": "B07GVRZT9V", "source": "audible", "books": [
    {"seq": 5.0, "asin": "B07Q1JZ2DL", "title": "Ghostwater", "author": "Will Wight"},
    {"seq": 10.0, "asin": "B09D4RHLRD", "title": "Reaper", "author": "Will Wight"}]}


def _seed(uid):
    db.set_meta("ser:a:B07GVRZT9V", json.dumps({"at": int(time.time()), "v": CRADLE}))
    with db.conn() as c:
        c.execute("UPDATE users SET abs_token='tok' WHERE id=?", (uid,))
        for iid, title, series, seq, fmt in (
                ("li_reape", "Reape", "Cradle", 10.0, "ebook"),
                ("li_reaper_a", "Reaper: Cradle, Book 10", "", None, "audiobook"),
                ("li_ghost", "Ghostwater (Cradle Book 5)", "", None, "ebook"),
                ("li_odd", "A Very Obscure Book", "", None, "ebook")):
            c.execute("INSERT INTO library (item_id,title,author,asin,series,series_seq,format,source) "
                      "VALUES (?,?,?,?,?,?,?,?)", (iid, title, "Will Wight", "", series, seq, fmt, "abs_ebooks"))
    return [{"item_id": i, "finished": True, "progress": 1, "last_update": n, "reading": False}
            for n, i in enumerate(["li_odd", "li_ghost", "li_reaper_a", "li_reape"])]


def _read_cards(html):
    sec = html.split('id="shelf-read"', 1)[1].split("</section>", 1)[0]
    return re.findall(r'<a class="media-card" data-format="(\w+)" href="([^"]*)">.*?media-title" title="([^"]*)"', sec, re.S)


def test_mangled_and_decorated_library_titles_open_their_book(admin_client, monkeypatch):
    hist = _seed(admin_client.uid)
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: hist)
    queued = []
    monkeypatch.setattr(scheduler, "queue_job", lambda *a: queued.append(a) or True)
    cards = _read_cards(admin_client.get("/home").get_data(as_text=True))
    by_href = {h: (f, t) for f, h, t in cards}
    # "Reape" (ebook, Cradle #10) and "Reaper: Cradle, Book 10" (audiobook) are one book now
    assert by_href["/book/B09D4RHLRD"][1].startswith("Reaper") and "Reape" not in [t for _, _, t in cards]
    assert "/book/B07Q1JZ2DL" in by_href and by_href["/book/B07Q1JZ2DL"][0] == "ebook"
    assert len([h for _, h, _ in cards if h == "/book/B09D4RHLRD"]) == 1
    # the one book nothing knows: /book/find on click, and resolved in the background now
    assert any(h.startswith("/book/find?") for _, h, _ in cards)
    assert ("bookid", "A Very Obscure Book", "Will Wight", "ebook") in queued


def test_a_mangled_title_alone_shows_the_catalogue_title(admin_client, monkeypatch):
    hist = [h for h in _seed(admin_client.uid) if h["item_id"] == "li_reape"]
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: hist)
    cards = _read_cards(admin_client.get("/home").get_data(as_text=True))
    assert cards == [("ebook", "/book/B09D4RHLRD", "Reaper")]


def test_background_job_fills_the_book_id(tmp_db, monkeypatch):
    from stackarr import audible
    monkeypatch.setattr(audible, "search", lambda q, num=12, page=0: [
        {"asin": "B0OBSCURE1", "title": "A Very Obscure Book", "author": "Will Wight", "language": "English"}])
    scheduler._run_job("bookid", "A Very Obscure Book", "Will Wight", "ebook")
    assert routes._known_book_id("A Very Obscure Book", "Will Wight") == "B0OBSCURE1"
