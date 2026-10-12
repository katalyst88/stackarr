"""Needs VIP: a state a reader understands, a place to see them, and a
re-search when VIP is switched on in Chaptarr (which Stackarr only reads)."""
import json
import time

import pytest

from fakes import SAME_ORIGIN, Resp
from stackarr import chaptarr, db, status, vip


def _indexers(is_vip):
    return [{"id": 1, "name": "Torznab", "implementation": "Torznab", "fields": []},
            {"id": 7, "name": "Tracker", "implementation": "Torznab",
             "fields": [{"name": "isVip", "value": is_vip}, {"name": "userClass", "value": "Power User"}]}]


@pytest.fixture
def chap(fake_chaptarr, monkeypatch):
    fake_chaptarr.authors = [{"id": 59, "authorName": "Matt Dinniman"}]
    fake_chaptarr.books[59] = [
        {"id": 8802, "authorId": 59, "title": "This Inevitable Ruin", "mediaType": "audiobook", "monitored": True,
         "asin": "B0DK22WQFV", "statistics": {"bookFileCount": 0}},
        {"id": 8867, "authorId": 59, "title": "A Parade of Horribles", "mediaType": "ebook", "monitored": True,
         "statistics": {"bookFileCount": 0}}]
    fake_chaptarr.vip = False
    writes = []

    def get(url, params=None, **kw):
        if url.endswith("/api/v1/indexer"):
            return Resp(200, _indexers(fake_chaptarr.vip))
        if url.endswith("/api/v1/queue"):
            return Resp(200, {"records": []})
        return fake_chaptarr.get(url, params=params, **kw)

    def put(url, **kw):
        if "/indexer" in url:
            writes.append(url)
        return fake_chaptarr.put(url, **kw)
    monkeypatch.setattr(chaptarr.requests, "get", get)
    monkeypatch.setattr(chaptarr.requests, "put", put)
    fake_chaptarr.indexer_writes = writes
    return fake_chaptarr


def _vip_request(uid, title, fmt):
    with db.conn() as c:
        rid = c.execute("INSERT INTO requests (user_id,title,author,status,format,chaptarr_ref) VALUES (?,?,?,?,?,?)",
                        (uid, title, "Matt Dinniman", "handed", fmt, "59")).lastrowid
    db.set_meta(f"relcheck:{rid}", json.dumps({"kind": "vip", "detail": "", "at": int(time.time())}))
    return rid


def test_reads_indexer_vip_from_chaptarr_and_never_writes_it(chap):
    got = vip.poll(now=1000)
    assert got["vip"] is False and got["user_class"] == "Power User"
    assert vip.state()["vip"] is False and chap.indexer_writes == []
    assert vip.poll(now=1000 + 60) is None                 # not again within POLL_MINUTES


def test_vip_turning_on_re_searches_the_vip_only_books(chap, admin_client):
    a = _vip_request(admin_client.uid, "This Inevitable Ruin", "audiobook")
    b = _vip_request(admin_client.uid, "A Parade of Horribles", "ebook")
    vip.poll(now=1000)
    chap.vip = True
    vip.poll(now=1000 + vip.POLL_MINUTES * 60 + 1)
    assert vip.progress()["total"] == 2
    assert vip.reflow_step(limit=1) == 1                   # trickled
    assert vip.reflow_step() == 1
    searched = [c[3]["bookIds"] for c in chap.calls_to("POST", "/api/v1/command")]
    assert searched == [[8802], [8867]]
    st = status.for_book("", "This Inevitable Ruin", "Matt Dinniman", viewer=admin_client.uid)
    assert st["state"] == "requested" and "VIP is on" in st["detail"]
    assert a and b and chap.indexer_writes == []


def test_requests_page_has_a_waiting_for_vip_section(chap, admin_client):
    db.set_meta("formats", "both")
    _vip_request(admin_client.uid, "This Inevitable Ruin", "audiobook")
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,title,author,status,format) VALUES (?,?,?,?,?)",
                  (admin_client.uid, "Some Other Book", "Someone", "handed", "audiobook"))
    html = admin_client.get("/requests").get_data(as_text=True)
    assert "Waiting for VIP" in html and "Needs VIP" in html
    assert "/help/vip" not in html
    only = admin_client.get("/requests?vip=1").get_data(as_text=True)
    assert "This Inevitable Ruin" in only and "Some Other Book" not in only
    assert "Needs VIP — check upstream" in only and "Search these again" in only
    assert admin_client.get("/help/vip").status_code == 404


def test_admin_recheck_button(chap, admin_client):
    _vip_request(admin_client.uid, "This Inevitable Ruin", "audiobook")
    r = admin_client.post("/api/admin/vip-recheck", headers=SAME_ORIGIN).get_json()
    assert r["ok"] and r["total"] == 1 and vip.progress()["todo"]


def test_settings_says_where_the_switch_is(chap, admin_client):
    vip.poll(now=1000)
    html = admin_client.get("/settings").get_data(as_text=True)
    assert "Upstream VIP: <b>off</b>" in html
