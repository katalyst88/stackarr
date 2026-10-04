"""In-memory stand-ins for the services Stackarr talks to."""
import re
from urllib.parse import urlparse

from stackarr import db

CHAPTARR = "http://chaptarr.test"
# what a browser sends on a same-origin POST; the CSRF guard requires it
SAME_ORIGIN = {"Origin": "http://localhost"}


def set_formats(mode: str):
    db.set_meta("formats", mode)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body
        self.ok = status < 400
        self.text = "" if body is None else str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeChaptarr:
    """Just enough of Chaptarr's /api/v1 to drive the handoff and the
    availability check. `calls` records (method, path, params, json) for every
    request so tests can assert the exact payloads Stackarr sends."""

    def __init__(self):
        self.calls = []
        self.lookup = []           # /author/lookup results
        self.authors = []          # authors already in Chaptarr
        self.books = {}            # author id -> [book rows]
        self.books_on_add = []     # rows a freshly POSTed author gets
        self.next_id = 100

    # -- helpers for tests ---------------------------------------------------
    def calls_to(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]

    def gets(self, path):
        return self.calls_to("GET", path)

    # -- transport -----------------------------------------------------------
    def _route(self, method, url, params=None, json=None, **_):
        path = urlparse(url).path
        self.calls.append((method, path, dict(params or {}), json))
        if method == "GET" and path == "/api/v1/tag":
            return Resp(200, [{"id": 1, "label": "stackarr"}])
        if method == "GET" and path == "/api/v1/author/lookup":
            return Resp(200, self.lookup)
        if method == "GET" and path == "/api/v1/author":
            return Resp(200, self.authors)
        if method == "POST" and path == "/api/v1/author":
            a = dict(json, id=self.next_id)
            self.next_id += 1
            self.authors.append(a)
            self.books[a["id"]] = [dict(b, authorId=a["id"]) for b in self.books_on_add]
            return Resp(201, a)
        m = re.fullmatch(r"/api/v1/author/(\d+)", path)
        if method == "PUT" and m:
            return Resp(202, json)
        if method == "GET" and path == "/api/v1/book":
            return Resp(200, self.books.get(int(params["authorId"]), []))
        if method == "PUT" and path == "/api/v1/book/monitor":
            return Resp(202, [])
        if method == "POST" and path == "/api/v1/command":
            return Resp(201, {"id": 1})
        if method == "GET" and path == "/api/v1/release":
            # Chaptarr's interactive search; it also names the row's other-media
            # sibling (same work, other mediaType)
            rows = [b for bs in self.books.values() for b in bs]
            me = next((b for b in rows if b["id"] == int(params["bookId"])), None)
            sib = next((b for b in rows if me and b["id"] != me["id"]
                        and b.get("title") == me.get("title")
                        and b.get("authorId") == me.get("authorId")), None)
            return Resp(200, {"releases": [], "siblingBookId": sib and sib["id"],
                              "siblingMediaType": sib and sib.get("mediaType")})
        return Resp(404, None)

    def get(self, url, **kw):
        return self._route("GET", url, **kw)

    def post(self, url, **kw):
        return self._route("POST", url, **kw)

    def put(self, url, **kw):
        return self._route("PUT", url, **kw)


def book(bid, title, media="audiobook", asin="", files=0, monitored=False, series=""):
    """A Chaptarr book row (media-split schema: one row per media edition)."""
    return {"id": bid, "title": title, "mediaType": media, "asin": asin,
            "audibleASIN": asin, "monitored": monitored, "seriesTitle": series,
            "statistics": {"bookFileCount": files}}


class FakeSource:
    """A connected library backend (Audiobookshelf, Kavita, …) whose snapshot
    is whatever the test hands it."""

    def __init__(self, items, media_format="audiobook", bid="abs"):
        self.items, self.media_format, self.id = items, media_format, bid

    def library_items(self):
        return [dict({"format": self.media_format, "source": self.id}, **m) for m in self.items]
