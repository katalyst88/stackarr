"""A book you started this morning is "Currently reading" on Home and counts
as in progress on Insights. listening_history() used to drop every unfinished
book under 25% (the recommender's seed threshold), and Home then also wanted
2%, so Dungeon Crawler Carl at 7 minutes in (0.9%) appeared nowhere."""
from stackarr import absclient, db

ME = {"mediaProgress": [
    # started this morning: 455 s of a 13.5 h book
    {"libraryItemId": "li_dcc", "isFinished": False, "progress": 0.0089, "currentTime": 455.3,
     "lastUpdate": 1_760_000_000_000},
    # an 11-second stray tap months ago — not "reading"
    {"libraryItemId": "li_tap", "isFinished": False, "progress": 0.0004, "currentTime": 11.5,
     "lastUpdate": 1_750_000_000_000},
    # hidden from Continue Listening in ABS
    {"libraryItemId": "li_hid", "isFinished": False, "progress": 0.4, "currentTime": 9000,
     "hideFromContinueListening": True, "lastUpdate": 1_750_000_000_000},
    {"libraryItemId": "li_cap", "isFinished": True, "progress": 1, "lastUpdate": 1_760_000_000_001},
]}


def _fake_me(monkeypatch):
    monkeypatch.setattr(absclient, "_user_get", lambda tok, path, params=None: ME)


def test_recommender_threshold_unchanged(monkeypatch):
    _fake_me(monkeypatch)
    ids = [h["item_id"] for h in absclient.listening_history("tok")]
    assert ids == ["li_cap", "li_hid"]                  # finished + 25%+ only, as before


def test_started_includes_just_begun(monkeypatch):
    _fake_me(monkeypatch)
    hist = {h["item_id"]: h for h in absclient.listening_history("tok", started=True)}
    assert hist["li_dcc"]["reading"] is True
    assert "li_tap" not in hist
    assert hist["li_hid"]["reading"] is False and hist["li_cap"]["reading"] is False


def _home(admin_client, monkeypatch, lib_rows):
    _fake_me(monkeypatch)
    monkeypatch.setattr(absclient, "item_detail", lambda iid: {
        "item_id": iid, "title": "Dungeon Crawler Carl (Unabridged)", "author": "Matt Dinniman", "asin": ""})
    with db.conn() as c:
        c.execute("UPDATE users SET abs_token='tok' WHERE id=?", (admin_client.uid,))
        for iid, title, author in lib_rows:
            c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES (?,?,?,?,?)",
                      (iid, title, author, "", "audiobook"))
    html = admin_client.get("/home").get_data(as_text=True)
    return html.split('id="shelf-reading"', 1)[1].split("</section>", 1)[0]


def test_home_currently_reading_shows_a_book_started_today(admin_client, monkeypatch):
    sec = _home(admin_client, monkeypatch, [("li_dcc", "Dungeon Crawler Carl (Unabridged)", "Matt Dinniman"),
                                            ("li_tap", "Cradle 12.5", "Will Wight"),
                                            ("li_cap", "The Captain", "Will Wight")])
    assert "Dungeon Crawler Carl" in sec
    assert "Cradle 12.5" not in sec and "The Captain" not in sec


def test_home_currently_reading_before_the_library_refresh(admin_client, monkeypatch):
    """Added to ABS today and started before Stackarr's 30-minute library
    refresh saw it: the shelf asks ABS for that item instead of dropping it."""
    sec = _home(admin_client, monkeypatch, [])
    assert "Dungeon Crawler Carl" in sec


def test_insights_in_progress_counts_a_book_started_today(admin_client, monkeypatch):
    _fake_me(monkeypatch)
    monkeypatch.setattr(absclient, "listening_stats", lambda tok: {"total_seconds": 0})
    with db.conn() as c:
        c.execute("UPDATE users SET abs_token='tok' WHERE id=?", (admin_client.uid,))
        c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES "
                  "('li_dcc','Dungeon Crawler Carl (Unabridged)','Matt Dinniman','','audiobook')")
    html = admin_client.get("/insights").get_data(as_text=True)
    assert '<div class="tile-num">1</div><div class="tile-label">in progress' in html
