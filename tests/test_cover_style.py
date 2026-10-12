"""Settings → Cover style: Audible (as is) or Clean (the print cover)."""
import json
import time

from fakes import SAME_ORIGIN
from stackarr import audible, audnexus, covers, db, ebookmeta, scheduler, tagging

AUD = "https://m.media-amazon.com/images/I/81abc._SL500_.jpg"
PRINT = "https://books.google.com/books/content?id=abc&printsec=frontcover&img=1&zoom=1"


def test_audible_style_leaves_covers_alone(tmp_db):
    assert covers.display(AUD, "Dungeon Crawler Carl", "Matt Dinniman") == AUD.replace("_SL500_", "_SL1500_")


def test_clean_style_queues_then_uses_the_print_cover(tmp_db, monkeypatch):
    db.set_meta("cover_style", "clean")
    queued = []
    monkeypatch.setattr(scheduler, "queue_job", lambda *a: queued.append(a) or True)
    first = covers.display(AUD, "Dungeon Crawler Carl", "Matt Dinniman")
    assert "media-amazon" in first and queued == [("pcover", "Dungeon Crawler Carl", "Matt Dinniman")]
    monkeypatch.setattr(ebookmeta, "search", lambda q, num=12: [
        {"title": "Dungeon Crawler Carl", "author": "Matt Dinniman", "cover": PRINT.replace("https", "http")},
        {"title": "Dungeon Crawler Carl", "author": "Someone Else", "cover": "https://x.example/other.jpg"}])
    assert covers.resolve("Dungeon Crawler Carl", "Matt Dinniman") == PRINT
    assert covers.display(AUD, "Dungeon Crawler Carl", "Matt Dinniman") == PRINT


def test_clean_style_keeps_audible_when_there_is_no_print_cover(tmp_db, monkeypatch):
    db.set_meta("cover_style", "clean")
    monkeypatch.setattr(ebookmeta, "search", lambda q, num=12: [])
    covers.resolve("An Audible Original", "Some Narrator")
    monkeypatch.setattr(scheduler, "queue_job", lambda *a: (_ for _ in ()).throw(AssertionError("requeued a miss")))
    assert "media-amazon" in covers.display(AUD, "An Audible Original", "Some Narrator")


def test_library_and_print_covers_are_never_swapped(tmp_db):
    db.set_meta("cover_style", "clean")
    assert covers.display("/cover/li_1", "Dungeon Crawler Carl", "Matt Dinniman") == "/cover/li_1"
    assert covers.display(PRINT, "Dungeon Crawler Carl", "Matt Dinniman") == PRINT


def test_book_page_and_setting(admin_client, monkeypatch):
    monkeypatch.setattr(audible, "by_asin", lambda a: {"asin": a, "title": "Dungeon Crawler Carl",
                                                      "author": "Matt Dinniman", "cover": AUD})
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    assert admin_client.post("/api/settings", json={"cover_style": "fancy"}, headers=SAME_ORIGIN).status_code == 400
    assert admin_client.post("/api/settings", json={"cover_style": "clean"}, headers=SAME_ORIGIN).get_json()["ok"]
    db.set_meta(covers._key("Dungeon Crawler Carl", "Matt Dinniman"), json.dumps({"url": PRINT, "at": int(time.time())}))
    html = admin_client.get("/book/B08V7VS8H2").get_data(as_text=True)
    cover = html.split('class="book-cover"', 1)[1].split("</div>", 1)[0]
    assert PRINT.replace("&", "&amp;") in cover
