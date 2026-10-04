"""Is a book already in the library? Library titles from Audiobookshelf are
often decorated ("The Captain: The Last Horizon, Book 1", "House of Blades
(Unabridged)") and usually have no ASIN, while catalogue titles are plain (or
decorated differently). These are real library rows from an install where every
one of these books still showed a Request button."""
import pytest

from stackarr import absclient, db, routes, titles

LIBRARY = [
    ("The Captain: The Last Horizon, Book 1", "Will Wight", "audiobook"),
    ("House of Blades (Unabridged)", "Will Wight", "audiobook"),
    ("Unsouled: Cradle, Volume 1", "Will Wight", "audiobook"),
    ("Wintersteel: Cradle, Book 8", "Will Wight", "audiobook"),
    ("Mistborn: Secret History", "Brandon Sanderson", "audiobook"),
    ("Star Wars: Thrawn", "Timothy Zahn", "audiobook"),
    ("Beware of Chicken", "Casualfarmer", "ebook"),
]


@pytest.fixture
def lib(tmp_db):
    with db.conn() as c:
        for i, (t, a, f) in enumerate(LIBRARY):
            c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES (?,?,?,'',?)",
                      (f"li_{i}", t, a, f))
    return db


def _owned(title, author, fmt=None, asin=""):
    with db.conn() as c:
        return routes._owned(c, asin, title, author, fmt=fmt)


@pytest.mark.parametrize("title", ["The Captain", "House of Blades", "Unsouled", "Wintersteel",
                                   "Unsouled: Cradle, Book 1", "The Captain (Unabridged)"])
def test_decorated_library_titles_count_as_owned(lib, title):
    assert _owned(title, "Will Wight")
    assert _owned(title, "Will Wight", fmt="audiobook")


def test_format_still_respected(lib):
    assert not _owned("The Captain", "Will Wight", fmt="ebook")
    assert _owned("Beware of Chicken: A Xianxia Cultivation Novel", "Casualfarmer", fmt="ebook")
    assert not _owned("Beware of Chicken: A Xianxia Cultivation Novel", "Casualfarmer", fmt="audiobook")


@pytest.mark.parametrize("title,author", [
    ("Soulsmith", "Will Wight"),                         # not in the library
    ("The Captain", "Someone Else"),                     # same title, other author
    ("The Captain", ""),                                 # no author → no title match
    ("Mistborn: The Final Empire", "Brandon Sanderson"),  # same lead-in, different work
    ("Star Wars: Ahsoka", "Timothy Zahn"),               # same franchise, different work
    ("House", "Will Wight"),                              # a prefix is not the work
])
def test_different_books_are_not_owned(lib, title, author):
    assert not _owned(title, author)


def test_asin_match_still_wins(lib):
    with db.conn() as c:
        c.execute("UPDATE library SET asin='B0ASIN0001' WHERE item_id='li_1'")
    assert _owned("Something Else Entirely", "Nobody", asin="B0ASIN0001")


def test_book_state_shows_available(lib):
    assert routes._state_for("B0NOTINLIB", "The Captain", "Will Wight") == "available"


def test_mark_read_pushes_to_the_decorated_library_item(lib, monkeypatch):
    finished = []
    monkeypatch.setattr(absclient, "set_finished", lambda tok, item_id: finished.append(item_id) or True)
    assert routes._push_read_to_source({"abs_token": "t"}, "The Captain", "Will Wight",
                                       "audiobook") == "Audiobookshelf"
    assert finished == ["li_0"]


def test_mark_read_push_skips_other_authors_book(lib, monkeypatch):
    finished = []
    monkeypatch.setattr(absclient, "set_finished", lambda tok, item_id: finished.append(item_id) or True)
    assert routes._push_read_to_source({"abs_token": "t"}, "The Captain", "Someone Else",
                                       "audiobook") is None
    assert finished == []


# --- the shared matcher itself ---------------------------------------------
def test_clean_strips_edition_and_series_decoration():
    assert titles.clean("House of Blades (Unabridged)") == "House of Blades"
    assert titles.clean("The Captain: The Last Horizon, Book 1") == "The Captain: The Last Horizon"
    assert titles.clean("Unsouled: Cradle, Volume 1") == "Unsouled: Cradle"
    assert titles.clean("Dune [Dramatized Adaptation]") == "Dune"
    assert titles.clean("Book One") == "Book One"        # never cleans a title away


def test_known_works_tolerates_subtitles_without_collapsing_franchises():
    known = titles.KnownWorks()
    known.add("The Captain: The Last Horizon, Book 1", "Will Wight")
    known.add("Beware of Chicken", "Casualfarmer")
    known.add("Star Wars: Thrawn", "Timothy Zahn")
    assert ("The Captain", "Will Wight") in known
    assert ("Beware of Chicken: A Xianxia Cultivation Novel", "Casualfarmer") in known
    assert ("Star Wars: Thrawn (Unabridged)", "Timothy Zahn") in known
    assert ("Star Wars: Ahsoka", "Timothy Zahn") not in known
    assert ("The Captain", "Someone Else") not in known
