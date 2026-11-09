"""Stuck requests say what the download needs (releasecheck.py + needs.py).
Live, 2026-10-12: DCC #7's audiobook, #7/#8's ebooks and The Last Horizon's
ebooks only had VIP-only releases on the tracker — Stackarr said "Requested"
and, a week later, "Not found yet"."""
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
GOOD = {"releases": [{"title": "Matt Dinniman - This Inevitable Ruin", "rejected": False, "rejections": []}]}


def test_classify():
    assert releasecheck.classify(VIP)["kind"] == "needs"
    assert [n["key"] for n in releasecheck.classify(VIP["releases"])["needs"]] == ["vip"]   # a bare list too
    none = releasecheck.classify({"releases": []}, "ebook")
    assert none["kind"] == "none" and none["needs"][0]["label"] == "No ebook release"
    rej = releasecheck.classify([{"rejected": True, "rejections": ["Wrong language"]},
                                 {"rejected": True, "rejections": ["Not enough seeders: 0"]},
                                 {"rejected": True, "rejections": ["Ratio too low"]}])
    assert [n["key"] for n in rej["needs"]] == ["ratio", "no_seeders", "profile"]
    assert releasecheck.classify([{"rejected": False, "rejections": []}, VIP["releases"][0]])["kind"] == "ok"
    bare = releasecheck.classify([{"rejected": True}])
    assert bare["kind"] == "needs" and bare["needs"][0]["text"] == "Every release was rejected"


def test_older_stored_answers_still_read(tmp_db):
    db.set_meta("relcheck:5", json.dumps({"kind": "vip", "detail": "Check upstream", "at": 1}))
    db.set_meta("relcheck:6", json.dumps({"kind": "rejected", "detail": "Every release was rejected: Wrong language", "at": 1}))
    db.set_meta("relcheck:7", json.dumps({"kind": "vip_retry", "detail": "", "at": 1}))
    assert [n["key"] for n in releasecheck.result(5)["needs"]] == ["vip"]
    assert [n["key"] for n in releasecheck.result(6)["needs"]] == ["profile"]
    assert releasecheck.result(7)["kind"] == "retry"


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
    fake_chaptarr.answer = VIP
    fake_chaptarr.indexers = [{"id": 7, "name": "Tracker", "implementation": "Torznab", "enable": True,
                               "fields": [{"name": "isVip", "value": False}]}]
    fake_chaptarr.indexerstatus = []

    def get(url, params=None, **kw):
        if url.endswith("/api/v1/release"):
            fake_chaptarr.releases.append(params)
            return Resp(200, fake_chaptarr.answer)
        if url.endswith("/api/v1/queue"):
            return Resp(200, {"records": []})
        if url.endswith("/api/v1/indexer"):
            return Resp(200, fake_chaptarr.indexers)
        if url.endswith("/api/v1/downloadclient"):
            return Resp(200, [{"id": 1, "name": "qBittorrent", "enable": True, "fields": []}])
        if url.endswith("/api/v1/indexerstatus"):
            return Resp(200, fake_chaptarr.indexerstatus)
        return fake_chaptarr.get(url, params=params, **kw)
    monkeypatch.setattr(chaptarr.requests, "get", get)
    return fake_chaptarr


def test_only_settled_unchecked_requests_are_candidates(chap):
    fresh, old = _req(1, 5), _req(1, 40, title="A Parade of Horribles")
    assert [r["id"] for r in releasecheck.candidates(5)] == [old]
    db.set_meta(f"relcheck:{old}", json.dumps({"kind": "needs", "needs": [{"key": "vip"}], "at": int(time.time())}))
    assert releasecheck.candidates(5) == []            # checked today
    assert fresh


def test_an_indexer_outage_is_looked_at_again_within_the_hour(chap):
    rid = _req(1, 120)
    db.set_meta(f"relcheck:{rid}", json.dumps({"kind": "needs", "needs": [{"key": "indexer_down"}],
                                               "at": int(time.time()) - 3700}))
    assert [r["id"] for r in releasecheck.candidates(5)] == [rid]


def test_one_check_says_what_it_needs_everywhere(chap, admin_client):
    db.set_meta("formats", "audiobook")
    rid = _req(admin_client.uid, 40)
    assert releasecheck.run_once() == 1
    assert chap.releases == [{"bookId": 8802}]
    st = status.for_book("", "This Inevitable Ruin", "Matt Dinniman", viewer=admin_client.uid)
    assert st["state"] == "needs" and st["label"] == "Needs VIP" and st["cls"] == "needs"
    assert [n["key"] for n in st["needs"]] == ["vip"]
    html = admin_client.get("/requests").get_data(as_text=True)
    assert "What this download needs" in html and "Needs VIP" in html and "VIP or higher-rank members" in html
    assert "Needs something" in html
    live = admin_client.get("/api/requests/status").get_json()
    assert live[str(rid)]["formats"]["audiobook"]["needs"][0]["key"] == "vip"
    assert releasecheck.run_once() == 0                # not again today
    assert len(chap.releases) == 1


def test_no_ebook_release_and_a_failing_indexer(chap):
    rid = _req(1, 40, fmt="ebook")
    chap.books[59].append({"id": 8803, "authorId": 59, "title": "This Inevitable Ruin", "mediaType": "ebook",
                           "monitored": True, "statistics": {"bookFileCount": 0}})
    chap.answer = {"releases": []}
    chap.indexerstatus = [{"indexerId": 7, "disabledTill": "2999-01-01T00:00:00Z"}]
    out = releasecheck.check(dict(releasecheck.candidates(1)[0]))
    assert out["kind"] == "none" and [n["key"] for n in out["needs"]] == ["indexer_down", "no_release"]
    assert out["needs"][1]["label"] == "No ebook release" and rid


def test_an_unreachable_chaptarr_is_an_error_not_a_need(chap, monkeypatch):
    rid = _req(1, 40)
    def down(url, **kw):
        raise chaptarr.requests.ConnectionError("refused")
    monkeypatch.setattr(chaptarr.requests, "get", down)
    out = releasecheck.check({"id": rid, "title": "This Inevitable Ruin", "author": "Matt Dinniman",
                              "format": "audiobook", "chaptarr_ref": "59"})
    assert out["kind"] == "error" and out["needs"] == []
    assert releasecheck.held_ids() == []


def test_when_the_need_clears_it_searches_again_once(chap, admin_client):
    rid = _req(admin_client.uid, 40)
    releasecheck.run_once()
    assert releasecheck.result(rid)["kind"] == "needs"
    chap.answer = GOOD                                  # VIP switched on upstream: the release is usable now
    db.set_meta(f"relcheck:{rid}", json.dumps(dict(releasecheck.result(rid), at=0)))
    releasecheck.run_once()
    searched = [c[3]["bookIds"] for c in chap.calls_to("POST", "/api/v1/command")]
    assert searched == [[8802]]
    st = status.for_book("", "This Inevitable Ruin", "Matt Dinniman", viewer=admin_client.uid)
    assert st["state"] == "requested" and "has cleared" in st["detail"] and not st["needs"]
    # looked at again after CHECK_AFTER_MIN; still usable → no second search
    db.set_meta(f"relcheck:{rid}", json.dumps(dict(releasecheck.result(rid), at=0)))
    releasecheck.run_once()
    assert len(chap.calls_to("POST", "/api/v1/command")) == 1


def test_a_settings_change_upstream_looks_again_at_once(chap):
    a, b = _req(1, 40), _req(1, 40, title="A Parade of Horribles")
    now = int(time.time())
    db.set_meta(f"relcheck:{a}", json.dumps({"kind": "needs", "needs": [{"key": "vip", "scope": "account"}], "at": now}))
    db.set_meta(f"relcheck:{b}", json.dumps({"kind": "none", "needs": [{"key": "no_release", "scope": "release"}], "at": now}))
    assert releasecheck.settings_changed(now=1000) == 0          # first look: nothing to compare with
    assert releasecheck.settings_changed(now=1060) is None       # not again within the poll interval
    chap.indexers[0]["fields"][0]["value"] = True                # VIP switched on in Chaptarr
    assert releasecheck.settings_changed(now=1000 + releasecheck.SETTINGS_POLL_MINUTES * 60 + 1) == 1
    assert releasecheck.result(a)["at"] == 0 and releasecheck.result(b)["at"] == now
    assert "isVip" not in db.get_meta("needs_settings", "")      # only a hash is kept
    assert chap.calls_to("PUT", "/api/v1/indexer") == []         # read-only


def test_admin_look_again_button(chap, admin_client):
    rid = _req(admin_client.uid, 40)
    db.set_meta(f"relcheck:{rid}", json.dumps({"kind": "needs", "needs": [{"key": "vip"}], "at": int(time.time())}))
    r = admin_client.post("/api/admin/needs-recheck", headers=SAME_ORIGIN).get_json()
    assert r["ok"] and r["total"] == 1 and releasecheck.result(rid)["at"] == 0


def test_requests_page_filters_to_held_back_books(chap, admin_client):
    db.set_meta("formats", "both")
    rid = _req(admin_client.uid, 40)
    db.set_meta(f"relcheck:{rid}", json.dumps({"kind": "needs", "at": int(time.time()),
                                               "needs": [{"key": "ratio", "label": "Needs ratio", "text": "Low ratio."}]}))
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,title,author,status,format) VALUES (?,?,?,?,?)",
                  (admin_client.uid, "Some Other Book", "Someone", "handed", "audiobook"))
    only = admin_client.get("/requests?needs=1").get_data(as_text=True)
    assert "This Inevitable Ruin" in only and "Some Other Book" not in only
    assert "Needs ratio" in only and "Look again now" in only


def test_pages_never_trigger_a_release_search(chap, admin_client):
    _req(admin_client.uid, 40)
    admin_client.get("/requests")
    admin_client.get("/api/requests/status")
    assert chap.releases == []


def test_can_be_turned_off(chap, admin_client):
    _req(admin_client.uid, 40)
    r = admin_client.post("/api/settings", json={"release_check": False}, headers=SAME_ORIGIN)
    assert r.get_json()["ok"] and releasecheck.run_once() == 0 and chap.releases == []
