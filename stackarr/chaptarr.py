"""Chaptarr handoff. When a suggestion is approved, Stackarr asks Chaptarr
(the *arr book backend) to add the author/book and search for it. Stackarr
never touches a download client directly — Chaptarr owns grab/import."""
import logging

import requests

from . import config, db

log = logging.getLogger("stackarr.chaptarr")


def url() -> str:
    return db.setting("chaptarr_url", config.CHAPTARR_URL).rstrip("/")


def api_key() -> str:
    return db.setting("chaptarr_api_key", config.CHAPTARR_API_KEY)


def root_folder() -> str:
    return db.setting("chaptarr_root_folder", config.CHAPTARR_ROOT_FOLDER)


def _profile(key: str, fallback: int) -> int:
    try:
        return int(db.setting(key, str(fallback)))
    except ValueError:
        return fallback


def _h():
    return {"X-Api-Key": api_key(), "Content-Type": "application/json"}


def configured() -> bool:
    return bool(url() and api_key())


def monitored_keys() -> set[str]:
    """title|author keys Chaptarr already manages, for dedupe."""
    keys = set()
    try:
        for a in requests.get(f"{url()}/api/v1/author", headers=_h(), timeout=20).json():
            keys.add((a.get("authorName") or "").lower())
    except Exception as e:
        log.debug("chaptarr monitored_keys failed: %s", e)
    return keys


def add_and_search(title: str, author: str, asin: str = "") -> dict:
    """Ensure the author exists in Chaptarr (added monitored) and kick a
    search. Returns {ok, ref, detail}. Fails gracefully if Chaptarr's
    metadata backend is unavailable."""
    if not configured():
        return {"ok": False, "detail": "Stackarr isn't connected to Chaptarr yet — add it in Settings → Connections."}
    qp = _profile("chaptarr_quality_profile_id", config.CHAPTARR_QUALITY_PROFILE_ID)
    mp = _profile("chaptarr_metadata_profile_id", config.CHAPTARR_METADATA_PROFILE_ID)
    rf = root_folder()
    try:
        look = requests.get(f"{url()}/api/v1/author/lookup",
                            headers=_h(), params={"term": author or title}, timeout=60)
        if look.status_code >= 500:
            return {"ok": False, "detail": "Chaptarr's book database is offline right now — we've kept this; try again shortly."}
        results = look.json() if look.ok else []
        if not results:
            return {"ok": False, "detail": f"Chaptarr couldn't find “{author or title}” in its catalogue."}
        a = results[0]
        folder = a.get("folder") or a["authorName"]
        a.update(
            mediaType="audiobook", selectedMediaType="audiobook", lastSelectedMediaType="audiobook",
            qualityProfileId=qp, metadataProfileId=mp,
            audiobookQualityProfileId=qp, audiobookMetadataProfileId=mp,
            ebookQualityProfileId=1, ebookMetadataProfileId=2,
            rootFolderPath=rf, path=f"{rf.rstrip('/')}/{folder}",
            monitored=True, monitorNewItems="all",
            addOptions={"monitor": "all", "searchForMissingBooks": True})
        r = requests.post(f"{url()}/api/v1/author", headers=_h(), json=a, timeout=90)
        if r.ok:
            return {"ok": True, "ref": str(r.json().get("id", "")),
                    "detail": f"Sent “{a['authorName']}” to Chaptarr — it's searching now."}
        return {"ok": False, "detail": "Chaptarr couldn't add this one right now — please try again in a bit."}
    except Exception:
        return {"ok": False, "detail": "Couldn't reach Chaptarr — check it's running and connected in Settings."}
