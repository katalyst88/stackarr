"""Audible catalog client. Public catalog API (no account, no key, no AI):
search, product-by-ASIN, and the /sims "listeners also enjoyed" endpoint
that drives a chunk of the recommendations."""
import logging

import requests

from . import config

log = logging.getLogger("stackarr.audible")

GROUPS = "contributors,media,product_attrs,product_desc,series,category_ladders,rating"


def _products(params: dict) -> list[dict]:
    try:
        r = requests.get(f"{config.AUDIBLE_API}/catalog/products", params=params, timeout=20)
        r.raise_for_status()
        return r.json().get("products", [])
    except Exception as e:
        log.warning("audible query failed: %s", e)
        return []


ROLE_NOISE = ("translator", "illustrator", "editor", "- contributor", "foreword",
              "introduction", "adapted", "afterword")


def _clean_contributors(people: list) -> str:
    """Drop translators/illustrators/editors and any name carrying a role
    suffix, so authors/narrators are the real ones (rreading-glasses lesson)."""
    out = []
    for x in people or []:
        nm = (x.get("name") or "").strip()
        if nm and not any(r in nm.lower() for r in ROLE_NOISE):
            out.append(nm.split(" - ")[0].strip())
    return ", ".join(dict.fromkeys(out))          # de-dup, keep order


def normalize(p: dict) -> dict:
    authors = _clean_contributors(p.get("authors"))
    narrators = _clean_contributors(p.get("narrators"))
    img = ""
    images = p.get("product_images") or {}
    for size in ("500", "256", "1024"):
        if images.get(size):
            img = images[size]
            break
    series, seq = "", None
    for s in p.get("series") or []:
        series = s.get("title") or ""
        try:
            seq = float(s.get("sequence"))
        except (TypeError, ValueError):
            seq = None
        break
    minutes = p.get("runtime_length_min") or 0
    rating, num_ratings = None, 0
    dist = (p.get("rating") or {}).get("overall_distribution") or {}
    try:
        rating = round(float(dist.get("average_rating") or 0), 2) or None
        num_ratings = int(dist.get("num_ratings") or 0)
    except (TypeError, ValueError):
        pass
    return {
        "asin": p.get("asin", ""), "title": p.get("title", ""),
        "subtitle": p.get("subtitle") or "", "author": authors, "narrator": narrators,
        "cover": img, "series": series, "sequence": seq,
        "release_date": (p.get("release_date") or "")[:10],
        "runtime_hours": round(minutes / 60, 1) if minutes else None,
        "rating": rating, "num_ratings": num_ratings,
        "summary": (p.get("merchandising_summary") or "")[:600],
        "categories": [c.get("name", "") for ladder in p.get("category_ladders") or []
                       for c in ladder.get("ladder", [])],
        "language": (p.get("language") or "").lower(),
    }


def search(query: str, num: int = 12, page: int = 0) -> list[dict]:
    return [normalize(p) for p in _products({
        "keywords": query, "num_results": num, "page": page, "products_sort_by": "Relevance",
        "response_groups": GROUPS}) if p.get("title")]


def by_author(author: str, num: int = 25) -> list[dict]:
    return [normalize(p) for p in _products({
        "author": author, "num_results": num, "products_sort_by": "-ReleaseDate",
        "response_groups": GROUPS}) if p.get("title")]


def by_asin(asin: str) -> dict | None:
    try:
        r = requests.get(f"{config.AUDIBLE_API}/catalog/products/{asin}",
                         params={"response_groups": GROUPS}, timeout=20)
        r.raise_for_status()
        p = r.json().get("product")
        return normalize(p) if p else None
    except Exception as e:
        log.warning("audible asin lookup failed for %s: %s", asin, e)
        return None


def similar(asin: str, num: int = 10) -> list[dict]:
    """Audible's own 'listeners also enjoyed'. Falls back to author-search
    (minus the seed) when /sims is unavailable, which it intermittently is."""
    try:
        r = requests.get(f"{config.AUDIBLE_API}/catalog/products/{asin}/sims",
                         params={"num_results": num, "response_groups": GROUPS}, timeout=20)
        r.raise_for_status()
        sims = [normalize(p) for p in r.json().get("similar_products", []) if p.get("title")]
        if sims:
            return sims
    except Exception as e:
        log.debug("audible sims failed for %s: %s", asin, e)
    seed = by_asin(asin)
    if seed and seed["author"]:
        return [b for b in by_author(seed["author"].split(",")[0], num) if b["asin"] != asin]
    return []


def find_asin(title: str, author: str) -> str:
    hits = _products({"title": title, "author": author, "num_results": 1,
                      "response_groups": "product_attrs"})
    return hits[0].get("asin", "") if hits else ""
