"""scheduler.refresh_library — a request flips to 'available' once its book is
in the library snapshot, or once Chaptarr has imported the requested format."""
import pytest

from fakes import FakeSource, book, set_formats
from stackarr import db, notify, scheduler


@pytest.fixture
def library(fake_chaptarr, monkeypatch):
    """Point refresh_library at fake library sources; returns a setter."""
    sources = []
    monkeypatch.setattr(scheduler.backends, "sources", lambda mode=None: list(sources))
    notified = []
    monkeypatch.setattr(notify, "request_available", lambda r, base_url="": notified.append(r["title"]))

    def use(*srcs):
        sources[:] = srcs
    use.notified = notified
    return use


def _request(title, author, fmt="audiobook", status="handed", ref="", asin="", source="manual"):
    with db.conn() as c:
        return c.execute("INSERT INTO requests (user_id,title,author,format,status,chaptarr_ref,asin,source) "
                         "VALUES (1,?,?,?,?,?,?,?)", (title, author, fmt, status, ref, asin, source)).lastrowid


def _status(rid):
    with db.conn() as c:
        return c.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()["status"]


def _abs(*items, media="audiobook"):
    return FakeSource([{"item_id": f"{media}-{i}", "title": t, "author": a}
                       for i, (t, a) in enumerate(items)], media_format=media)


# --- A1: legacy edition strings in requests.format --------------------------
def test_legacy_edition_string_matches_as_audiobook(library):
    set_formats("both")
    library(_abs(("Cradle Volume One", "Will Wight")))
    rid = _request("Cradle Volume One", "Will Wight", fmt="Unabridged")
    scheduler.refresh_library()
    assert _status(rid) == "available"


def test_format_is_still_respected(library):
    set_formats("both")
    library(_abs(("Cradle Volume One", "Will Wight")))           # audiobook only
    rid = _request("Cradle Volume One", "Will Wight", fmt="ebook", status="queued")
    scheduler.refresh_library()
    assert _status(rid) == "queued"


def test_both_needs_every_format(library):
    set_formats("both")
    library(_abs(("Cradle Volume One", "Will Wight")))
    rid = _request("Cradle Volume One", "Will Wight", fmt="both", status="queued")
    scheduler.refresh_library()
    assert _status(rid) == "queued"
    library(_abs(("Cradle Volume One", "Will Wight")),
            _abs(("Cradle Volume One", "Will Wight"), media="ebook"))
    scheduler.refresh_library()
    assert _status(rid) == "available"


# --- A2: titles matched in both directions ----------------------------------
def test_decorated_request_title_matches_plain_library_title(library):
    library(_abs(("Beware of Chicken", "Casualfarmer")))
    rid = _request("Beware of Chicken: A Xianxia Cultivation Novel", "Casualfarmer")
    scheduler.refresh_library()
    assert _status(rid) == "available"
    assert library.notified == ["Beware of Chicken: A Xianxia Cultivation Novel"]


def test_decorated_library_title_still_matches(library):
    library(_abs(("Pandora's Star (Commonwealth Saga #1)", "Peter F. Hamilton")))
    rid = _request("Pandora's Star", "Peter F. Hamilton")
    scheduler.refresh_library()
    assert _status(rid) == "available"


def test_ambiguous_stem_does_not_match(library):
    library(_abs(("Mindset: The New Psychology of Success", "Carol S. Dweck"),
                 ("Mindset: 4 Book Collection Set", "Carol S. Dweck")))
    rid = _request("Mindset - Updated Edition", "Carol S. Dweck")
    scheduler.refresh_library()
    assert _status(rid) == "handed"


def test_other_authors_book_does_not_match(library):
    library(_abs(("Beware of Chicken", "Someone Else")))
    rid = _request("Beware of Chicken: A Xianxia Cultivation Novel", "Casualfarmer")
    scheduler.refresh_library()
    assert _status(rid) == "handed"


def test_bulk_requests_are_never_matched_as_a_title(library):
    library(_abs(("Cradle", "Will Wight")))
    rid = _request("Full series: Cradle", "Will Wight", source="series")
    scheduler.refresh_library()
    assert _status(rid) == "handed"


# --- A3/A4: Chaptarr knows about files the library snapshot can't see -------
def test_ebook_imported_by_chaptarr_marks_available(library, fake_chaptarr):
    set_formats("both")
    library(_abs())                                    # no ebook source connected
    fake_chaptarr.books = {8: [book(11, "House of Blades", "audiobook", files=0),
                               book(12, "House of Blades", "ebook", files=1)]}
    rid = _request("House of Blades: The Traveler's Gate 1", "Will Wight", fmt="ebook", ref="8")
    scheduler.refresh_library()
    assert _status(rid) == "available"
    assert library.notified == ["House of Blades: The Traveler's Gate 1"]


def test_chaptarr_file_for_the_other_media_does_not_count(library, fake_chaptarr):
    library(_abs())
    fake_chaptarr.books = {8: [book(11, "House of Blades", "audiobook", files=0),
                               book(12, "House of Blades", "ebook", files=1)]}
    rid = _request("House of Blades", "Will Wight", fmt="audiobook", ref="8")
    scheduler.refresh_library()
    assert _status(rid) == "handed"


def test_author_found_by_name_when_request_has_no_ref(library, fake_chaptarr):
    library(_abs())
    fake_chaptarr.authors = [{"id": 5, "authorName": "Anna Malaika Tubbs"},
                             {"id": 8, "authorName": "Will Wight"}]
    fake_chaptarr.books = {8: [book(11, "House of Blades", "audiobook", files=2)]}
    rid = _request("House of Blades", "Will Wight, Travis Baldree")
    scheduler.refresh_library()
    assert _status(rid) == "available"


def test_chaptarr_is_asked_once_per_author_and_only_for_handed(library, fake_chaptarr):
    library(_abs())
    fake_chaptarr.authors = [{"id": 8, "authorName": "Will Wight"}]
    fake_chaptarr.books = {8: [book(11, "Unsouled"), book(12, "Soulsmith")]}
    _request("Unsouled", "Will Wight", ref="8")
    _request("Soulsmith", "Will Wight", ref="8")
    _request("Blackflame", "Will Wight")
    _request("Skysworn", "Will Wight", status="failed", ref="8")
    _request("Ghostwater", "Will Wight", status="queued", ref="8")
    scheduler.refresh_library()
    assert len(fake_chaptarr.gets("/api/v1/book")) == 1
    assert len(fake_chaptarr.gets("/api/v1/author")) == 1


def test_chaptarr_not_called_when_library_already_matched(library, fake_chaptarr):
    library(_abs(("Unsouled", "Will Wight")))
    rid = _request("Unsouled", "Will Wight", ref="8")
    scheduler.refresh_library()
    assert _status(rid) == "available"
    assert not fake_chaptarr.calls


def test_decorated_library_title_with_series_suffix(library):
    library(_abs(("The Captain: The Last Horizon, Book 1", "Will Wight"),
                 ("House of Blades (Unabridged)", "Will Wight")))
    a = _request("The Captain", "Will Wight")
    b = _request("House of Blades", "Will Wight")
    scheduler.refresh_library()
    assert _status(a) == _status(b) == "available"


def test_same_lead_in_different_work_is_not_available(library):
    library(_abs(("Mistborn: Secret History", "Brandon Sanderson")))
    rid = _request("Mistborn: The Final Empire", "Brandon Sanderson", status="queued")
    scheduler.refresh_library()
    assert _status(rid) == "queued"
