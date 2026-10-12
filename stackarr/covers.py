"""Cover style: Audible's art as is, or a clean print cover.

Audiobook covers from Audible often carry its yellow "Audible Original" /
"Unabridged" banner. With Settings → General → Cover style = Clean, a card
or book page showing an Audible cover shows the book's print/ebook cover
instead — from the ebook catalogues Stackarr already uses (Google Books, Open
Library) — and falls back to the Audible one when there is none.

Never on a page request: an unknown book shows the Audible cover this time
and is queued for the background worker (scheduler.queue_job("pcover", …)),
which remembers the answer in meta ("pcover:<key>"; a miss for a week). No
new hosts."""
import json
import logging
import re
import time

from . import db, titles

log = logging.getLogger("stackarr.covers")

STYLES = ("audible", "clean")
MISS_DAYS = 7
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
    return "" if time.time() - float(v.get("at") or 0) < MISS_DAYS * 86400 else None


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
    url = ""
    try:
        a = (author or "").split(",")[0].strip().lower()
        hits = [h for h in ebookmeta.search(f"{titles.clean(title)} {author}", num=5)
                if h.get("cover") and a and a in (h.get("author") or "").lower()]
        m = titles.matches(hits, title)
        url = (m[0]["cover"] if m else "") or ""
        url = url.replace("http://", "https://", 1)
    except Exception as e:
        log.debug("print cover lookup failed for %s: %s", title, e)
    db.set_meta(_key(title, author), json.dumps({"url": url, "at": int(time.time())}))
    return url


def bookcover(b) -> str:
    """Jinja filter: `{{ s|bookcover }}` — the row's cover in the chosen style."""
    if not b:
        return ""
    get = b.get if hasattr(b, "get") else (lambda k, d=None: getattr(b, k, d))
    return display(get("cover") or "", get("title") or "", get("author") or "")
