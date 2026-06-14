"""Chaptarr handoff. When a suggestion is approved, Stackarr asks Chaptarr
(the *arr book backend) to add the author/book and search for it. Stackarr
never touches a download client directly — Chaptarr owns grab/import."""
import logging
import re

import requests

from . import config, db

log = logging.getLogger("stackarr.chaptarr")


def url() -> str:
    return db.setting("chaptarr_url", config.CHAPTARR_URL).rstrip("/")


def api_key() -> str:
    return db.setting("chaptarr_api_key", config.CHAPTARR_API_KEY)


def root_folder() -> str:
    rf = db.setting("chaptarr_root_folder", config.CHAPTARR_ROOT_FOLDER)
    # Guard against Git-Bash path mangling (a unix /path passed through Git Bash
    # at container-create time becomes "C:/Program Files/Git/path"). Recover the
    # real container path so Chaptarr doesn't reject it as an invalid path.
    m = re.search(r"/Git(/.*)$", rf)
    if rf.startswith("C:") and "Program Files/Git" in rf and m:
        rf = m.group(1)
    return rf


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
    """author names Chaptarr already manages, lowercased, for dedupe."""
    keys = set()
    try:
        for a in requests.get(f"{url()}/api/v1/author", headers=_h(), timeout=20).json():
            keys.add((a.get("authorName") or "").lower())
    except Exception as e:
        log.debug("chaptarr monitored_keys failed: %s", e)
    return keys


def health() -> list[dict]:
    """Chaptarr's own health warnings (e.g. the api.chaptarr.com metadata
    outage) — surfaced in Stackarr's connection test so degraded metadata is
    visible. Each: {type, message}."""
    try:
        r = requests.get(f"{url()}/api/v1/health", headers=_h(), timeout=15)
        return [{"type": h.get("type", ""), "message": h.get("message", "")} for h in r.json()] if r.ok else []
    except Exception:
        return []


def can_grab(title: str, author: str) -> bool:
    """Cheap pre-check: does Chaptarr's catalogue know this author/title? Lets
    the UI flag 'no releases yet' before queueing a dead-end."""
    if not configured():
        return False
    try:
        r = requests.get(f"{url()}/api/v1/author/lookup", headers=_h(),
                         params={"term": author or title}, timeout=30)
        return bool(r.ok and r.json())
    except Exception:
        return False


def queue_status() -> dict:
    """Live download state keyed by lowercased title — {title: status} where
    status is downloading | importing | queued. Drives real request progress."""
    out = {}
    try:
        r = requests.get(f"{url()}/api/v1/queue", headers=_h(),
                         params={"pageSize": 200, "includeAuthor": "true", "includeBook": "true"}, timeout=20)
        for rec in (r.json() or {}).get("records", []) if r.ok else []:
            title = ((rec.get("book") or {}).get("title")
                     or (rec.get("title") or "")).lower().strip()
            if not title:
                continue
            st = (rec.get("status") or "").lower()
            tdl = (rec.get("trackedDownloadState") or "").lower()
            if "import" in tdl or st == "completed":
                out[title] = "importing"
            elif st in ("downloading", "queued", "paused", "delay"):
                out[title] = "downloading"
            else:
                out[title] = st or "downloading"
    except Exception as e:
        log.debug("chaptarr queue_status failed: %s", e)
    return out


def _ensure_tag(label: str = "stackarr") -> int | None:
    """Ensure a Chaptarr tag exists; return its id (so Stackarr-added books are
    tagged for easy filtering in Chaptarr). Best-effort."""
    try:
        for t in requests.get(f"{url()}/api/v1/tag", headers=_h(), timeout=15).json():
            if (t.get("label") or "").lower() == label:
                return t.get("id")
        r = requests.post(f"{url()}/api/v1/tag", headers=_h(), json={"label": label}, timeout=15)
        return r.json().get("id") if r.ok else None
    except Exception:
        return None


def add_and_search(title: str, author: str, asin: str = "", fmt: str = "audiobook",
                   root_folder_override: str = "") -> dict:
    """Ensure the author exists in Chaptarr (added monitored, tagged 'stackarr')
    and kick a search. `fmt` (audiobook | ebook) decides the media type +
    profiles. `root_folder_override` lets a request pick its destination.
    Returns {ok, ref, detail}. Fails gracefully if Chaptarr is unavailable."""
    if not configured():
        return {"ok": False, "detail": "Stackarr isn't connected to Chaptarr yet — add it in Settings → Connections."}
    # Audiobook + ebook each have their own quality/metadata profile pair in
    # Chaptarr; the active media type's pair becomes the author's primary.
    ab_qp = _profile("chaptarr_quality_profile_id", config.CHAPTARR_QUALITY_PROFILE_ID)
    ab_mp = _profile("chaptarr_metadata_profile_id", config.CHAPTARR_METADATA_PROFILE_ID)
    eb_qp = _profile("chaptarr_ebook_quality_profile_id", config.CHAPTARR_EBOOK_QUALITY_PROFILE_ID)
    eb_mp = _profile("chaptarr_ebook_metadata_profile_id", config.CHAPTARR_EBOOK_METADATA_PROFILE_ID)
    media = "ebook" if fmt == "ebook" else "audiobook"
    qp, mp = (eb_qp, eb_mp) if media == "ebook" else (ab_qp, ab_mp)
    rf = (root_folder_override or root_folder()).rstrip("/") or root_folder()
    tag_id = _ensure_tag("stackarr")
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
            mediaType=media, selectedMediaType=media, lastSelectedMediaType=media,
            qualityProfileId=qp, metadataProfileId=mp,
            audiobookQualityProfileId=ab_qp, audiobookMetadataProfileId=ab_mp,
            ebookQualityProfileId=eb_qp, ebookMetadataProfileId=eb_mp,
            rootFolderPath=rf, path=f"{rf.rstrip('/')}/{folder}",
            tags=([tag_id] if tag_id else []),
            monitored=True, monitorNewItems="all",
            addOptions={"monitor": "all", "searchForMissingBooks": True})
        r = requests.post(f"{url()}/api/v1/author", headers=_h(), json=a, timeout=90)
        if r.ok:
            return {"ok": True, "ref": str(r.json().get("id", "")),
                    "detail": f"Sent “{a['authorName']}” to Chaptarr — it's searching now."}
        body = r.text or ""
        if r.status_code in (502, 503) or "V5 API" in body or "author info" in body:
            return {"ok": False, "detail": "Chaptarr's metadata service (api.chaptarr.com) is down, so it can't add "
                    "books right now — this is a Chaptarr outage, not Stackarr. We've kept your pick; retry when it's back."}
        if "Invalid Path" in body:
            return {"ok": False, "detail": "Chaptarr rejected the root folder path — check Settings → Connections → "
                    "Chaptarr root folder matches a path that exists inside the Chaptarr container."}
        return {"ok": False, "detail": "Chaptarr couldn't add this one right now — please try again in a bit."}
    except Exception:
        return {"ok": False, "detail": "Couldn't reach Chaptarr — check it's running and connected in Settings."}
