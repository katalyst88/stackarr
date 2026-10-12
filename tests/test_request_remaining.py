"""'Request remaining' guardrails, and the author-reuse fix. Field cases:
"Get full series" monitored and searched every Chaptarr row in the series
(owned ones too) and fell back to the author's whole backlist; and requests
for The Knight and The Pilot failed with "Chaptarr couldn't find 'Will
Wight'" because the author lookup came back empty on back-to-back calls
although he was already Chaptarr author id 3."""
import pytest

from fakes import SAME_ORIGIN, book
from stackarr import chaptarr, db
from test_series import FUTURE, HORIZON, SERIES, _client, aud, household  # noqa: F401  (fixtures)

WIGHT = {"id": 3, "authorName": "Will Wight", "foreignAuthorId": "hc:3", "monitored": True,
         "audiobookMonitorExisting": chaptarr.MONITOR_EXISTING_SELECTED, "audiobookRootFolderPath": "/audiobooks",
         "audiobookQualityProfileId": 2, "audiobookMetadataProfileId": 1,
         "ebookMonitorExisting": chaptarr.MONITOR_EXISTING_SELECTED, "ebookRootFolderPath": "/books",
         "ebookQualityProfileId": 1, "ebookMetadataProfileId": 2}


def _chaptarr_rows(media=("audiobook",)):
    rows, i = [], 40
    for asin, seq, title, _ in HORIZON:
        for m in media:
            i += 1
            rows.append(book(i, title, m, files=1 if seq <= 2 else 0, series=f"The Last Horizon #{seq}"))
    return rows + [book(90, "Unsouled", "audiobook", series="Cradle #1"),
                   book(91, "Soulsmith", "audiobook", series="Cradle #2")]


@pytest.fixture
def chap(aud, monkeypatch):
    from stackarr import routes
    monkeypatch.setattr(routes, "_LAST_HEAVY", {})        # per-user cooldown is process-wide
    fake = aud.chaptarr
    fake.lookup = []                          # the field failure: lookup comes back empty
    fake.authors = [dict(WIGHT)]
    fake.books = {3: _chaptarr_rows()}
    return fake


def _ids(fake, title, media="audiobook"):
    return [b["id"] for b in fake.books[3] if b["title"] == title and b["mediaType"] == media]


def _monitored(fake):
    return [c[3]["bookIds"] for c in fake.calls_to("PUT", "/api/v1/book/monitor")]


def _commands(fake):
    return [c[3] for c in fake.calls_to("POST", "/api/v1/command")]


def _requests(**where):
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM requests WHERE source='series_book' ORDER BY id")]
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


def _post(client, keys, fmt="audiobook", **target):
    body = {"series_asin": SERIES, "series": "The Last Horizon", "author": "Will Wight",
            "format": fmt, "keys": keys, **target}
    return client.post("/api/series/request-remaining", json=body, headers=SAME_ORIGIN)


ALL_KEYS = [a for a, _, _, _ in HORIZON]


def test_existing_author_is_reused_without_a_lookup(chap):
    chap.books = {3: [book(44, "The Pilot", "audiobook")]}
    res = chaptarr.add_and_search("The Pilot", "Will Wight", fmt="audiobook")
    assert res["ok"], res
    assert not chap.gets("/api/v1/author/lookup")
    assert not chap.calls_to("POST", "/api/v1/author")
    assert _monitored(chap) == [[44]]


def test_lookup_still_used_for_a_new_author(chap):
    chap.authors = []
    chap.lookup = [{"authorName": "Will Wight", "foreignAuthorId": "hc:3", "folder": "Will Wight"}]
    chap.books_on_add = [book(1, "The Pilot", "audiobook")]
    assert chaptarr.add_and_search("The Pilot", "Will Wight", fmt="audiobook")["ok"]
    assert len(chap.gets("/api/v1/author/lookup")) == 1
    assert len(chap.calls_to("POST", "/api/v1/author")) == 1


def test_sends_only_what_is_missing_and_one_search(chap, household):
    with db.conn() as c:                                   # nothing requested yet: #3 and #4 are missing
        c.execute("DELETE FROM requests")
    r = _post(_client(household), ALL_KEYS).get_json()
    assert r["ok"] and r["count"] == 2, r
    want = _ids(chap, "The Knight") + _ids(chap, "The Pilot")
    assert _monitored(chap) == [want]                      # only those rows, not #1/#2 (owned) or #5 (unreleased)
    assert _commands(chap) == [{"name": "BookSearch", "bookIds": want}]
    assert not chap.gets("/api/v1/author/lookup")          # author reused (the "couldn't find Will Wight" fix)
    rows = _requests()
    assert [(x["title"], x["status"], x["format"]) for x in rows] == [
        ("The Knight", "handed", "audiobook"), ("The Pilot", "handed", "audiobook")]
    assert all(x["chaptarr_ref"] == "3" for x in rows)


def test_skips_requested_and_unreleased_even_when_posted(chap, household):
    r = _post(_client(household), ALL_KEYS).get_json()     # The Knight is requested in the fixture
    assert r["count"] == 1
    assert _monitored(chap) == [_ids(chap, "The Pilot")]
    assert [x["title"] for x in _requests()] == ["The Pilot"]


def test_only_confirmed_books_are_sent(chap, household):
    with db.conn() as c:
        c.execute("DELETE FROM requests")
    _post(_client(household), ["B0D3915Q9H"])             # confirmed The Knight only
    assert _monitored(chap) == [_ids(chap, "The Knight")]
    assert [x["title"] for x in _requests()] == ["The Knight"]


def test_nothing_left(chap, household):
    r = _post(_client(household), ["B0BVMW49D1", "B0HM96MTRZ"]).get_json()   # finished + unreleased
    assert r["ok"] is False and "Nothing left" in r["detail"]
    assert not _monitored(chap) and not _requests()


def test_capped_per_click(chap, household, aud):
    long = [(f"B0LONG{i:04d}", i, f"Long Book {i}", "2020-01-01") for i in range(1, 15)]
    aud.add_series("B0LONGSERI", "Long Series", long)
    chap.books = {3: [book(200 + i, f"Long Book {i}", "audiobook", series=f"Long Series #{i}") for i in range(1, 15)]}
    r = _post(_client(household), [a for a, _, _, _ in long], series_asin="B0LONGSERI", series="Long Series").get_json()
    assert r["count"] == 10
    assert _monitored(chap) == [[200 + i for i in range(1, 11)]]
    assert len(_requests()) == 10


def test_cap_applies_to_what_was_ticked_not_the_first_ten(chap, household, aud):
    long = [(f"B0LONG{i:04d}", i, f"Long Book {i}", "2020-01-01") for i in range(1, 15)]
    aud.add_series("B0LONGSERI", "Long Series", long)
    chap.books = {3: [book(200 + i, f"Long Book {i}", "audiobook", series=f"Long Series #{i}") for i in range(1, 15)]}
    ticked = [a for a, s, _, _ in long if s >= 3]                 # unticked #1-#2, ticked #11-#14 instead
    _post(_client(household), ticked, series_asin="B0LONGSERI", series="Long Series")
    assert _monitored(chap) == [[200 + i for i in range(3, 13)]]   # the first ten TICKED: #3-#12


def test_never_falls_back_to_the_whole_author(chap, household):
    chap.books = {3: [book(90, "Unsouled", "audiobook", series="Cradle #1"),
                      book(91, "Soulsmith", "audiobook", series="Cradle #2")]}
    r = _post(_client(household), ALL_KEYS).get_json()
    assert r["ok"] is False and "isn't listed upstream" in r["detail"]
    assert not _monitored(chap) and not _commands(chap)    # no AuthorSearch, no backlist monitoring
    assert [(x["title"], x["status"]) for x in _requests()] == [("The Pilot", "failed")]


def test_book_chaptarr_already_has_is_not_monitored(chap, household):
    pilot = next(b for b in chap.books[3] if b["title"] == "The Pilot")
    pilot["statistics"]["bookFileCount"] = 1
    r = _post(_client(household), ALL_KEYS).get_json()
    assert r["ok"] and not _monitored(chap)
    assert [(x["title"], x["status"]) for x in _requests()] == [("The Pilot", "available")]


def test_both_formats_one_search_per_media_and_a_row_each(chap, household):
    db.set_meta("formats", "both")
    chap.books = {3: _chaptarr_rows(("audiobook", "ebook"))}
    r = _post(_client(household), ["B0F8CJQZM3"], fmt="both").get_json()
    assert r["ok"]
    assert _monitored(chap) == [_ids(chap, "The Pilot", "audiobook"), _ids(chap, "The Pilot", "ebook")]
    assert len(_commands(chap)) == 2
    assert [(x["title"], x["format"]) for x in _requests()] == [("The Pilot", "audiobook"), ("The Pilot", "ebook")]


def test_non_admin_goes_through_approval_one_row_per_book(chap, household):
    with db.conn() as c:
        c.execute("DELETE FROM requests")
        uid = c.execute("INSERT INTO users (username, role, abs_token) VALUES ('kid','user','tok2')").lastrowid
    db.set_meta("require_approval", "1")
    r = _post(_client(uid), ALL_KEYS).get_json()
    assert r["ok"] and r["pending"] and r["count"] == 2
    assert not _monitored(chap) and not _commands(chap)
    rows = _requests(status="pending_approval")
    assert [x["title"] for x in rows] == ["The Knight", "The Pilot"]


def test_approving_a_series_book_grabs_just_that_book(chap, household):
    from fakes import SAME_ORIGIN as H
    with db.conn() as c:
        rid = c.execute("INSERT INTO requests (user_id,title,author,status,format,source) VALUES "
                        "(?, 'The Pilot', 'Will Wight', 'pending_approval', 'audiobook', 'series_book')",
                        (household,)).lastrowid
    assert _client(household).post(f"/api/requests/{rid}/approve", headers=H).get_json()["ok"]
    assert _monitored(chap) == [_ids(chap, "The Pilot")]


def test_csrf_and_input_guards(chap, household):
    client = _client(household)
    assert client.post("/api/series/request-remaining", json={"keys": ALL_KEYS, "series_asin": SERIES}).status_code == 403
    assert client.post("/api/series/request-remaining", json={"series_asin": SERIES},
                       headers=SAME_ORIGIN).status_code == 400
    assert client.post("/api/series/request-remaining", json={"keys": ["x"]}, headers=SAME_ORIGIN).status_code == 400
    assert not _monitored(chap)


def test_cooldown_between_clicks(chap, household):
    client = _client(household)
    _post(client, ALL_KEYS)
    assert _post(client, ALL_KEYS).status_code == 429


def test_old_series_endpoint_still_works_but_says_deprecated(chap, household):
    r = _client(household).post("/api/series/add", json={"series": "The Last Horizon", "author": "Will Wight"},
                                headers=SAME_ORIGIN)
    assert r.headers.get("Deprecation") == "true"
    assert r.get_json()["ok"]
