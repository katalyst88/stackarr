"""Audnexus client (api.audnex.us) — rich audiobook metadata keyed by ASIN:
better series ordering, narrators, genres and ratings than Audible alone.
Used to strengthen series-next detection and the narrator-following lane."""
import logging

import requests

from . import config

log = logging.getLogger("stackarr.audnexus")


def book(asin: str) -> dict | None:
    try:
        r = requests.get(f"{config.AUDNEXUS_API}/books/{asin}",
                         params={"region": config.AUDNEXUS_REGION}, timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        sp = d.get("seriesPrimary") or {}
        seq = None
        try:
            seq = float(sp.get("position"))
        except (TypeError, ValueError):
            seq = None
        return {
            "asin": d.get("asin", asin), "title": d.get("title", ""),
            "author": ", ".join(a.get("name", "") for a in d.get("authors") or []),
            "narrator": ", ".join(n.get("name", "") for n in d.get("narrators") or []),
            "series": sp.get("name", ""), "sequence": seq,
            "series_asin": sp.get("asin", ""),
            "genres": [g.get("name", "") for g in d.get("genres") or []],
            "cover": d.get("image", ""),
            "rating": float(d["rating"]) if d.get("rating") else None,
            "release_date": (d.get("releaseDate") or "")[:10],
            "region": config.AUDNEXUS_REGION,
        }
    except Exception as e:
        log.debug("audnexus book failed for %s: %s", asin, e)
        return None
