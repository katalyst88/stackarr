"""chaptarr._match_book — picking the requested work out of an author's books.
Real-world shapes from a media-split Chaptarr (one audiobook + one ebook row
per work), as fixture data."""
from fakes import book
from stackarr import chaptarr

HAMILTON = [
    book(1317, "Pandora's Star", "audiobook", asin="B001NNC0K6"),
    book(1434, "Pandora's Star", "ebook"),
    book(1320, "Judas Unchained", "audiobook"),
    book(1437, "Judas Unchained", "ebook"),
    book(1500, "The Dreaming Void", "audiobook"),
]

DWECK = [
    book(1, "Mindset: The New Psychology of Success"),
    book(2, "Mindset: 4 Book Collection Set"),
    book(3, "Mindset: Changing The Way You think To Fulfil Your Potential"),
    book(4, "Mindset - A Nova Psicologia do Sucesso"),
]


def _id(books, title, asin=""):
    return (chaptarr._match_book(books, title, asin) or {}).get("id")


def test_audible_subtitle_is_ignored():
    assert _id(HAMILTON, "Pandora's Star: Commonwealth Saga 1", "B0DLBV4Z35") == 1317
    assert _id(HAMILTON, "Judas Unchained: Commonwealth Saga 2", "B0DLBT38D2") == 1320


def test_plain_title_and_asin_tiers_unchanged():
    assert _id(HAMILTON, "Pandora's Star") == 1317
    assert _id(HAMILTON, "totally different title", "B001NNC0K6") == 1317


def test_ambiguous_stem_refuses():
    # four different "Mindset …" works — guessing would grab an omnibus or a
    # translation, so no match is the right answer
    assert _id(DWECK, "Mindset - Updated Edition", "140554399X") is None


def test_media_siblings_are_one_work():
    siblings = [book(1, "Pandora's Star", "audiobook"), book(2, "Pandora's Star", "ebook")]
    assert _id(siblings, "Pandora's Star: Commonwealth Saga 1") == 1


def test_short_stems():
    short = [book(3, "It"), book(4, "It Ends With Us")]
    assert _id(short, "It: A Novel") == 3
    assert _id([book(8, "It Ends With Us")], "It: A Novel") is None


def test_franchise_lead_in_loses_to_the_work():
    assert _id([book(9, "Star Wars"), book(10, "Thrawn")], "Star Wars: Thrawn") == 10


def test_two_distinct_works_no_guess():
    two = [book(5, "The Long Way Home"), book(6, "The Long Way Down")]
    assert _id(two, "The Long Way: Special Edition") is None


def test_longer_chaptarr_title_prefix_matches():
    assert _id([book(7, "Pandora's Star (Commonwealth Saga #1)")], "Pandora's Star") == 7


def test_empty_title():
    assert _id(HAMILTON, "") is None


def test_pick_author_prefers_the_named_author():
    results = [{"authorName": "Anna Malaika Tubbs"}, {"authorName": "Eddie S. Glaude Jr."},
               {"authorName": "James Baldwin"}]
    assert chaptarr._pick_author(results, "James Baldwin")["authorName"] == "James Baldwin"
    assert chaptarr._pick_author(results, "Somebody Else")["authorName"] == "Anna Malaika Tubbs"
