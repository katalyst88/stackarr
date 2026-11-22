"""One entry per book on Requests, and one row per (user, book, format) in the
database. The field case: The Pilot showed four times — a failed audiobook
and ebook attempt, then the series request that worked, for both formats."""
import re

import pytest

from fakes import SAME_ORIGIN
from stackarr import absclient, chaptarr, db, routes


def _row(uid, st, fmt, title="The Pilot", asin="B0F8CJQZM3", detail=""):
    with db.conn() as c:
        return c.execute("INSERT INTO requests (user_id,asin,title,author,status,format,detail) VALUES (?,?,?,?,?,?,?)",
                         (uid, asin, title, "Will Wight", st, fmt, detail)).lastrowid


@pytest.fixture
def pilot(admin_client):
    db.set_meta("formats", "both")
    uid = admin_client.uid
    ids = [_row(uid, "failed", "audiobook", detail="Chaptarr couldn't find “Will Wight” in its catalogue."),
           _row(uid, "failed", "ebook", detail="Chaptarr couldn't find “Will Wight” in its catalogue."),
           _row(uid, "handed", "audiobook"), _row(uid, "handed", "ebook")]
    return ids


def test_requests_page_shows_the_pilot_once(admin_client, pilot):
    html = admin_client.get("/requests").get_data(as_text=True)
    assert html.count('class="req-row"') == 1
    row = html.split('class="req-row"', 1)[1]
    assert "Earlier attempts (2)" in row
    pills = re.findall(r'class="badge badge-(\w+)"', row.split("req-history")[0])
    assert pills == ["handed", "handed"]                     # audiobook + eBook, both Requested
    assert ">Retry<" not in row                              # the failures were superseded
    # Remove takes every attempt of the book
    assert ",".join(str(i) for i in sorted(pilot, reverse=True)) in row


def test_wanted_lists_only_books_whose_current_try_failed(admin_client, pilot):
    _row(admin_client.uid, "failed", "audiobook", title="Giovanni's Room", asin="")
    html = admin_client.get("/requests?wanted=1").get_data(as_text=True)
    assert html.count('class="req-row"') == 1 and "Giovanni" in html and "The Pilot" not in html


def test_recent_requests_lane_has_one_card_per_book(admin_client, pilot, monkeypatch):
    monkeypatch.setattr(absclient, "recent_added", lambda n=14: [])
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: [])
    with db.conn() as c:     # a lane to render the page's content block
        c.execute("INSERT INTO suggestions (user_id,asin,title,author,lane,status,score) VALUES (?,?,?,?,?,?,?)",
                  (admin_client.uid, "B0XXXXXXXX", "Some Pick", "Someone", "author", "pending", 1))
    html = admin_client.get("/suggestions").get_data(as_text=True)
    lane = html.split("Recent requests", 1)[1].split("</section>", 1)[0]
    assert lane.count('class="media-card"') == 1 and "Failed" not in lane


def test_a_new_attempt_updates_the_failed_row(tmp_db):
    with db.conn() as c:
        rid = c.execute("INSERT INTO requests (user_id,asin,title,author,status,format) "
                        "VALUES (1,'B0F8CJQZM3','The Pilot','Will Wight','failed','audiobook')").lastrowid
        again = db.record_request(c, 1, {"asin": "B0F8CJQZM3", "title": "The Pilot", "author": "Will Wight"},
                                  "audiobook", "handed", "Sent", "ref", "series_book")
        other = db.record_request(c, 1, {"asin": "B0F8CJQZM3", "title": "The Pilot", "author": "Will Wight"},
                                  "ebook", "handed", "Sent", "ref", "series_book")
        rows = [dict(r) for r in c.execute("SELECT id, status, format FROM requests ORDER BY id")]
    assert again == rid and other != rid
    assert rows == [{"id": rid, "status": "handed", "format": "audiobook"},
                    {"id": other, "status": "handed", "format": "ebook"}]


def test_asking_again_while_open_is_not_a_second_search(admin_client, monkeypatch):
    db.set_meta("formats", "audiobook")
    calls = []
    monkeypatch.setattr(chaptarr, "add_and_search", lambda *a, **k: calls.append(a) or {"ok": True, "detail": "Sent"})
    body = {"asin": "B0F8CJQZM3", "title": "The Pilot", "author": "Will Wight"}
    r1 = admin_client.post("/api/request", json=body, headers=SAME_ORIGIN).get_json()
    r2 = admin_client.post("/api/request", json=body, headers=SAME_ORIGIN).get_json()
    assert r1["ok"] and r2["ok"] and r2.get("duplicate")
    assert len(calls) == 1
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_retry_updates_in_place(admin_client, monkeypatch):
    db.set_meta("formats", "audiobook")
    monkeypatch.setattr(chaptarr, "add_and_search", lambda *a, **k: {"ok": True, "detail": "Sent"})
    rid = _row(admin_client.uid, "failed", "audiobook")
    admin_client.post(f"/api/request/{rid}/retry", headers=SAME_ORIGIN)
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT id, status FROM requests")]
    assert rows == [{"id": rid, "status": "handed"}]


def test_existing_duplicate_rows_are_grouped_not_deleted(tmp_db):
    """The live database already holds duplicates: the page groups them; no
    migration rewrites history."""
    with db.conn() as c:
        for st, f in (("failed", "audiobook"), ("failed", "ebook"), ("handed", "audiobook"), ("handed", "ebook")):
            c.execute("INSERT INTO requests (user_id,asin,title,author,status,format) VALUES (1,'B0F8CJQZM3',"
                      "'The Pilot','Will Wight',?,?)", (st, f))
        rows = [dict(r) for r in c.execute("SELECT * FROM requests")]
    db.set_meta("formats", "both")
    groups = routes.group_requests(rows)
    assert len(groups) == 1 and len(groups[0]["history"]) == 2 and groups[0]["retry_id"] is None
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 4
