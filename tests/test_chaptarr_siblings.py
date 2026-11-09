"""Chaptarr lists one work more than once (live, Matt Dinniman, 2026-10-12):
a series-linked row and a series-less row from another metadata source.
Stackarr searched the first title match, so DCC #2's ebook was downloaded
again (the file sat on the sibling row 8866) and its audiobook import was
blocked: "grabbed for 'Carl's Doomsday Scenario' (BookId 8798) … but import
matched (BookId 8805)"."""
from fakes import CHAPTARR  # noqa: F401
from stackarr import chaptarr, db

ROWS = [
    {"id": 8798, "title": "Carl's Doomsday Scenario", "mediaType": "audiobook", "monitored": True,
     "seriesTitle": "Dungeon Crawler Carl #2", "foreignBookId": "gr:87846872", "statistics": {"bookFileCount": 0}},
    {"id": 8805, "title": "Carl's Doomsday Scenario", "mediaType": "audiobook", "monitored": False,
     "asin": "B09345T1HY", "foreignBookId": "hc:446680", "statistics": {"bookFileCount": 0}},
    {"id": 8859, "title": "Carl's Doomsday Scenario", "mediaType": "ebook", "monitored": True,
     "seriesTitle": "Dungeon Crawler Carl #2", "statistics": {"bookFileCount": 0}},
    {"id": 8866, "title": "Carl's Doomsday Scenario: Dungeon Crawler Carl Book 2", "mediaType": "ebook",
     "monitored": True, "asin": "B08PBCD9Y7", "statistics": {"bookFileCount": 1}},
    {"id": 8851, "title": "Dungeon Crawler Carl Series 4-Book Collection Set", "mediaType": "ebook",
     "isOmnibus": True, "statistics": {"bookFileCount": 0}},
]


def test_work_rows_finds_decorated_siblings():
    ids = [r["id"] for r in chaptarr.work_rows(ROWS, "Carl's Doomsday Scenario", media="ebook")]
    assert ids == [8859, 8866]


def test_a_file_on_any_sibling_means_chaptarr_has_it():
    row, have = chaptarr.pick_row(ROWS, "Carl's Doomsday Scenario", "B0934VH42B", "ebook")
    assert have["id"] == 8866 and row["id"] == 8866


def test_no_file_prefers_the_row_with_an_asin():
    row, have = chaptarr.pick_row(ROWS, "Carl's Doomsday Scenario", "B0934VH42B", "audiobook")
    assert have is None and row["id"] == 8805


def _setup(fake):
    fake.authors = [{"id": 59, "authorName": "Matt Dinniman", "monitored": True,
                     "audiobookMonitorExisting": 2, "ebookMonitorExisting": 2,
                     "audiobookRootFolderPath": "/audiobooks", "ebookRootFolderPath": "/books"}]
    fake.books[59] = [dict(r, authorId=59) for r in ROWS]


def test_handoff_skips_a_book_chaptarr_already_has(fake_chaptarr):
    _setup(fake_chaptarr)
    res = chaptarr.add_and_search("Carl's Doomsday Scenario", "Matt Dinniman", "B0934VH42B", fmt="ebook")
    assert res["ok"] and res.get("available")
    assert not fake_chaptarr.calls_to("POST", "/api/v1/command")         # no second download


def test_handoff_searches_the_row_the_import_will_match(fake_chaptarr):
    _setup(fake_chaptarr)
    res = chaptarr.add_and_search("Carl's Doomsday Scenario", "Matt Dinniman", "B0934VH42B", fmt="audiobook")
    assert res["ok"] and not res.get("available")
    cmd = fake_chaptarr.calls_to("POST", "/api/v1/command")[-1][3]
    assert cmd["bookIds"] == [8805]


def test_series_handoff_uses_the_same_rules(fake_chaptarr):
    _setup(fake_chaptarr)
    res = chaptarr.add_books("Matt Dinniman", [{"title": "Carl's Doomsday Scenario", "asin": "B0934VH42B"}], fmt="ebook")
    assert res["books"][0].get("available")
    res = chaptarr.add_books("Matt Dinniman", [{"title": "Carl's Doomsday Scenario", "asin": "B0934VH42B"}], fmt="audiobook")
    assert fake_chaptarr.calls_to("POST", "/api/v1/command")[-1][3]["bookIds"] == [8805]


def test_has_book_file_sees_the_sibling(fake_chaptarr):
    _setup(fake_chaptarr)
    assert chaptarr.has_book_file("Carl's Doomsday Scenario", "Matt Dinniman", "", "ebook", "59")
    assert not chaptarr.has_book_file("Carl's Doomsday Scenario", "Matt Dinniman", "", "audiobook", "59")


def test_direct_request_records_available(admin_client, fake_chaptarr):
    from fakes import SAME_ORIGIN
    _setup(fake_chaptarr)
    db.set_meta("formats", "ebook")
    r = admin_client.post("/api/request", json={"title": "Carl's Doomsday Scenario", "author": "Matt Dinniman",
                                                "asin": "B0934VH42B", "format": "ebook"}, headers=SAME_ORIGIN)
    assert r.get_json()["ok"]
    with db.conn() as c:
        assert c.execute("SELECT status FROM requests").fetchone()["status"] == "available"


def test_a_different_book_number_is_not_a_sibling():
    rows = [{"id": i, "title": f"Long Book {i}", "mediaType": "audiobook"} for i in (1, 2, 3)]
    assert [r["id"] for r in chaptarr.work_rows(rows, "Long Book 3", media="audiobook")] == [3]


def test_a_409_from_the_download_client_reads_as_already_there(fake_chaptarr, monkeypatch):
    from fakes import Resp
    rec = {"status": "warning", "trackedDownloadStatus": "warning", "trackedDownloadState": "downloading",
           "errorMessage": "qBittorrent: 409 Conflict", "statusMessages": [], "size": 1, "sizeleft": 1,
           "book": {"title": "Carl's Doomsday Scenario", "mediaType": "audiobook"}}
    monkeypatch.setattr(chaptarr.requests, "get", lambda *a, **k: Resp(200, {"records": [rec]}))
    q = chaptarr.queue()[0]
    assert q["state"] == "waiting" and q["reason"].startswith("Already in the download client")
