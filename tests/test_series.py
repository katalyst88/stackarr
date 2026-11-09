"""Series maps and the "Next in <series>" strip / Home "Continue" card. The
fixture is a real series (The Last Horizon, Will Wight; series ASIN
B0BVQ26GG1) in the household state that broke in the field: #1 finished in
Audiobookshelf, #2 in the library, #3 requested, #4 missing (its request
failed), #5 not out yet. No live calls: Audible and Chaptarr are fakes."""
import datetime
import time

import pytest

from fakes import FakeAudible, book
from stackarr import absclient, audible, chaptarr, db, seriesmap

SERIES = "B0BVQ26GG1"
FUTURE = str(datetime.date.today() + datetime.timedelta(days=60))
HORIZON = [("B0BVMW49D1", 1, "The Captain", "2023-04-04"),
           ("B0CL1HY6G4", 2, "The Engineer", "2023-12-05"),
           ("B0D3915Q9H", 3, "The Knight", "2024-06-11"),
           ("B0F8CJQZM3", 4, "The Pilot", "2025-07-01"),
           ("B0HM96MTRZ", 5, "The Commander", FUTURE)]


@pytest.fixture
def aud(fake_chaptarr, monkeypatch):
    """Fake Audible + fake Chaptarr behind the one `requests.get` they share."""
    fa = FakeAudible()
    fa.add_series(SERIES, "The Last Horizon", HORIZON)

    def get(url, **kw):
        return fa.get(url, **kw) if "audible." in url else fake_chaptarr.get(url, **kw)
    monkeypatch.setattr(audible.requests, "get", get)
    fa.chaptarr = fake_chaptarr
    return fa


@pytest.fixture
def household(tmp_db, monkeypatch):
    """The library/request state of the field case, plus an ABS finish of #1."""
    with db.conn() as c:
        uid = c.execute("INSERT INTO users (username, role, abs_token) VALUES ('reader','admin','tok')").lastrowid
        c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES "
                  "('li_1','The Captain: The Last Horizon, Book 1','Will Wight','','audiobook'),"
                  "('li_2','The Engineer','Will Wight','','audiobook')")
        c.execute("INSERT INTO requests (user_id,title,author,status,format) VALUES "
                  "(?, 'The Knight','Will Wight','handed','audiobook'),"
                  "(?, 'The Pilot','Will Wight','failed','audiobook')", (uid, uid))
    finished_at = (time.time() - 3 * 86400) * 1000
    monkeypatch.setattr(absclient, "listening_history", lambda tok: [
        {"item_id": "li_1", "finished": True, "progress": 1, "last_update": finished_at}])
    db.set_meta("formats", "audiobook")
    return uid


def test_audible_series_is_two_calls_in_order_then_cached(aud):
    data = seriesmap.from_audible(SERIES)
    assert data["name"] == "The Last Horizon" and data["source"] == "audible"
    assert [(b["seq"], b["title"]) for b in data["books"]] == [
        (1, "The Captain"), (2, "The Engineer"), (3, "The Knight"), (4, "The Pilot"), (5, "The Commander")]
    assert data["books"][0]["cover"].endswith("._SL1500_.jpg")
    paths = [p for p, _ in aud.calls]
    assert paths == [f"/1.0/catalog/products/{SERIES}", "/1.0/catalog/products"]
    assert aud.calls[1][1]["asins"].count(",") == 4          # ONE batch lookup for all five
    seriesmap.from_audible(SERIES)
    assert len(aud.calls) == 2                               # second view: cache hit


def test_omnibus_and_dramatized_editions_dont_take_a_position(aud):
    aud.add_series("B0SERIES02", "Cradle", [
        ("B0UNSOULED", 1, "Unsouled", "2016-01-01"),
        ("B0GAUNSOUL", 1, "Unsouled (Dramatized Adaptation)", "2023-01-01"),
        ("B0OMNIBUS1", "1-3", "Cradle: Foundation", "2018-01-01"),
        ("B0SOULSMTH", 2, "Soulsmith", "2016-06-01")])
    data = seriesmap.from_audible("B0SERIES02")
    assert [b["asin"] for b in data["books"]] == ["B0UNSOULED", "B0SOULSMTH"]


def test_a_miss_is_cached_for_an_hour(aud, monkeypatch):
    assert seriesmap.from_audible("B0NOTSERIE") is None
    seriesmap.from_audible("B0NOTSERIE")
    assert len(aud.calls) == 1
    now = time.time()
    monkeypatch.setattr(seriesmap.time, "time", lambda: now + seriesmap.MISS_TTL + 5)
    seriesmap.from_audible("B0NOTSERIE")
    assert len(aud.calls) == 2


def test_library_book_without_asin_is_placed_by_title_and_author(aud):
    got = seriesmap.series_of("", "The Captain: The Last Horizon, Book 1", "Will Wight")
    assert got == {"series_asin": SERIES, "name": "The Last Horizon", "seq": 1.0}


def test_search_hit_by_another_author_is_not_trusted(aud):
    assert seriesmap.series_of("", "The Captain", "Someone Else") == {}


def test_ebook_only_series_falls_back_to_chaptarr_without_an_author_lookup(aud):
    aud.chaptarr.authors = [{"id": 3, "authorName": "Will Wight"}]
    aud.chaptarr.books = {3: [
        book(31, "The Crimson Vault", "ebook", series="The Elder Empire: Shadow #2"),
        book(30, "Of Shadow and Sea", "ebook", series="The Elder Empire: Shadow #1"),
        book(32, "Of Shadow and Sea", "audiobook", series="The Elder Empire: Shadow #1"),
        book(33, "Unrelated", "ebook", series="Cradle #4")]}
    data = seriesmap.resolve(series="The Elder Empire: Shadow", author="Will Wight")
    assert data["source"] == "chaptarr"
    assert [(b["seq"], b["title"]) for b in data["books"]] == [(1, "Of Shadow and Sea"), (2, "The Crimson Vault")]
    assert not aud.chaptarr.gets("/api/v1/author/lookup")


def test_chaptarr_fallback_never_adds_an_unknown_author(aud):
    assert seriesmap.resolve(series="Mother of Learning", author="Domagoj Kurmaic") is None
    assert not aud.chaptarr.gets("/api/v1/author/lookup")
    assert not aud.chaptarr.calls_to("POST", "/api/v1/author")


def _books(uid):
    from stackarr import picks
    return seriesmap.annotate(seriesmap.from_audible(SERIES), picks.finished_works(db.get_user(uid)))


def test_each_book_gets_its_household_state(aud, household):
    states = [(b["seq"], b["state"]) for b in _books(household)]
    assert states == [(1, "finished"), (2, "available"), (3, "handed"), (4, "missing"), (5, "upcoming")]


def test_strip_starts_after_the_highest_finished_book(aud, household):
    books = _books(household)
    start, nxt = seriesmap.strip(books, after=4)            # viewing #4: still after what you've read
    assert start == 1 and [b["seq"] for b in nxt] == [2, 3, 4]


def test_strip_starts_after_the_viewed_book_when_nothing_is_finished(aud, household, monkeypatch):
    monkeypatch.setattr(absclient, "listening_history", lambda tok: [])
    start, nxt = seriesmap.strip(_books(household), after=2)
    assert start == 2 and [b["seq"] for b in nxt] == [3, 4, 5]


def _client(uid):
    from stackarr import create_app
    app = create_app()
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    return client


def test_next_endpoint_for_a_book_page(aud, household):
    r = _client(household).get(f"/api/series/next?series_asin={SERIES}&asin=B0BVMW49D1&seq=1").get_json()
    assert r["ok"] and r["series"] == "The Last Horizon" and r["after"] == 1
    assert r["after_title"] == "The Captain"
    assert [(b["title"], b["state"]) for b in r["next"]] == [
        ("The Engineer", "available"), ("The Knight", "handed"), ("The Pilot", "missing")]


def test_next_endpoint_rejects_a_non_asin_in_the_url_path(aud, household):
    r = _client(household).get("/api/series/next?series_asin=../../evil")
    assert r.status_code == 400
    assert not aud.calls


def test_continue_card_follows_a_recent_finish(aud, household):
    r = _client(household).get("/api/series/continue").get_json()
    assert r["ok"] and r["series"] == "The Last Horizon"
    assert r["finished_title"].startswith("The Captain") and r["finished_days"] == 3
    assert [b["seq"] for b in r["next"]] == [2, 3, 4]


def test_continue_card_ignores_an_old_finish(aud, household, monkeypatch):
    old = (time.time() - 30 * 86400) * 1000
    monkeypatch.setattr(absclient, "listening_history", lambda tok: [
        {"item_id": "li_1", "finished": True, "progress": 1, "last_update": old}])
    assert _client(household).get("/api/series/continue").get_json() == {"ok": False}
    assert not aud.calls


def test_book_page_carries_the_series_for_the_strip(aud, household, monkeypatch):
    from stackarr import audnexus, tagging
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    html = _client(household).get("/book/B0CL1HY6G4").get_data(as_text=True)
    assert 'id="series-next"' in html and f'data-series-asin="{SERIES}"' in html
    assert 'data-seq="2.0"' in html
