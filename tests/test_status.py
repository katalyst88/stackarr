"""One status for a book, everywhere (status.py).

The field case (2026-10-12): The Pilot had a failed audiobook + ebook attempt,
then a working series request for both. The book page said "Requested", the
Requests page "Downloading". Chaptarr's queue also held things Stackarr never
showed: a stalled torrent, a release held by a tracker check."""
import datetime

import pytest

from fakes import Resp
from stackarr import audible, audnexus, chaptarr, db, status, tagging

QUEUE = {"totalRecords": 3, "records": [
    {"status": "downloading", "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading",
     "statusMessages": [], "errorMessage": None, "size": 620646354, "sizeleft": 600000000,
     "timeleft": "4.12:50:11", "book": {"title": "The Pilot: The Last Horizon, Book #4", "mediaType": "audiobook"},
     "author": {"authorName": "Will Wight"}},
    {"status": "warning", "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading",
     "statusMessages": [], "errorMessage": "The download is stalled with no connections", "size": 1, "sizeleft": 1,
     "timeleft": None, "book": {"title": "The Knight", "mediaType": "audiobook"}, "author": {"authorName": "Will Wight"}},
    {"status": "delay", "trackedDownloadStatus": "warning", "trackedDownloadState": "downloading",
     "statusMessages": [{"title": "x", "messages": ["Tracker safety pause: unsatisfied-slot status unavailable or stale"]}],
     "errorMessage": None, "size": 1, "sizeleft": 1, "timeleft": None,
     "book": {"title": "The Engineer", "mediaType": "ebook"}, "author": {"authorName": "Travis Baldree"}},
]}


@pytest.fixture
def queue(fake_chaptarr, monkeypatch):
    calls = []

    def get(url, params=None, **kw):
        if url.endswith("/api/v1/queue"):
            calls.append(url)
            return Resp(200, QUEUE)
        return fake_chaptarr.get(url, params=params, **kw)
    monkeypatch.setattr(chaptarr.requests, "get", get)
    return calls


def test_queue_reads_progress_holds_and_stalls(queue):
    q = {x["title"]: x for x in chaptarr.queue()}
    pilot = q["The Pilot: The Last Horizon, Book #4"]
    assert pilot["state"] == "downloading" and pilot["pct"] == 3 and pilot["eta"] == "4 days"
    assert q["The Knight"]["state"] == "stalled"
    assert q["The Engineer"]["state"] == "waiting" and "Tracker safety pause" in q["The Engineer"]["reason"]


def _req(uid, title, author, st, fmt, asin="", days_ago=0):
    when = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,asin,title,author,status,format,detail,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", (uid, asin, title, author, st, fmt, f"{st} detail", when))


@pytest.fixture
def pilot(tmp_db):
    db.set_meta("formats", "both")
    with db.conn() as c:
        uid = c.execute("INSERT INTO users (username, role) VALUES ('alice','admin')").lastrowid
    for f in ("audiobook", "ebook"):
        _req(uid, "The Pilot", "Will Wight", "failed", f, "B0F8CJQZM3")
    for f in ("audiobook", "ebook"):
        _req(uid, "The Pilot", "Will Wight", "handed", f, "B0F8CJQZM3")
    return uid


def test_latest_attempt_wins_and_the_queue_says_downloading(pilot, queue):
    st = status.for_book("B0F8CJQZM3", "The Pilot", "Will Wight", viewer=pilot)
    assert st["state"] == "downloading" and st["label"] == "Downloading"
    assert st["formats"]["audiobook"]["detail"] == "3% · about 4 days left"
    assert st["formats"]["ebook"]["state"] == "requested"          # the failed attempts are superseded
    assert st["summary"] == "Audiobook: Downloading · eBook: Requested"


def test_stalled_and_held_are_named(tmp_db, queue):
    db.set_meta("formats", "both")
    _req(1, "The Knight", "Will Wight", "handed", "audiobook")
    _req(1, "The Engineer", "Travis Baldree", "handed", "ebook")
    assert status.for_book("", "The Knight", "Will Wight")["state"] == "stalled"
    held = status.for_book("", "The Engineer", "Travis Baldree")
    assert held["state"] == "waiting" and held["detail"].startswith("Held by Chaptarr: Tracker safety pause")


def test_a_request_that_never_reaches_the_queue_is_not_found_yet(tmp_db, queue):
    db.set_meta("formats", "ebook")
    _req(1, "The Captain", "Will Wight", "handed", "ebook", days_ago=status.STALE_DAYS + 1)
    st = status.for_book("", "The Captain", "Will Wight")
    assert st["state"] == "not_found" and "VIP-only" in st["detail"]
    _req(1, "The Engineer", "Will Wight", "handed", "ebook", days_ago=1)
    assert status.for_book("", "The Engineer", "Will Wight")["state"] == "requested"


def test_in_library_wins_overall_but_keeps_the_other_format(tmp_db, queue):
    db.set_meta("formats", "both")
    with db.conn() as c:
        c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES "
                  "('li_1','The Captain: The Last Horizon, Book 1','Will Wight','','audiobook')")
    _req(1, "The Captain", "Will Wight", "handed", "ebook", days_ago=1)
    st = status.for_book("B0BVMW49D1", "The Captain", "Will Wight")
    assert st["state"] == "available" and st["summary"] == "Audiobook: In library · eBook: Requested"


def test_failed_counts_only_for_its_requester(tmp_db, queue):
    db.set_meta("formats", "audiobook")
    _req(7, "Giovanni's Room", "James Baldwin", "failed", "audiobook")
    assert status.for_book("", "Giovanni's Room", "James Baldwin", viewer=1)["state"] == "none"
    assert status.for_book("", "Giovanni's Room", "James Baldwin", viewer=7)["state"] == "failed"


def test_queue_is_read_once_and_only_when_something_is_on_order(tmp_db, queue):
    db.set_meta("formats", "audiobook")
    status.for_book("", "Dune", "Frank Herbert")
    assert queue == []                                              # nothing open: no call
    _req(1, "The Knight", "Will Wight", "handed", "audiobook")
    for _ in range(3):
        status.for_book("", "The Knight", "Will Wight")
    assert len(queue) == 1                                          # cached across renders


def test_every_page_says_the_same_thing(pilot, queue, monkeypatch):
    """Book page, search cards and the series strip agree on The Pilot."""
    from stackarr import create_app
    app = create_app()
    client = app.test_client()
    with client.session_transaction() as s:
        s["uid"] = pilot
    monkeypatch.setattr(audible, "by_asin", lambda a: {"asin": a, "title": "The Pilot", "author": "Will Wight"})
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    monkeypatch.setattr(audible, "search", lambda *a, **k: [
        {"asin": "B0F8CJQZM3", "title": "The Pilot", "author": "Will Wight", "language": "English"}])
    html = client.get("/book/B0F8CJQZM3").get_data(as_text=True)
    assert 'corner-badge downloading' in html and ">Downloading<" in html
    assert "Requested</span>" in html                               # the eBook line, by format
    card = client.get("/api/search?q=pilot").get_json()[0]
    assert card["state"] == "downloading" and card["status"]["label"] == "Downloading"
