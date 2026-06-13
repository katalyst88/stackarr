"""The recommendation engine. Fully deterministic, no AI: every pick is a
real catalog entry reached by an explainable rule, and carries its reason.

Signals, per seed (a book the user finished / is listening to):
  - series-next     : the next book in a series you're mid-way through   (strongest)
  - sims            : Audible's own "listeners also enjoyed"
  - author-backlist : other books by an author you've read
  - narrator        : other books by narrators you listen to a lot
Plus deterministic modifiers: recency of the seed listen, your 5-star
ratings (boost), Audible average rating (floor + small boost), popularity
dampening (so it isn't only bestsellers), and your negative signals
(passes / DNF / deleted items hard-exclude or de-weight). Results are
de-duplicated by edition, diversity-capped per author, and ranked.
"""
import logging
import math
import re
import time

from . import absclient, audible, audnexus, config, db, discover

log = logging.getLogger("stackarr.recommend")

DRAMATIZED = ("dramatized", "graphic audio", "graphicaudio", "[ga]", "radio play")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _key(title: str, author: str) -> str:
    t = re.sub(r"\s*[:(].*$", "", _norm(title))          # drop subtitle/parenthetical
    return f"{t}|{_norm((author or '').split(',')[0])}"


def _recency_weight(last_update_ms: float, now_ms: float) -> float:
    age_days = max((now_ms - last_update_ms) / 86_400_000, 0.5)
    if age_days < 14:
        return 1.0
    if age_days < 60:
        return 0.7
    if age_days < 180:
        return 0.45
    return 0.25


def _popularity_factor(num_ratings: int) -> float:
    """Gently penalise mega-popular titles so suggestions aren't only
    bestsellers. factor in (0,1]; 1 = no penalty."""
    if num_ratings <= 0 or config.POPULARITY_DAMPEN <= 0:
        return 1.0
    return 1.0 / (1.0 + config.POPULARITY_DAMPEN * math.log10(max(num_ratings, 10)))


def run(user_id: int, max_new: int | None = None) -> int:
    """Generate pending suggestions for one user. Returns count added."""
    max_new = max_new or config.SUGGEST_MAX_PENDING
    user = db.get_user(user_id)
    if not user or not user.get("abs_token"):
        return 0

    seeds = absclient.listening_history(user["abs_token"])
    with db.conn() as c:
        # exclusion + preference state
        known = set()
        for row in c.execute("SELECT title, author FROM library WHERE gone_at IS NULL"):
            known.add(_key(row["title"], row["author"]))
        for tbl in ("requests", "suggestions"):
            for row in c.execute(f"SELECT title, author FROM {tbl} WHERE user_id=?", (user_id,)):
                known.add(_key(row["title"], row["author"]))
        neg = {(s["kind"], s["value"].lower()): s["weight"]
               for s in c.execute("SELECT kind,value,weight FROM signals WHERE user_id=? AND weight<0", (user_id,))}
        pos = {(s["kind"], s["value"].lower()): s["weight"]
               for s in c.execute("SELECT kind,value,weight FROM signals WHERE user_id=? AND weight>0", (user_id,))}
        # 5-star ratings -> per-author/series preference boost
        for r in c.execute("SELECT asin,stars,author FROM ratings WHERE user_id=?", (user_id,)):
            if r["author"]:
                k = ("author", r["author"].split(",")[0].lower())
                pos[k] = pos.get(k, 0) + (r["stars"] - 3) * 1.5   # +3 for 5★, -3 for 1★
        seed_lib = {row["item_id"]: dict(row) for row in
                    c.execute("SELECT item_id,title,author,asin FROM library")}

    # cold-start: thin/no history -> deterministic popular/curated fallback
    if len(seeds) < 2:
        log.info("user %s cold-start (%d seeds) -> discover fallback", user_id, len(seeds))
        cands = {b["asin"]: {"cand": b, "score": b.get("rating") or 3, "lane": "discover",
                             "reason": "Popular right now — listen to a few books and your picks get personal"}
                 for b in discover.popular() if b.get("asin")}
        return _finalize(user_id, cands, known, neg, max_new)

    now_ms = time.time() * 1000
    cands: dict[str, dict] = {}
    narrators_seen: dict[str, float] = {}

    def consider(b: dict, base: float, lane: str, reason: str):
        asin = b.get("asin")
        if not asin or _key(b["title"], b["author"]) in known:
            return
        if any(d in (b["title"] or "").lower() for d in DRAMATIZED):
            return                                          # skip dramatized/GraphicAudio variants
        if (b.get("rating") or 5) < config.SUGGEST_RATING_FLOOR:
            return
        # negative signals -> hard exclude
        first_author = (b["author"] or "").split(",")[0].lower()
        if ("asin", asin.lower()) in neg or ("author", first_author) in neg \
                or ("series", (b.get("series") or "").lower()) in neg:
            return
        score = base
        score += pos.get(("author", first_author), 0)
        score += pos.get(("series", (b.get("series") or "").lower()), 0)
        for nm in (b.get("narrator") or "").split(","):
            score += pos.get(("narrator", nm.strip().lower()), 0) * 0.5
        if b.get("rating"):
            score += (b["rating"] - config.SUGGEST_RATING_FLOOR) * config.W_RATING
        score *= _popularity_factor(b.get("num_ratings", 0))
        cur = cands.get(asin)
        if cur:
            cur["score"] += score                           # frequency across seeds compounds
        else:
            cands[asin] = {"cand": b, "score": score, "lane": lane, "reason": reason}

    for rank, seed in enumerate(seeds[:15]):
        meta = seed_lib.get(seed["item_id"]) or absclient.item_detail(seed["item_id"])
        title, author = meta.get("title", ""), meta.get("author", "")
        if not title:
            continue
        asin = meta.get("asin") or audible.find_asin(title, author)
        rw = _recency_weight(seed["last_update"], now_ms) * (1 + (15 - rank) / 30)

        ax = audnexus.book(asin) if asin else None
        for nm in ((ax or {}).get("narrator") or "").split(","):
            if nm.strip():
                narrators_seen[nm.strip()] = narrators_seen.get(nm.strip(), 0) + rw

        # series-next
        srs = (ax or {}).get("series")
        seq = (ax or {}).get("sequence")
        if srs and seq is not None:
            for b in audible.search(srs, num=20):
                if _norm(b.get("series")) == _norm(srs) and b.get("sequence") == seq + 1:
                    consider(b, config.W_SERIES_NEXT * rw, "series",
                             f"Next in {srs} after “{title}”")
        # author backlist
        if author:
            for b in audible.by_author(author.split(",")[0], num=15):
                consider(b, config.W_AUTHOR_BACKLIST * rw, "foryou",
                         f"More from {author.split(',')[0]}, whom you've listened to")
        # sims
        if seed["finished"] and asin:
            for i, b in enumerate(audible.similar(asin, num=8)):
                consider(b, (config.W_SIMS_FREQ - i * 0.5) * rw, "foryou",
                         f"Listeners who finished “{title}” also enjoyed this")

    # narrator-following lane: top narrators across history
    for nm, wt in sorted(narrators_seen.items(), key=lambda x: x[1], reverse=True)[:3]:
        for b in audible.search(nm, num=10):
            if nm.lower() in (b.get("narrator") or "").lower():
                consider(b, config.W_NARRATOR * wt, "narrator", f"Narrated by {nm}, whom you listen to often")

    return _finalize(user_id, cands, known, neg, max_new)


def _finalize(user_id: int, cands: dict, known: set, neg: dict, max_new: int) -> int:
    # edition dedup by (title, author): keep highest score
    best_by_key: dict[str, dict] = {}
    for entry in cands.values():
        b = entry["cand"]
        k = _key(b["title"], b["author"])
        if k not in best_by_key or entry["score"] > best_by_key[k]["score"]:
            best_by_key[k] = entry
    ranked = sorted(best_by_key.values(), key=lambda x: x["score"], reverse=True)

    # diversity: cap per author
    per_author: dict[str, int] = {}
    added = 0
    with db.conn() as c:
        for entry in ranked:
            if added >= max_new:
                break
            b = entry["cand"]
            a = (b["author"] or "").split(",")[0].lower()
            if per_author.get(a, 0) >= config.SUGGEST_MAX_PER_AUTHOR:
                continue
            cur = c.execute("SELECT changes()")
            c.execute(
                "INSERT OR IGNORE INTO suggestions "
                "(user_id, asin, title, author, narrator, series, cover, reason, lane, score) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (user_id, b["asin"], b["title"], b.get("author", ""), b.get("narrator", ""),
                 b.get("series", ""), b.get("cover", ""), entry["reason"], entry["lane"],
                 round(entry["score"], 2)))
            if c.execute("SELECT changes()").fetchone()[0]:
                per_author[a] = per_author.get(a, 0) + 1
                added += 1
    log.info("user %s: %d candidates -> %d new suggestions", user_id, len(cands), added)
    return added
