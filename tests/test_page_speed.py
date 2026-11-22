"""Page-path work that cost a thread per image or per view.

* /cover/<item_id> for an item ABS has no art for asked ABS for the item and
  then searched Audible — inline, per <img>.
* The book page fetched Audible and Audnexus one after the other on every
  view, and again on Back."""
from stackarr import absclient, audible, audnexus, db, routes, tagging


def test_cover_without_abs_art_never_searches_audible(admin_client, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("no art")))
    monkeypatch.setattr(audible, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("inline search")))
    monkeypatch.setattr(absclient, "item_detail", lambda *a: (_ for _ in ()).throw(AssertionError("inline ABS call")))
    with db.conn() as c:
        c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES "
                  "('li_x','Some Book','Some Author','','audiobook')")
    r = admin_client.get("/cover/li_x")
    assert r.status_code == 302 and "/coverart?" in r.headers["Location"] and "nolib=1" in r.headers["Location"]
    # and /coverart doesn't bounce it back to /cover (a redirect loop)
    r2 = admin_client.get(r.headers["Location"])
    assert "/cover/" not in r2.headers["Location"]


def test_book_page_catalogue_lookups_are_cached(admin_client, monkeypatch):
    calls = []
    monkeypatch.setattr(audible, "by_asin", lambda a: calls.append("audible") or {"asin": a, "title": "Dune", "author": "Frank Herbert"})
    monkeypatch.setattr(audnexus, "book", lambda a: calls.append("audnexus") or {"genres": ["Science Fiction"]})
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    for _ in range(3):
        html = admin_client.get("/book/B002V1OF70").get_data(as_text=True)
        assert "Dune" in html and "Science Fiction" in html
    assert sorted(calls) == ["audible", "audnexus"]
    assert "B002V1OF70" in routes._BOOK_META
