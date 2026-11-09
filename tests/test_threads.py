"""Fewer requests holding waitress threads. The live log showed ~190 "Task
queue depth" warnings: Traefik's access log has bursts of 30-60 /cover
requests per Home render (309 in the session, each an ABS round trip on one
of 8 threads), 0.7-1.8 s book pages and 0.6 s typeahead searches."""
import importlib
import os

from stackarr import absclient, audible, audnexus, config, db, routes, scheduler, tagging


class _R:
    def __init__(self, body=b"\x89PNG fake", ctype="image/png"):
        self.ok, self.content, self.headers = True, body, {"Content-Type": ctype}


def test_more_threads_by_default(monkeypatch):
    monkeypatch.delenv("STACKARR_THREADS", raising=False)
    assert importlib.reload(config).THREADS == 16
    monkeypatch.setenv("STACKARR_THREADS", "2")
    assert importlib.reload(config).THREADS == 4          # floor
    monkeypatch.delenv("STACKARR_THREADS")
    importlib.reload(config)


def test_abs_cover_is_fetched_once_then_served_from_disk(admin_client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import requests
    calls = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(a[0]) or _R())
    for _ in range(3):
        r = admin_client.get("/cover/li_abc-123")
        assert r.status_code == 200 and r.data == b"\x89PNG fake" and r.mimetype == "image/png"
    assert len(calls) == 1
    assert os.path.exists(tmp_path / "covers" / "li_abc-123.png")


def test_cover_cache_never_touches_odd_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    routes._cover_cache_put("..", b"x", "image/png")
    routes._cover_cache_put("a.b", b"x", "image/png")
    assert routes._cover_cache_get("..") is None
    assert not (tmp_path / "covers").exists() or os.listdir(tmp_path / "covers") == []


def test_book_page_never_fetches_tags_inline(admin_client, monkeypatch):
    monkeypatch.setattr(audible, "by_asin", lambda a: {"asin": a, "title": "Dune", "author": "Frank Herbert",
                                                      "categories": ["Science Fiction"]})
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "_subjects", lambda *a, **k: (_ for _ in ()).throw(AssertionError("inline OL call")))
    queued = []
    monkeypatch.setattr(scheduler, "queue_job", lambda *a: queued.append(a) or True)
    assert admin_client.get("/book/B002V1OF70").status_code == 200
    assert queued and queued[0][0] == "tags" and queued[0][1] == "B002V1OF70"


def test_typeahead_repeats_are_local(admin_client, monkeypatch):
    calls = []
    monkeypatch.setattr(audible, "search", lambda q, num=12, page=0: calls.append(q) or [
        {"asin": "B002V1OF70", "title": "Dune", "author": "Frank Herbert", "cover": "", "language": "English"}])
    routes._SUGGEST_CACHE.clear()
    for _ in range(3):
        assert admin_client.get("/api/suggest?q=dune").get_json()[0]["asin"] == "B002V1OF70"
    assert len(calls) == 1
