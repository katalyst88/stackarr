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


class FakeAudible:
    """The Audible catalogue calls the series map makes: a series product's
    `relationships`, a batch `asins` lookup, product-by-ASIN, and the title
    search behind find_asin. `calls` records (path, params)."""

    def __init__(self):
        self.calls = []
        self.products = {}         # asin -> raw catalogue product
        self.series = {}           # series asin -> [(asin, sequence-string)]

    def add_series(self, series_asin, name, books, author="Will Wight"):
        """books: [(asin, seq, title, release_date)] — seq may be "1-3" etc."""
        self.series[series_asin] = [(a, str(s)) for a, s, _, _ in books]
        for a, s, t, rd in books:
            self.products[a] = {
                "asin": a, "title": t, "authors": [{"name": author}], "release_date": rd,
                "series": [{"asin": series_asin, "title": name, "sequence": str(s)}],
                "product_images": {"500": f"https://m.media-amazon.com/images/I/{a}._SL500_.jpg"}}

    def get(self, url, params=None, **_):
        path = urlparse(url).path
        params = dict(params or {})
        self.calls.append((path, params))
        m = re.fullmatch(r"/1\.0/catalog/products/(\w+)", path)
        if m:
            a = m.group(1)
            if a in self.series:
                return Resp(200, {"product": {"asin": a, "content_delivery_type": "BookSeries", "relationships": [
                    {"asin": c, "relationship_to_product": "child", "relationship_type": "series",
                     "sequence": s, "sort": str(i)} for i, (c, s) in enumerate(self.series[a])]}})
            if a in self.products:
                return Resp(200, {"product": self.products[a]})
            return Resp(404, {})
        if path == "/1.0/catalog/products":
            if params.get("asins"):
                return Resp(200, {"products": [self.products[a] for a in params["asins"].split(",")
                                               if a in self.products]})
            want = (params.get("title") or params.get("keywords") or "").lower()
            hits = [p for p in self.products.values()
                    if want and (want in p["title"].lower() or p["title"].lower() in want)]
            return Resp(200, {"products": hits[:int(params.get("num_results", 10))]})
        return Resp(404, None)


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
