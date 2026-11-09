"""A cover is either real or Stackarr's "Fetching cover…" placeholder, and the
real one follows. /coverart never waits on a lookup: an unknown book is
queued for the paced background worker and answered with cover-wip.svg
(not cached, so the page's re-ask reaches the server); the worker retries
with backoff when the catalogue errors instead of caching a miss."""
import os

from stackarr import audible, db, scheduler

STATIC = os.path.join(os.path.dirname(__file__), "..", "stackarr", "static")


def test_unknown_cover_is_the_wip_placeholder(admin_client, monkeypatch):
    queued = []
    monkeypatch.setattr(scheduler, "queue_cover", lambda *a: queued.append(a) or True)
    r = admin_client.get("/coverart?title=Some+Book&author=Some+Author")
    assert r.status_code == 302 and r.headers["Location"].endswith("/static/cover-wip.svg")
    assert r.headers["Cache-Control"] == "no-store" and queued
    assert os.path.exists(os.path.join(STATIC, "cover-wip.svg"))


def test_resolved_cover_replaces_it(admin_client, monkeypatch):
    db.set_meta("cart:" + db.rating_key("", "Some Book", "Some Author"), "https://m.media-amazon.com/images/I/x.jpg")
    r = admin_client.get("/coverart?title=Some+Book&author=Some+Author&_r=1")
    assert r.headers["Location"] == "https://m.media-amazon.com/images/I/x.jpg"


def test_a_catalogue_error_is_retried_not_cached_as_a_miss(tmp_db, monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("429-ish")
    monkeypatch.setattr(audible.requests, "get", boom)
    scheduler._cover_retry.clear()
    key = db.rating_key("", "Some Book", "Some Author")
    scheduler._resolve_one("", "Some Book", "Some Author", "", key)
    assert db.get_meta("cart:" + key, "") == ""                    # no "none" written
    assert len(scheduler._cover_retry) == 1 and scheduler._cover_retry[0][2] == 1
    # the last attempt gives up and records the miss (for a day)
    scheduler._resolve_one("", "Some Book", "Some Author", "", key, attempt=len(scheduler.COVER_RETRY_MINUTES))
    assert db.get_meta("cart:" + key, "") == "none"
    scheduler._cover_retry.clear()


def test_a_genuine_miss_is_cached(tmp_db, monkeypatch):
    from fakes import Resp
    monkeypatch.setattr(audible.requests, "get", lambda *a, **k: Resp(200, {"products": []}))
    key = db.rating_key("", "Nothing", "Nobody")
    scheduler._cover_retry.clear()
    scheduler._resolve_one("", "Nothing", "Nobody", "", key)
    assert db.get_meta("cart:" + key, "") == "none" and not scheduler._cover_retry
