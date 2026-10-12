"""Catalogue lists in the reader's language, one card per work.

Audible's author catalogue for Matt Dinniman (com.au, 2026-10-12): 36 titles,
13 English, the rest Spanish/French/German/Italian/Portuguese editions of
Dungeon Crawler Carl — all on the author page, several per book."""
from stackarr import audible, db, routes, seriesmap


def _b(asin, title, lang, n=0, series="Dungeon Crawler Carl"):
    return {"asin": asin, "title": title, "author": "Matt Dinniman", "language": lang, "num_ratings": n,
            "series": series, "cover": "", "release_date": "2020-01-01"}


DINNIMAN = [_b("B0FVXFFNKF", "Dungeon Crawler Carl (Italian Edition)", "Italian"),
            _b("B0G4RZ7F9K", "Dungeon Crawler Carl (German Edition)", "German"),
            _b("B0H27GS86K", "Operation Bounce House", "German"),
            _b("B0FK5JF894", "Operation Bounce House", "English", 900),
            _b("B0934VH42B", "Carl's Doomsday Scenario", "English", 40000),
            _b("B08V7VS8H2", "Dungeon Crawler Carl", "English", 80000),
            _b("B0XXXXXXX1", "Dungeon Crawler Carl", "English", 12)]      # a re-release of the same work


def test_clean_catalogue_keeps_the_language_and_one_edition(tmp_db):
    got = routes._clean_catalogue(DINNIMAN)
    assert [b["asin"] for b in got] == ["B0FK5JF894", "B0934VH42B", "B08V7VS8H2"]


def test_any_language_keeps_every_language(tmp_db):
    db.set_meta("language", "any")
    assert len(routes._clean_catalogue(DINNIMAN)) == 6        # editions still collapse


def test_ebook_iso_codes_count_as_the_language(tmp_db):
    assert len(routes._in_language([{"title": "x", "language": "en"}, {"title": "y", "language": "de"}])) == 1


def test_author_page_shows_english_once(admin_client, monkeypatch):
    monkeypatch.setattr(audible, "by_author", lambda name, num=25: list(DINNIMAN))
    html = admin_client.get("/author/Matt Dinniman").get_data(as_text=True)
    assert "Italian Edition" not in html and "German Edition" not in html
    assert html.count('title="Dungeon Crawler Carl"') == 1 and html.count('title="Operation Bounce House"') == 1


def test_find_asin_prefers_the_language(monkeypatch):
    monkeypatch.setattr(audible, "_products", lambda params: [
        {"asin": "B0G4RZ7F9K", "title": "Dungeon Crawler Carl (German Edition)", "language": "german"},
        {"asin": "B08V7VS8H2", "title": "Dungeon Crawler Carl", "language": "english"}])
    assert audible.find_asin("Dungeon Crawler Carl", "Matt Dinniman", "english") == "B08V7VS8H2"
    assert audible.find_asin("Dungeon Crawler Carl", "Matt Dinniman") == "B0G4RZ7F9K"


def test_series_map_drops_placeholders_and_other_languages(tmp_db, monkeypatch):
    monkeypatch.setattr(audible, "series_children", lambda sa: [("B08V7VS8H2", 1.0), ("B0FVXFFNKF", 1.0),
                                                               ("B0934VH42B", 2.0), ("B0EMBARGO1", 9.0)])
    monkeypatch.setattr(audible, "by_asins", lambda asins: [
        dict(_b("B08V7VS8H2", "Dungeon Crawler Carl", "English"), series_asin="B08WJ59784"),
        dict(_b("B0FVXFFNKF", "Dungeon Crawler Carl (Italian Edition)", "Italian"), series_asin="B08WJ59784"),
        dict(_b("B0934VH42B", "Carl's Doomsday Scenario", "English"), series_asin="B08WJ59784"),
        dict(_b("B0EMBARGO1", "Embargo title BR", "English"), release_date="2200-01-01")])
    data = seriesmap.from_audible("B08WJ59784")
    assert [(b["seq"], b["asin"]) for b in data["books"]] == [(1.0, "B08V7VS8H2"), (2.0, "B0934VH42B")]
