"""What a download needs: Chaptarr's raw words → reasons a reader understands.

A request can sit still for many reasons that have nothing to do with
Stackarr: every release is for a tracker's VIP members, the account's ratio
is too low, the torrent was removed from the tracker, nobody is seeding, the
indexer is rate-limiting. Chaptarr already knows — its release rejections
("... requires VIP membership", "Not enough seeders") and its queue messages
(qBittorrent's "unregistered torrent", "stalled with no connections") say so
— but in each tracker's and client's own wording.

`classify(texts)` matches those texts against RULES and returns the needs in
rule order, one per kind: {key, label, text, scope, raw}. `label` is the
short badge word ("Needs VIP"), `text` the sentence for the card / book page.
Nothing here names a tracker: the rules match generic phrases, and an install
adds or overrides rules with a JSON file (STACKARR_NEEDS_RULES, see
config.py) without a code change — same key replaces a built-in rule, a new
key is tried first. Text no rule matches comes back as key "other" with the
raw words, so a reason is never hidden.

`scope` says what clears it, which drives the automatic retry
(releasecheck.py):

  account   the tracker account (VIP, ratio, bonus points, a token). A change
            to Chaptarr's indexer settings re-checks at once; otherwise daily.
  setup     the connection (IP / seedbox registration, client allow-list). A
            change to Chaptarr's indexer or download-client settings re-checks.
  indexer   the indexer itself (down, rate-limited): re-checked hourly.
  release   the releases on offer (none in this format, profile rejects them):
            re-checked daily, as new releases appear.
  swarm     the torrent (no seeders, removed): live from the download client.
"""
import json
import logging
import re

from . import config

log = logging.getLogger("stackarr.needs")

# key, badge label, sentence, scope, retry hours, patterns (case-insensitive
# regexes). Order matters: the first rule that matches a text wins it, so the
# narrow ones ("IP not registered") come before the broad ones ("not
# registered" = torrent removed).
DEFAULT_RULES = [
    {"key": "connectable", "label": "Needs connection set up", "scope": "setup", "retry_hours": 24,
     "text": "The tracker doesn't recognise the connection the download comes from (an IP or seedbox that "
             "isn't registered, or a port that isn't reachable).",
     "patterns": [r"not connectable", r"connectab", r"\bip\b.{0,30}\bnot (?:registered|allowed|whitelisted|authori[sz]ed)",
                  r"(?:unregistered|unknown|unauthori[sz]ed|wrong|dynamic) ip", r"ip mismatch", r"seedbox",
                  r"session.{0,20}(?:expired|invalid)"]},
    {"key": "client", "label": "Needs another client", "scope": "setup", "retry_hours": 24,
     "text": "The tracker won't accept the download client (it isn't on the tracker's allowed list).",
     "patterns": [r"client.{0,20}(?:not (?:allowed|whitelisted|approved|accepted)|banned|blacklisted|rejected)",
                  r"(?:banned|unsupported|unapproved) client", r"client.{0,10}version"]},
    {"key": "vip", "label": "Needs VIP", "scope": "account", "retry_hours": 24,
     "text": "Every release is for the tracker's VIP or higher-rank members.",
     "patterns": [r"\bvip\b", r"\belite\b", r"user ?class", r"(?:rank|class) (?:too low|required)", r"members? only"]},
    {"key": "freeleech", "label": "Needs freeleech", "scope": "account", "retry_hours": 24,
     "text": "Only freeleech releases are allowed, or this one needs a freeleech token (wedge) to download.",
     "patterns": [r"free ?leech", r"\bfl\b", r"wedge", r"personal (?:fl|freeleech)"]},
    {"key": "ratio", "label": "Needs ratio", "scope": "account", "retry_hours": 24,
     "text": "The tracker account's ratio or upload credit is too low, or it has too many unfinished seeding obligations.",
     "patterns": [r"ratio", r"upload (?:credit|buffer)", r"\bbuffer\b", r"download (?:limit|credit)", r"hit.?(?:and|&|n).?run",
                  r"\bh&r\b", r"unsatisfied"]},
    {"key": "bonus", "label": "Needs bonus points", "scope": "account", "retry_hours": 24,
     "text": "The tracker account needs more bonus points for this.",
     "patterns": [r"bonus", r"seed ?points?", r"\bbp\b", r"not enough points", r"points? (?:needed|required)"]},
    {"key": "removed", "label": "Removed from tracker", "scope": "swarm", "retry_hours": 24,
     "text": "The torrent was removed from the tracker (unregistered). A different release has to be grabbed.",
     "patterns": [r"unregistered", r"not registered", r"torrent (?:not found|does not exist|deleted|removed)",
                  r"\btrumped\b", r"\bnuked\b", r"info ?hash not found"]},
    {"key": "no_seeders", "label": "No seeders", "scope": "swarm", "retry_hours": 6,
     "text": "Nobody is sharing this torrent right now, so it can't download. It continues when a seeder returns.",
     "patterns": [r"stall", r"no (?:seeds?|seeders|peers|connections)", r"\b0 seeders?\b", r"not enough seeders",
                  r"minimum seeders", r"dead torrent"]},
    {"key": "indexer_down", "label": "Indexer unavailable", "scope": "indexer", "retry_hours": 1,
     "text": "The indexer is down or limiting searches right now. Stackarr tries again later.",
     "patterns": [r"indexer.{0,40}(?:unavailable|disabled|down|fail|error)", r"rate.?limit", r"too many requests",
                  r"query limit", r"grab limit", r"api limit", r"timed? ?out", r"captcha", r"cloudflare",
                  r"http\D{0,10}(?:429|50[234])\b", r"bad gateway", r"service unavailable"]},
    {"key": "profile", "label": "Doesn't fit profile", "scope": "release", "retry_hours": 24,
     "text": "The releases found don't fit the quality profile (size, format or language).",
     "patterns": [r"\bsize\b", r"quality", r"profile", r"not wanted", r"cutoff", r"minimum", r"maximum", r"language",
                  r"preferred word", r"must (?:not )?contain", r"\bformat\b"]},
]

# A label and sentence for the two needs no text carries.
NO_RELEASE = {"key": "no_release", "scope": "release", "retry_hours": 24}


def _load_rules() -> list[dict]:
    rules = [dict(r) for r in DEFAULT_RULES]
    path = config.NEEDS_RULES_FILE
    if not path:
        return rules
    try:
        with open(path, encoding="utf-8") as f:
            extra = json.load(f)
        if isinstance(extra, dict):
            extra = extra.get("rules") or []
        by_key = {r["key"]: i for i, r in enumerate(rules)}
        new = []
        for r in extra:
            if not isinstance(r, dict) or not r.get("key") or not isinstance(r.get("patterns"), list):
                continue
            for p in r["patterns"]:
                re.compile(p)                           # a bad regex fails the whole file, loudly
            r = {"label": r["key"].replace("_", " ").capitalize(), "text": "", "scope": "release",
                 "retry_hours": 24, **r}
            if r["key"] in by_key:
                rules[by_key[r["key"]]] = r
            else:
                new.append(r)
        return new + rules
    except Exception as e:
        log.warning("needs rules file %s ignored: %s", path, e)
        return [dict(r) for r in DEFAULT_RULES]


RULES = _load_rules()
_COMPILED: list = []


def _compiled() -> list:
    if not _COMPILED:
        _COMPILED.extend((r, [re.compile(p, re.I) for p in r["patterns"]]) for r in RULES)
    return _COMPILED


def reload():
    """Read the rules file again (tests; a changed file)."""
    global RULES
    RULES = _load_rules()
    _COMPILED.clear()


def rule(key: str) -> dict | None:
    return next((r for r in RULES if r["key"] == key), None)


def _need(r: dict, raw: str = "") -> dict:
    return {"key": r["key"], "label": r["label"], "text": r.get("text") or raw, "scope": r.get("scope") or "release",
            "raw": raw[:200]}


def match(text: str) -> dict | None:
    """The rule one raw text matches, or None."""
    t = str(text or "")
    for r, pats in _compiled():
        if any(p.search(t) for p in pats):
            return r
    return None


def classify(texts, limit: int = 4) -> list[dict]:
    """Needs for a list of raw rejection / status texts, in rule order, one per
    key; unmatched texts follow as "other" with their own words."""
    found, other = {}, []
    for t in texts or []:
        t = str(t or "").strip()
        if not t:
            continue
        r = match(t)
        if r is None:
            if t not in other:
                other.append(t)
        elif r["key"] not in found:
            found[r["key"]] = _need(r, t)
    order = [r["key"] for r in RULES]
    out = sorted(found.values(), key=lambda n: order.index(n["key"]))
    out += [{"key": "other", "label": "Needs attention", "text": t, "scope": "release", "raw": t[:200]} for t in other]
    return out[:limit]


def no_release(media: str) -> dict:
    """Nothing on the indexers in this format at all."""
    word = "ebook" if media == "ebook" else "audiobook"
    return {**NO_RELEASE, "label": f"No {word} release",
            "text": f"No {word} release was found on the indexers. Stackarr looks again daily, as new ones appear.",
            "raw": ""}


def retry_hours(needs: list[dict]) -> float:
    """How soon to look again: the soonest any of the needs might clear."""
    hours = [float((rule(n.get("key")) or NO_RELEASE).get("retry_hours") or 24) for n in needs or []]
    return min(hours) if hours else 24.0


def cleared_by_settings(needs: list[dict]) -> bool:
    """Could a change to Chaptarr's indexer / download-client settings clear
    any of these (VIP switched on, a seedbox registered, a new client)?"""
    return any(n.get("scope") in ("account", "setup", "indexer") for n in needs or [])


def badge(needs: list[dict]) -> str:
    """One short label for a status pill: the first need, "+N" for the rest."""
    if not needs:
        return ""
    more = len(needs) - 1
    return needs[0]["label"] + (f" +{more}" if more > 0 else "")
