"""Insights reflect current data, the same way Home does.

Live on 2026-10-12 it showed 30 books finished for 13 books (the library holds
several Cradle books twice, e.g. "Unsouled (Cradle Book 1)" and "Unsouled:
Cradle, Volume 1", and numbered rips like "09 Bloodline"), 1 author explored,
0 in progress (the book started that morning was under the 25% cut-off) and
285 "added via Stackarr" (one per format row, back-filled rows included).
Ratings were stale because the stars never saved (fixed separately)."""
import datetime
import time

from stackarr import absclient, db

NOW = time.time() * 1000


def _seed(uid):
    items = [("li_1", "Unsouled (Cradle Book 1)"), ("li_2", "Unsouled: Cradle, Volume 1"),
             ("li_3", "09 Bloodline"), ("li_4", "Bloodline (Cradle Book 9)"),
             ("li_5", "The Captain: The Last Horizon, Book 1"), ("li_6", "Dungeon Crawler Carl (Unabridged)")]
    authors = {"li_6": "Matt Dinniman"}
    with db.conn() as c:
        c.execute("UPDATE users SET abs_token='tok' WHERE id=?", (uid,))
        for iid, t in items:
            c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES (?,?,?,?,?)",
                      (iid, t, authors.get(iid, "Will Wight"), "", "audiobook"))
        # The Captain is ALSO on the Read shelf — still one book
        c.execute("INSERT INTO shelf (user_id,rkey,state,title,author,format,finished_at) VALUES "
                  "(?,?,?,?,?,?,datetime('now','localtime'))", (uid, "B0BVMW49D1", "read", "The Captain", "Will Wight", "audiobook"))
        for title, fmt, src in (("The Captain", "audiobook", "series_book"), ("The Captain", "ebook", "series_book"),
                                ("Some Old Book", "audiobook", "backfill")):
            c.execute("INSERT INTO requests (user_id,title,author,status,format,source) VALUES (?,?,?,?,?,?)",
                      (uid, title, "Will Wight", "available", fmt, src))
    return [{"item_id": i, "finished": True, "progress": 1, "last_update": NOW - n * 86400_000, "reading": False}
            for n, i in enumerate(["li_5", "li_1", "li_2", "li_3", "li_4"])] + [
        {"item_id": "li_6", "finished": False, "progress": 0.009, "last_update": NOW, "reading": True}]


def _tiles(html):
    import re
    return dict((label, int(n)) for n, label in
                re.findall(r'tile-num">([\d,]+)</div><div class="tile-label">([^<]*)', html))


def test_insights_count_each_book_once(admin_client, monkeypatch):
    hist = _seed(admin_client.uid)
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: hist)
    monkeypatch.setattr(absclient, "listening_stats", lambda tok: {"total_seconds": 3600 * 102})
    tiles = _tiles(admin_client.get("/insights").get_data(as_text=True))
    assert tiles == {"books finished": 3, "in progress": 1, "authors explored": 2, "added via Stackarr": 1}


def test_insights_year_matches_home(admin_client, monkeypatch):
    hist = _seed(admin_client.uid)
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: hist)
    monkeypatch.setattr(absclient, "listening_stats", lambda tok: {"total_seconds": 0})
    db.set_meta(f"goal_{admin_client.uid}", "24")
    year = datetime.date.today().year
    home = admin_client.get("/home").get_data(as_text=True)
    ins = admin_client.get("/insights").get_data(as_text=True)
    num = '<span class="goal-bar-num">{}</span>'
    expected = sum(1 for n in range(3) if str(datetime.date.fromtimestamp((NOW - n * 86400_000) / 1000).year) == str(year))
    assert num.format(expected) in home and num.format(expected) in ins


def test_a_new_rating_shows_up(admin_client, monkeypatch):
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: [])
    monkeypatch.setattr(absclient, "listening_stats", lambda tok: {"total_seconds": 0})
    from fakes import SAME_ORIGIN
    r = admin_client.post("/api/rate", json={"asin": "t-the-captain-will-wight", "stars": 4,
                                             "title": "The Captain", "author": "Will Wight"}, headers=SAME_ORIGIN)
    assert r.get_json()["ok"]
    assert "4.0 avg" in admin_client.get("/insights").get_data(as_text=True)


def test_home_read_shelf_shows_a_library_duplicate_once(admin_client, monkeypatch):
    hist = _seed(admin_client.uid)
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: hist)
    import re
    html = admin_client.get("/home").get_data(as_text=True)
    sec = html.split('id="shelf-read"', 1)[1].split("</section>", 1)[0]
    shown = re.findall(r'media-title" title="([^"]*)"', sec)
    assert len(shown) == 3, shown           # The Captain (also on the Read shelf), Unsouled, Bloodline
