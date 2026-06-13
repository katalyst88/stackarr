"""Chaptarr handoff. When a suggestion is approved, Stackarr asks Chaptarr
(the *arr book backend) to add the author/book and search for it. Stackarr
never touches a download client directly — Chaptarr owns grab/import."""
import logging

import requests

from . import config

log = logging.getLogger("stackarr.chaptarr")


def _h():
    return {"X-Api-Key": config.CHAPTARR_API_KEY, "Content-Type": "application/json"}


def configured() -> bool:
    return bool(config.CHAPTARR_URL and config.CHAPTARR_API_KEY)


def monitored_keys() -> set[str]:
    """title|author keys Chaptarr already manages, for dedupe."""
    keys = set()
    try:
        for a in requests.get(f"{config.CHAPTARR_URL}/api/v1/author", headers=_h(), timeout=20).json():
            keys.add((a.get("authorName") or "").lower())
    except Exception as e:
        log.debug("chaptarr monitored_keys failed: %s", e)
    return keys


def add_and_search(title: str, author: str, asin: str = "") -> dict:
    """Ensure the author exists in Chaptarr (added monitored) and kick a
    search. Returns {ok, ref, detail}. Fails gracefully if Chaptarr's
    metadata backend is unavailable."""
    if not configured():
        return {"ok": False, "detail": "Chaptarr not configured"}
    try:
        look = requests.get(f"{config.CHAPTARR_URL}/api/v1/author/lookup",
                            headers=_h(), params={"term": author or title}, timeout=60)
        if look.status_code >= 500:
            return {"ok": False, "detail": "Chaptarr metadata backend unavailable (try later)"}
        results = look.json() if look.ok else []
        if not results:
            return {"ok": False, "detail": f"Chaptarr found no author for '{author or title}'"}
        a = results[0]
        folder = a.get("folder") or a["authorName"]
        a.update(
            mediaType="audiobook", selectedMediaType="audiobook", lastSelectedMediaType="audiobook",
            qualityProfileId=config.CHAPTARR_QUALITY_PROFILE_ID,
            metadataProfileId=config.CHAPTARR_METADATA_PROFILE_ID,
            audiobookQualityProfileId=config.CHAPTARR_QUALITY_PROFILE_ID,
            audiobookMetadataProfileId=config.CHAPTARR_METADATA_PROFILE_ID,
            ebookQualityProfileId=1, ebookMetadataProfileId=2,
            rootFolderPath=config.CHAPTARR_ROOT_FOLDER,
            path=f"{config.CHAPTARR_ROOT_FOLDER.rstrip('/')}/{folder}",
            monitored=True, monitorNewItems="all",
            addOptions={"monitor": "all", "searchForMissingBooks": True})
        r = requests.post(f"{config.CHAPTARR_URL}/api/v1/author", headers=_h(), json=a, timeout=90)
        if r.ok:
            return {"ok": True, "ref": str(r.json().get("id", "")),
                    "detail": f"Added {a['authorName']} to Chaptarr; searching"}
        return {"ok": False, "detail": f"Chaptarr add failed: {r.status_code} {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "detail": f"Chaptarr error: {e}"}
