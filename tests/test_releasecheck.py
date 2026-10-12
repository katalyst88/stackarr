"""Stuck requests get a reason (releasecheck.py). Live, 2026-10-12: DCC #7's
audiobook, #7/#8's ebooks and The Last Horizon's ebooks only have
VIP-only releases — Stackarr said "Requested" and, a week later, "Not found
yet"."""
import datetime
import json
import time

import pytest

from fakes import SAME_ORIGIN, Resp
from stackarr import chaptarr, db, releasecheck, status

VIP = {"releases": [
    {"title": "Matt Dinniman - This Inevitable Ruin", "rejected": True,
     "rejections": ["VIP-only torrent requires VIP membership"]},
    {"title": "This Inevitable Ruin [ebook]", "rejected": True,
     "rejections": [{"reason": "VIP-only torrent requires VIP membership"}]}]}


def test_classify():
    assert releasecheck.classify(VIP)["kind"] == "vip"
    assert releasecheck.classify(VIP["releases"])["kind"] == "vip"           # a bare list too
    assert releasecheck.classify({"releases": []}) == {"kind": "none", "detail": "No releases found on your indexers"}
    rej = releasecheck.classify([{"rejected": True, "rejections": ["Wrong language"]},
                                 {"rejected": True, "rejections": ["Below minimum size"]}])
    assert rej["kind"] == "rejected" and "Wrong language; Below minimum size" in rej["detail"]
    assert releasecheck.classify([{"rejected": False, "rejections": []}, VIP["releases"][0]])["kind"] == "ok"


def _req(uid, minutes_ago, title="This Inevitable Ruin", fmt="audiobook"):
    when = (datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.conn() as c:
        return c.execute("INSERT INTO requests (user_id,title,author,status,format,chaptarr_ref,created_at,updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", (uid, title, "Matt Dinniman", "handed", fmt, "59", when, when)).lastrowid


@pytest.fixture
def chap(fake_chaptarr, monkeypatch):
    fake_chaptarr.authors = [{"id": 59, "authorName": "Matt Dinniman"}]
    fake_chaptarr.books[59] = [
        {"id": 8802, "authorId": 59, "title": "This Inevitable Ruin", "mediaType": "audiobook", "monitored": True,
         "asin": "B0DK22WQFV", "statistics": {"bookFileCount": 0}}]
    fake_chaptarr.releases = []

    def get(url, params=None, **kw):
        if url.endswith("/api/v1/release"):
            fake_chaptarr.releases.append(params)
            return Resp(200, VIP)
        if url.endswith("/api/v1/queue"):
            return Resp(200, {"records": []})
        return fake_chaptarr.get(url, params=params, **kw)
    monkeypatch.setattr(chaptarr.requests, "get", get)
    return fake_chaptarr


def test_only_settled_unchecked_requests_are_candidates(chap):
    fresh, old = _req(1, 5), _req(1, 40, title="A Parade of Horribles")
    assert [r["id"] for r in releasecheck.candidates(5)] == [old]
    db.set_meta(f"relcheck:{old}", json.dumps({"kind": "vip", "at": int(time.time())}))
    assert releasecheck.candidates(5) == []            # checked today
    assert fresh


def test_one_check_names_the_reason_everywhere(chap, admin_client):
    db.set_meta("formats", "audiobook")
    rid = _req(admin_client.uid, 40)
    assert releasecheck.run_once() == 1
    assert chap.releases == [{"bookId": 8802}]
    st = status.for_book("", "This Inevitable Ruin", "Matt Dinniman", viewer=admin_client.uid)
    assert st["state"] == "needs_vip" and st["label"] == "Needs VIP"
    assert "check Chaptarr / indexer" in st["detail"]
    html = admin_client.get("/requests").get_data(as_text=True)
    assert "Needs VIP" in html and "VIP-only" in html
    assert releasecheck.run_once() == 0                # not again today
    assert len(chap.releases) == 1 and rid


def test_pages_never_trigger_a_release_search(chap, admin_client):
    _req(admin_client.uid, 40)
    admin_client.get("/requests")
    admin_client.get("/api/requests/status")
    assert chap.releases == []


def test_can_be_turned_off(chap, admin_client):
    _req(admin_client.uid, 40)
    r = admin_client.post("/api/settings", json={"release_check": False}, headers=SAME_ORIGIN)
    assert r.get_json()["ok"] and releasecheck.run_once() == 0 and chap.releases == []
