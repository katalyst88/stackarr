"""A book page always has a cover when we have one. Dungeon Crawler Carl #1
(B08V8B2CGV, a US ASIN) isn't sold on the AU marketplace, so the catalogue
lookup returns nothing and the page drew an empty cover — although
Audiobookshelf holds the book (and its art) under that ASIN."""
from stackarr import audible, audnexus, db, scheduler, tagging


def _page(admin_client, monkeypatch, lib=True):
    monkeypatch.setattr(audible, "by_asin", lambda a: None)
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    queued = []
    monkeypatch.setattr(scheduler, "queue_cover", lambda *a: queued.append(a) or True)
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,asin,title,author,status,format) VALUES "
                  "(?,'B08V8B2CGV','Dungeon Crawler Carl','Matt Dinniman','available','audiobook')", (admin_client.uid,))
        if lib:
            c.execute("INSERT INTO library (item_id,title,author,asin,format,source) VALUES "
                      "('7930852b','Dungeon Crawler Carl (Unabridged)','Matt Dinniman','B08V8B2CGV','audiobook','abs')")
    db.invalidate_cover_index()
    return admin_client.get("/book/B08V8B2CGV").get_data(as_text=True), queued


def test_owned_book_uses_the_library_cover(admin_client, monkeypatch):
    html, _ = _page(admin_client, monkeypatch)
    cover = html.split('class="book-cover"', 1)[1].split("</div>", 1)[0]
    assert 'src="/cover/7930852b"' in cover


def test_unowned_book_gets_the_background_cover_path(admin_client, monkeypatch):
    html, _ = _page(admin_client, monkeypatch, lib=False)
    cover = html.split('class="book-cover"', 1)[1].split("</div>", 1)[0]
    assert 'src="/coverart?' in cover and "Dungeon+Crawler+Carl" in cover
