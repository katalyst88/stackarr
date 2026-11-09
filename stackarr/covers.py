"""Cover style: Audible's art as is, or a clean print cover.

Audiobook covers from Audible often carry its yellow "Audible Original" /
"Unabridged" banner. With Settings → General → Cover style = Clean, a card
or book page showing an Audible cover shows the book's print/ebook cover
instead — from the ebook catalogues Stackarr already uses (Google Books, Open
Library) — and falls back to the Audible one when there is none.

Never on a page request: an unknown book shows the Audible cover this time
and is queued for the background worker (scheduler.queue_job("pcover", …)),
which remembers the answer in meta ("pcover:<key>"; a miss for a week, an
empty or failed search — the catalogue down or rate-limited — only for a few
hours). No new hosts."""
import json
import logging
import re
import time

from . import db, titles

log = logging.getLogger("stackarr.covers")

STYLES = ("audible", "clean")
MISS_DAYS = 7
SOFT_MISS_HOURS = 6     # the search came back empty or failed: ask again soon
# already a print cover (the ebook catalogues' own art) — nothing to swap
_PRINT = re.compile(r"^https?://(?:books\.google\.[a-z.]+|[a-z0-9.-]*googleusercontent\.com|covers\.openlibrary\.org)/")


def style() -> str:
    s = db.get_meta("cover_style", "audible")
    return s if s in STYLES else "audible"


def _key(title, author) -> str:
    return "pcover:" + db.rating_key("", titles.clean(title or ""), (author or "").split(",")[0])


def clean_cover(title, author) -> str | None:
    """The cached print cover: a URL, "" for a known miss, None if unknown."""
    raw = db.get_meta(_key(title, author), "")
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    if v.get("url"):
        return v["url"]
    keep = SOFT_MISS_HOURS * 3600 if v.get("soft") else MISS_DAYS * 86400
    return "" if time.time() - float(v.get("at") or 0) < keep else None


def display(url, title="", author="") -> str:
    """The cover to show for a catalogue row, honouring the cover style."""
    from .audible import _hi_res
    url = url or ""
    # catalogue art only: our own library's covers are relative URLs (/cover, /libcover)
    if not url or style() != "clean" or not url.startswith(("http://", "https://")) or _PRINT.match(url) or not title:
        return _hi_res(url)
    got = clean_cover(title, author)
    if got:
        return got
    if got is None:
        try:
            from . import scheduler
            scheduler.queue_job("pcover", title, (author or "").split(",")[0])
        except Exception as e:
            log.debug("could not queue print cover for %s: %s", title, e)
    return _hi_res(url)


def resolve(title, author) -> str:
    """Background: find a print cover for (title, author) and remember it."""
    from . import ebookmeta
    url, soft = "", False
    try:
        a = (author or "").split(",")[0].strip().lower()
        found = ebookmeta.search(f"{titles.clean(title)} {author}", num=5)
        # the catalogues swallow their own errors: nothing at all back is as
        # likely a rate limit as a book they don't know
        soft = not found
        hits = [h for h in found if h.get("cover") and a and a in (h.get("author") or "").lower()]
        m = titles.matches(hits, title)
        url = (m[0]["cover"] if m else "") or ""
        url = url.replace("http://", "https://", 1)
    except Exception as e:
        soft = True
        log.debug("print cover lookup failed for %s: %s", title, e)
    v = {"url": url, "at": int(time.time())}
    if soft and not url:
        v["soft"] = 1
    db.set_meta(_key(title, author), json.dumps(v))
    return url


def local_url(ref: str) -> str:
    """The same-origin URL for a library cover ref: "cw:161" / "kv:127" →
    /libcover, an Audiobookshelf item id → /cover/<id>?v=<its update time>, so
    a cover fixed in ABS is a new URL and no browser keeps the old one."""
    from flask import url_for
    if ":" in ref:
        return url_for("main.libcover", ref=ref)
    v = db.cover_version(ref)
    return url_for("main.cover", item_id=ref, v=v) if v else url_for("main.cover", item_id=ref)


def for_book(url, title="", author="", asin="") -> str:
    """THE cover for a book, wherever it's drawn: the library's own cover when
    the household owns it (Audiobookshelf first, where covers are curated),
    else the catalogue art in the chosen style. Every section goes through
    here, so one book never shows two different covers on one page."""
    asin = asin if (asin or "").startswith("B0") else ""
    try:
        ref = db.library_cover(title or "", author or "", asin, strict=bool(url)) if title or asin else ""
    except Exception as e:
        log.debug("library cover lookup failed for %s: %s", title, e)
        ref = ""
    if ref:
        return local_url(ref)
    url = url or ""
    if url.startswith("/cover/"):
        # a stored link to an ABS cover (shelf rows keep one): give it its version
        item = url.split("?")[0][len("/cover/"):]
        if item and "/" not in item:
            return local_url(item)
    return display(url, title, author)


def bookcover(b) -> str:
    """Jinja filter: `{{ s|bookcover }}` — the row's cover (covers.for_book)."""
    if not b:
        return ""
    get = b.get if hasattr(b, "get") else (lambda k, d=None: getattr(b, k, d))
    return for_book(get("cover") or "", get("title") or "", get("author") or "", get("asin") or "")
