"""Fixes from the round-5 review: stuck requests can be sent again, the
household's rows don't hide yours, admin Remove really removes, covers can't
run script, LIKE takes an author's % and _ literally, background lookups are
bounded, and a cold queue cache is fetched once."""
import datetime
import json
import threading
import time

import pytest

from fakes import SAME_ORIGIN, Resp
from stackarr import chaptarr, covers, db, routes, scheduler, status


def _req(uid, title, st, fmt="audiobook", author="Will Wight", days_ago=0):
    when = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.conn() as c:
        return c.execute("INSERT INTO requests (user_id,title,author,status,format,created_at,updated_at) "
                         "VALUES (?,?,?,?,?,?,?)", (uid, title, author, st, fmt, when, when)).lastrowid


def _user(name, role="user"):
    with db.conn() as c:
        return c.execute("INSERT INTO users (username, role) VALUES (?,?)", (name, role)).lastrowid


@pytest.fixture
def grabs(admin_client, monkeypatch):
    """Chaptarr's add-and-search, recorded; nothing in the queue."""
    db.set_meta("formats", "audiobook")
    sent = []

    def add_and_search(title, author="", asin="", fmt="audiobook", **kw):
        sent.append(title)
        return {"ok": True, "detail": "Sent upstream", "ref": "59"}
    monkeypatch.setattr(chaptarr, "add_and_search", add_and_search)
    monkeypatch.setattr(status, "live_queue", lambda: [])
    return sent


# -- a stuck request can be asked for again ---------------------------------

def test_a_fresh_request_is_not_sent_twice(admin_client, grabs):
    _req(admin_client.uid, "The Captain", "handed", days_ago=1)
    r = admin_client.post("/api/request", json={"title": "The Captain", "author": "Will Wight",
                                                "format": "audiobook"}, headers=SAME_ORIGIN).get_json()
    assert r.get("duplicate") and grabs == []


def test_a_stale_request_can_be_sent_again_and_restarts_the_clock(admin_client, grabs):
    rid = _req(admin_client.uid, "The Captain", "handed", days_ago=status.STALE_DAYS + 1)
    assert status.for_book("", "The Captain", "Will Wight", viewer=admin_client.uid)["state"] == "not_found"
    r = admin_client.post("/api/request", json={"title": "The Captain", "author": "Will Wight",
                                                "format": "audiobook"}, headers=SAME_ORIGIN).get_json()
    assert r["ok"] and not r.get("duplicate") and grabs == ["The Captain"]
    with db.conn() as c:
        rows = c.execute("SELECT id FROM requests").fetchall()
    assert [x["id"] for x in rows] == [rid]                        # same row, updated
    assert status.for_book("", "The Captain", "Will Wight", viewer=admin_client.uid)["state"] == "requested"


def test_a_held_request_shows_retry_and_retry_sends_it(admin_client, grabs):
    rid = _req(admin_client.uid, "The Captain", "handed", days_ago=1)
    assert admin_client.post(f"/api/request/{rid}/retry", headers=SAME_ORIGIN).status_code == 400
    db.set_meta(f"relcheck:{rid}", json.dumps({"kind": "needs", "needs": [{"key": "vip", "label": "Needs VIP"}],
                                               "at": int(time.time())}))
    html = admin_client.get("/requests").get_data(as_text=True)
    assert f"Stackarr.retry({rid})" in html
    r = admin_client.post(f"/api/request/{rid}/retry", headers=SAME_ORIGIN).get_json()
    assert r["ok"] and grabs == ["The Captain"]
    assert db.get_meta(f"relcheck:{rid}", "") == ""               # a new attempt, a clean slate


def test_a_stale_request_still_downloading_is_not_stuck(admin_client, grabs, monkeypatch):
    rid = _req(admin_client.uid, "The Captain", "handed", days_ago=status.STALE_DAYS + 1)
    monkeypatch.setattr(status, "live_queue", lambda: [{"title": "The Captain", "author": "Will Wight",
                                                        "media": "audiobook", "state": "downloading"}])
    with db.conn() as c:
        row = dict(c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())
    assert not status.stuck(row)


def test_retry_all_takes_stuck_requests_too(admin_client, grabs):
    _req(admin_client.uid, "The Captain", "failed")
    _req(admin_client.uid, "The Engineer", "handed", days_ago=status.STALE_DAYS + 1)
    _req(admin_client.uid, "The Knight", "handed", days_ago=1)
    r = admin_client.post("/api/requests/retry-all", headers=SAME_ORIGIN).get_json()
    assert r["retried"] == 2 and sorted(grabs) == ["The Captain", "The Engineer"]


# -- the household's rows ---------------------------------------------------

def test_someone_elses_newer_row_does_not_hide_yours(tmp_db, monkeypatch):
    monkeypatch.setattr(status, "live_queue", lambda: [])
    db.set_meta("formats", "audiobook")
    me, them = _user("me"), _user("them")
    _req(me, "The Captain", "handed", days_ago=1)
    _req(them, "The Captain", "pending_approval")
    assert status.for_book("", "The Captain", "Will Wight", viewer=me)["state"] == "requested"
    # and the other way round: their active request beats my unapproved one
    _req(me, "The Knight", "pending_approval")
    _req(them, "The Knight", "handed", days_ago=1)
    assert status.for_book("", "The Knight", "Will Wight", viewer=me)["state"] == "requested"
    # with only my own rows, my latest attempt is still the truth
    _req(me, "The Engineer", "handed", days_ago=2)
    _req(me, "The Engineer", "pending_approval")
    assert status.for_book("", "The Engineer", "Will Wight", viewer=me)["state"] == "pending"


# -- Remove -------------------------------------------------------------------

def test_admin_remove_removes_anyones_row(admin_client):
    other = _user("bob")
    rid = _req(other, "The Captain", "failed")
    db.set_meta(f"relcheck:{rid}", "{}")
    assert admin_client.delete(f"/api/request/{rid}", headers=SAME_ORIGIN).get_json()["ok"]
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM requests").fetchone()["n"] == 0
    assert db.get_meta(f"relcheck:{rid}", "") == ""


def test_a_user_cannot_remove_someone_elses_row_and_is_told(admin_client):
    from stackarr import create_app
    bob, carol = _user("bob"), _user("carol")
    rid = _req(carol, "The Captain", "failed")
    client = create_app().test_client()
    with client.session_transaction() as sess:
        sess["uid"] = bob
    r = client.delete(f"/api/request/{rid}", headers=SAME_ORIGIN)
    assert r.status_code == 404 and r.get_json()["ok"] is False
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM requests").fetchone()["n"] == 1


# -- LIKE ---------------------------------------------------------------------

def test_like_takes_percent_and_underscore_literally(tmp_db):
    assert db.contains("a_b%") == "%a\\_b\\%%"
    db.set_meta("formats", "audiobook")
    _req(1, "Odd Book", "handed", author="AxB Smith", days_ago=1)
    with db.conn() as c:
        assert status._rows_for(c, "A_B Smith", None) == []
        assert len(status._rows_for(c, "AxB Smith", None)) == 1


# -- covers -------------------------------------------------------------------

@pytest.fixture
def abs_cover(admin_client, monkeypatch):
    import requests
    from stackarr import absclient
    got = {}
    monkeypatch.setattr(absclient, "abs_url", lambda: "http://abs")
    monkeypatch.setattr(absclient, "admin_token", lambda: "t")
    monkeypatch.setattr(requests, "get", lambda url, **kw: got["resp"])
    return got


class _Raw:
    def __init__(self, body, ctype):
        self.ok, self.status_code, self.content, self.headers = True, 200, body, {"Content-Type": ctype}


def test_an_svg_cover_is_sandboxed(admin_client, abs_cover):
    abs_cover["resp"] = _Raw(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                             "image/svg+xml")
    for _ in range(2):                                             # fetched, then from the disk cache
        r = admin_client.get("/cover/li_svg")
        assert r.status_code == 200 and r.mimetype == "image/svg+xml"
        csp = r.headers["Content-Security-Policy"]
        assert "sandbox" in csp and "default-src 'none'" in csp and "frame-ancestors" in csp
        assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_a_cover_that_is_not_an_image_is_never_served(admin_client, abs_cover):
    abs_cover["resp"] = _Raw(b"<html><script>alert(1)</script></html>", "text/html")
    r = admin_client.get("/cover/li_html")
    assert r.status_code == 302 and "cover-placeholder.svg" in r.headers["Location"]


def test_a_jpeg_cover_is_served_as_is(admin_client, abs_cover):
    abs_cover["resp"] = _Raw(b"\xff\xd8\xff", "image/jpeg")
    r = admin_client.get("/cover/li_jpg")
    assert r.status_code == 200 and r.data == b"\xff\xd8\xff"
    assert "sandbox" not in r.headers["Content-Security-Policy"]


def test_an_empty_print_cover_search_is_a_short_miss(tmp_db, monkeypatch):
    from stackarr import ebookmeta
    monkeypatch.setattr(ebookmeta, "search", lambda q, num=5: [])
    covers.resolve("Unsouled", "Will Wight")
    assert covers.clean_cover("Unsouled", "Will Wight") == ""
    v = json.loads(db.get_meta(covers._key("Unsouled", "Will Wight")))
    db.set_meta(covers._key("Unsouled", "Will Wight"),
                json.dumps(dict(v, at=int(time.time()) - covers.SOFT_MISS_HOURS * 3600 - 1)))
    assert covers.clean_cover("Unsouled", "Will Wight") is None   # asked again
    # a search that answered but had no cover: a miss for a week
    monkeypatch.setattr(ebookmeta, "search", lambda q, num=5: [{"title": "Other", "author": "Someone"}])
    covers.resolve("Soulsmith", "Will Wight")
    v = json.loads(db.get_meta(covers._key("Soulsmith", "Will Wight")))
    assert "soft" not in v


# -- background lookups and the queue cache ----------------------------------

def test_background_lookups_requeue_after_an_hour_and_stay_bounded(monkeypatch):
    monkeypatch.setattr(scheduler, "_job_seen", {})
    monkeypatch.setattr(scheduler, "_job_q", scheduler.queue.Queue(maxsize=100))
    assert scheduler.queue_job("bookid", "A", "B", "audiobook")
    assert not scheduler.queue_job("bookid", "a", "b", "audiobook")
    key = next(iter(scheduler._job_seen))
    scheduler._job_seen[key] -= scheduler.JOB_REQUEUE_SECONDS + 1
    assert scheduler.queue_job("bookid", "A", "B", "audiobook")
    monkeypatch.setattr(scheduler, "_JOB_SEEN_MAX", 5)
    for i in range(20):
        scheduler._job_q = scheduler.queue.Queue(maxsize=100)
        scheduler.queue_job("pcover", f"t{i}", "a")
    assert len(scheduler._job_seen) <= 5


def test_a_cold_queue_cache_is_fetched_once(tmp_db, monkeypatch):
    calls = []

    def slow_queue():
        calls.append(1)
        time.sleep(0.2)
        return [{"title": "x"}]
    monkeypatch.setattr(chaptarr, "configured", lambda: True)
    monkeypatch.setattr(chaptarr, "queue", slow_queue)
    status.reset_cache()
    out = []
    ts = [threading.Thread(target=lambda: out.append(status.live_queue())) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1 and all(o == [{"title": "x"}] for o in out)
