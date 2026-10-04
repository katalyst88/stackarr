"""Covers must never cost the page a worker thread.

These pin the 2026-09-21 fault: /home emitted ~59 <img> tags pointing at
/coverart, each of which did an external metadata lookup on the request thread.
Misses were never cached so every render repeated all of them, Google Books'
keyless daily quota was exhausted, and with 8 worker threads the whole app
starved — the page crawled, not just the pictures.
"""
import os
import sys
import time
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def app(tmp_db):
    """The app against a throwaway database (conftest's `tmp_db`).

    This used to set STACKARR_DATA itself and then PURGE every `stackarr.*`
    entry from sys.modules so the package re-read it on import. That worked
    while this was the only test file, but it swaps the module objects out
    from under the shared fixtures in conftest.py — which hold references to
    the originals — so any test running afterwards got a client whose session
    resolved against a different `db` module and came back 401. conftest now
    points STACKARR_DATA at a temp dir before the package is imported at all,
    and `tmp_db` gives each test its own migrated DB, so neither the env
    juggling nor the purge is needed."""
    # The cover index is a module global with a 300s TTL, and the purge used to
    # reset it incidentally. Each test gets a fresh DB, so the index built from
    # the previous one must go with it or a cover resolves against rows that no
    # longer exist. Production does this on every library refresh.
    tmp_db.invalidate_cover_index()
    from stackarr import create_app
    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app):
    from stackarr import db
    u = db.create_local_user("tester", "pw", role="admin")
    c = app.test_client()
    with c.session_transaction() as s:
        s["uid"] = u["id"]
    return c


def _add_library_row(item_id, title, author, cover, source="calibreweb"):
    from stackarr import db
    with db.conn() as c:
        c.execute(
            "INSERT INTO library (item_id,library_id,title,author,asin,format,source,cover,last_seen) "
            "VALUES (?,?,?,?,'','ebook',?,?,datetime('now'))",
            (item_id, "lib", title, author, source, cover))


# --------------------------------------------------------------- the schema
def test_library_stores_a_cover_reference(app):
    from stackarr import db
    with db.conn() as c:
        cols = [r[1] for r in c.execute("pragma table_info(library)")]
    assert "cover" in cols, "library must be able to remember a local cover"


# ------------------------------------------- Calibre-Web hands us the cover
def test_calibreweb_keeps_the_cover_link_opds_already_sends():
    from stackarr.backends.calibreweb import _cover_ref
    entry = ET.fromstring(
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        '<id>urn:uuid:abc</id><title>Abaddon\'s Gate</title>'
        '<link rel="http://opds-spec.org/image" type="image/jpeg" href="/opds/cover/161"/>'
        '<link rel="http://opds-spec.org/image/thumbnail" type="image/jpeg" href="/opds/cover/161"/>'
        '<link rel="http://opds-spec.org/acquisition" href="/opds/download/161/epub/"/>'
        "</entry>")
    assert _cover_ref(entry) == "cw:161"


def test_calibreweb_entry_with_no_image_yields_no_cover():
    from stackarr.backends.calibreweb import _cover_ref
    entry = ET.fromstring(
        '<entry xmlns="http://www.w3.org/2005/Atom"><id>urn:uuid:abc</id>'
        '<title>No Art</title>'
        '<link rel="http://opds-spec.org/acquisition" href="/opds/download/9/epub/"/></entry>')
    assert _cover_ref(entry) == ""


def test_calibreweb_ignores_a_non_numeric_cover_href():
    """A href we can't reduce to a book id must not become a bogus ref."""
    from stackarr.backends.calibreweb import _cover_ref
    entry = ET.fromstring(
        '<entry xmlns="http://www.w3.org/2005/Atom"><id>urn:uuid:abc</id><title>T</title>'
        '<link rel="http://opds-spec.org/image" href="/static/generic.png"/></entry>')
    assert _cover_ref(entry) == ""


# ------------------------------------------------- owned books resolve local
def test_owned_book_resolves_to_a_local_cover(app):
    from stackarr import db
    _add_library_row("calibreweb:urn:uuid:1", "Abaddon's Gate", "James S. A. Corey", "cw:161")
    assert db.library_cover("Abaddon's Gate", "James S. A. Corey") == "cw:161"


def test_owned_book_matches_despite_punctuation_differences(app):
    from stackarr import db
    _add_library_row("calibreweb:urn:uuid:2", "Abaddon's Gate", "James S. A. Corey", "cw:161")
    assert db.library_cover("Abaddon’s Gate!", "James S. A. Corey") == "cw:161"


def test_kavita_series_matches_on_title_alone(app):
    """Kavita stores a series with no author; a title-only match must still win,
    otherwise every Kavita ebook falls through to an external lookup."""
    from stackarr import db
    _add_library_row("kavita:127", "Children of Time", "", "kv:127", source="kavita")
    assert db.library_cover("Children of Time", "Adrian Tchaikovsky") == "kv:127"


def test_unowned_book_has_no_local_cover(app):
    from stackarr import db
    _add_library_row("calibreweb:urn:uuid:3", "Owned Book", "A", "cw:1")
    assert db.library_cover("Some Book We Do Not Have", "Someone") == ""


def test_a_gone_book_is_not_offered_as_a_cover(app):
    from stackarr import db
    _add_library_row("calibreweb:urn:uuid:4", "Deleted Book", "A", "cw:7")
    with db.conn() as c:
        c.execute("UPDATE library SET gone_at=datetime('now') WHERE item_id=?",
                  ("calibreweb:urn:uuid:4",))
    assert db.library_cover("Deleted Book", "A") == ""


# ------------------------------------------ /coverart never calls out, ever
def test_coverart_makes_no_external_call(client, monkeypatch):
    """The whole point. An <img> request must not reach a third party."""
    import requests
    called = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
        AssertionError("coverart made an outbound HTTP call")))
    r = client.get("/coverart?title=Some+Unowned+Book&author=Nobody&asin=&fmt=ebook")
    assert r.status_code == 302
    assert not called


def test_coverart_answers_immediately_for_an_unknown_book(client):
    t = time.time()
    r = client.get("/coverart?title=Totally+Unknown+Thing&author=X&asin=&fmt=ebook")
    elapsed = time.time() - t
    assert r.status_code == 302
    assert "cover-placeholder" in r.headers["Location"]
    assert elapsed < 0.5, f"coverart took {elapsed:.2f}s; it must never block a page"


def test_coverart_redirects_an_owned_book_to_the_local_cover(client):
    _add_library_row("calibreweb:urn:uuid:9", "Siren Song", "Holly Scott", "cw:42")
    r = client.get("/coverart?title=Siren+Song&author=Holly+Scott&asin=&fmt=ebook")
    assert r.status_code == 302
    assert "/libcover/cw:42" in r.headers["Location"]


def test_coverart_queues_an_unowned_book_for_the_background(client):
    # asserted on the dedupe set, not the queue: the worker thread drains the
    # queue, so queue size is a race while this record is stable
    from stackarr import db, scheduler
    key = db.rating_key("", "An Unowned Title", "Someone")
    assert key not in scheduler._cover_seen
    client.get("/coverart?title=An+Unowned+Title&author=Someone&asin=&fmt=ebook")
    assert key in scheduler._cover_seen


def test_queue_cover_accepts_a_book_once_and_refuses_the_repeat(app):
    from stackarr import scheduler
    assert scheduler.queue_cover("", "Repeated Title", "Someone", "ebook") is True
    for _ in range(4):
        assert scheduler.queue_cover("", "Repeated Title", "Someone", "ebook") is False


def test_queue_cover_refuses_a_book_with_nothing_to_search_on(app):
    from stackarr import scheduler
    assert scheduler.queue_cover("", "", "", "ebook") is False


def test_coverart_uses_a_cover_the_worker_already_resolved(client):
    from stackarr import db
    key = "cart:" + db.rating_key("", "Resolved Book", "Auth")
    db.set_meta(key, "https://example.test/art.jpg")
    r = client.get("/coverart?title=Resolved+Book&author=Auth&asin=&fmt=ebook")
    assert r.headers["Location"] == "https://example.test/art.jpg"


def test_coverart_shows_placeholder_for_a_remembered_miss(client):
    from stackarr import db
    db.set_meta("cart:" + db.rating_key("", "Missing Art", "Auth"), "none")
    r = client.get("/coverart?title=Missing+Art&author=Auth&asin=&fmt=ebook")
    assert "cover-placeholder" in r.headers["Location"]


# ------------------------------------------------- misses expire, not forever
def test_a_remembered_miss_expires_so_the_book_is_retried(app):
    from stackarr import db, scheduler
    key = db.rating_key("", "Late Art", "Auth")
    db.set_meta("cart:" + key, "none")
    db.set_meta("cartx:" + key, str(int(time.time()) - 1))      # already due
    scheduler._expire_cover_misses()
    assert db.get_meta("cart:" + key, "") == "", "an expired miss must be retried"


def test_a_legacy_miss_with_no_expiry_stamp_is_cleared_once(app):
    """Misses cached before this fix have no expiry and would blank those covers
    for good."""
    from stackarr import db, scheduler
    key = db.rating_key("", "Old Miss", "Auth")
    db.set_meta("cart:" + key, "none")          # no cartx: partner, as before
    scheduler._expire_cover_misses()
    assert db.get_meta("cart:" + key, "") == ""


def test_expiry_never_touches_a_real_cached_cover(app):
    from stackarr import db, scheduler
    key = db.rating_key("", "Good Art", "Auth")
    db.set_meta("cart:" + key, "https://example.test/keep.jpg")
    scheduler._expire_cover_misses()
    assert db.get_meta("cart:" + key, "") == "https://example.test/keep.jpg"


def test_a_fresh_miss_is_not_expired_early(app):
    from stackarr import db, scheduler
    key = db.rating_key("", "Recent Miss", "Auth")
    db.set_meta("cartx:" + key, str(int(time.time()) + 3600))   # stamp first, as the worker does
    db.set_meta("cart:" + key, "none")
    scheduler._expire_cover_misses()
    assert db.get_meta("cart:" + key, "") == "none"


# --------------------------------------------- Google Books daily-quota brake
def test_daily_quota_429_opens_the_breaker(app):
    from stackarr import ebookmeta

    class R:
        status_code = 429

        def json(self):
            return {"error": {"message": "Quota exceeded for quota metric 'Queries' "
                                         "and limit 'Queries per day' of service "
                                         "'books.googleapis.com'"}}

    assert ebookmeta._note_gb_quota(R()) is True
    assert ebookmeta.gb_quota_exhausted() is True


def test_a_burst_429_does_not_open_the_breaker(app):
    """A short-term rate limit is worth retrying; only a per-DAY limit is not."""
    from stackarr import ebookmeta

    class R:
        status_code = 429

        def json(self):
            return {"error": {"message": "Rate limit exceeded, please try again shortly"}}

    assert ebookmeta._note_gb_quota(R()) is False
    assert ebookmeta.gb_quota_exhausted() is False


def test_breaker_stops_google_books_being_called(app, monkeypatch):
    from stackarr import ebookmeta
    ebookmeta.db.set_meta(ebookmeta.GB_QUOTA_KEY, str(int(time.time()) + 600))
    monkeypatch.setattr(ebookmeta, "_get", lambda *a, **k: pytest.fail(
        "Google Books was called while the daily-quota breaker was open"))
    assert ebookmeta._gb_get({"q": "anything"}) == []


def test_breaker_reopens_once_it_has_expired(app):
    from stackarr import ebookmeta
    ebookmeta.db.set_meta(ebookmeta.GB_QUOTA_KEY, str(int(time.time()) - 1))
    assert ebookmeta.gb_quota_exhausted() is False


# ------------------------------------------------ the index must stay honest
def test_cover_index_is_rebuilt_after_a_library_refresh(app):
    """A book added since the index was built must resolve, not read as unowned."""
    from stackarr import db
    assert db.library_cover("Late Arrival", "Auth") == ""      # builds+caches the index
    _add_library_row("calibreweb:urn:uuid:late", "Late Arrival", "Auth", "cw:99")
    db.invalidate_cover_index()
    assert db.library_cover("Late Arrival", "Auth") == "cw:99"


def test_cover_index_does_not_offer_a_book_with_no_cover(app):
    from stackarr import db
    _add_library_row("calibreweb:urn:uuid:nc", "No Cover Book", "Auth", "")
    db.invalidate_cover_index()
    assert db.library_cover("No Cover Book", "Auth") == ""


# ---------------------------------- a backend's history is fetched once a page
def test_reading_history_is_fetched_once_per_request(app):
    """Calibre-Web's history is a dozen HTTP calls; the home page used to ask
    for it twice in one render."""
    from stackarr import routes
    calls = []

    class FakeBackend:
        id = "fake"

        def reading_history(self, u):
            calls.append(u)
            return [{"item_id": "x", "finished": True, "last_update": 0}]

    be, user = FakeBackend(), {"id": 1}
    with app.test_request_context("/home"):
        assert routes.reading_history(be, user) == routes.reading_history(be, user)
        routes.reading_history(be, user)
    assert len(calls) == 1, f"history fetched {len(calls)} times in one request"


def test_reading_history_is_refetched_on_the_next_request(app):
    """It must be per-request, or marking a book read would not show up."""
    from stackarr import routes
    calls = []

    class FakeBackend:
        id = "fake"

        def reading_history(self, u):
            calls.append(u)
            return []

    be, user = FakeBackend(), {"id": 1}
    for _ in range(3):
        with app.test_request_context("/home"):
            routes.reading_history(be, user)
    assert len(calls) == 3
