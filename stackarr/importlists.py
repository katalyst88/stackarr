"""Import lists: pull a user's existing "Want to Read" shelves from Goodreads
(RSS) or Hardcover (GraphQL) and feed them in as suggestion seeds. Optional;
configured per deployment."""
import logging
import re
import xml.etree.ElementTree as ET

import requests

from . import config, db

log = logging.getLogger("stackarr.importlists")


def goodreads(rss_url: str) -> list[dict]:
    """Parse a Goodreads shelf RSS (e.g. the 'to-read' shelf) into
    [{title, author}]. RSS is public per-shelf; no API key needed."""
    out = []
    try:
        # Goodreads 403s the default python-requests User-Agent, so present a
        # browser one — the per-shelf RSS itself is public, no key needed.
        r = requests.get(rss_url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            author = (item.findtext("author_name") or "").strip()
            if not author:
                # Goodreads sometimes embeds author in a namespaced tag
                m = re.search(r"author_name>([^<]+)<", ET.tostring(item, encoding="unicode"))
                author = m.group(1).strip() if m else ""
            if title:
                out.append({"title": title, "author": author})
    except Exception as e:
        log.warning("goodreads import failed: %s", e)
    return out


def hardcover(token: str) -> list[dict]:
    """Query Hardcover for the user's want-to-read list via GraphQL."""
    if not token:
        return []
    q = {"query": "{ me { user_books(where: {status_id: {_eq: 1}}) "
                  "{ book { title contributions { author { name } } } } } }"}
    try:
        r = requests.post("https://api.hardcover.app/v1/graphql",
                          headers={"Authorization": token.replace("Bearer ", "Bearer "),
                                   "Content-Type": "application/json"},
                          json=q, timeout=20)
        r.raise_for_status()
        out = []
        for ub in (((r.json().get("data") or {}).get("me") or [{}])[0] or {}).get("user_books", []):
            bk = ub.get("book") or {}
            authors = [c.get("author", {}).get("name", "")
                       for c in bk.get("contributions") or []]
            if bk.get("title"):
                out.append({"title": bk["title"], "author": ", ".join(filter(None, authors))})
        return out
    except Exception as e:
        log.warning("hardcover import failed: %s", e)
        return []


def all_for_user() -> list[dict]:
    # Settings-UI values (DB) take precedence over the env defaults, matching the
    # rest of the app — otherwise the in-app reading-list panel would be inert.
    rss = db.setting("goodreads_rss", config.GOODREADS_RSS)
    token = db.setting("hardcover_token", config.HARDCOVER_TOKEN)
    items = []
    for url in [u for u in (rss or "").split(",") if u.strip()]:
        items += goodreads(url.strip())
    items += hardcover(token)
    return items
