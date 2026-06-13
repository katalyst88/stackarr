"""Prowlarr client (optional). Stackarr hands grabs to Chaptarr, which has
its own indexers — so Prowlarr here is used for connection testing and, when
configured, a quick "is there a release out there?" availability check."""
import logging

import requests

from . import config, db

log = logging.getLogger("stackarr.prowlarr")


def url() -> str:
    return db.setting("prowlarr_url", config.PROWLARR_URL).rstrip("/")


def api_key() -> str:
    return db.setting("prowlarr_api_key", config.PROWLARR_API_KEY)


def configured() -> bool:
    return bool(url() and api_key())


def test() -> dict:
    if not configured():
        return {"ok": False, "detail": "Prowlarr not configured"}
    try:
        r = requests.get(f"{url()}/api/v1/system/status",
                         headers={"X-Api-Key": api_key()}, timeout=15)
        if r.ok:
            return {"ok": True, "detail": f"Connected (Prowlarr {r.json().get('version','?')})"}
        return {"ok": False, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def has_release(query: str) -> bool:
    """Best-effort availability hint: does any indexer have a match?"""
    if not configured():
        return False
    try:
        r = requests.get(f"{url()}/api/v1/search", headers={"X-Api-Key": api_key()},
                         params={"query": query, "type": "search"}, timeout=45)
        return bool(r.ok and r.json())
    except Exception:
        return False
