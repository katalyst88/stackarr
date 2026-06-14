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
import datetime
import logging
import math
import re
import time

from . import (absclient, audible, audnexus, config, db, discover, importlists,
               tagging, taste)

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
    """Generate pending suggestions for one user. Returns count added.
    `max_new=None` means 'use the default cap'; an explicit 0 means 'no room'."""
    if max_new is None:
        max_new = config.SUGGEST_MAX_PENDING
    if max_new <= 0:
        return 0
    user = db.get_user(user_id)
    if not user or not user.get("abs_token"):
        return 0

    seeds = absclient.listening_history(user["abs_token"])
    # Taste is format-isolated by default: audiobook picks dedupe only against
    # owned/suggested/requested AUDIOBOOKS, and only audiobook ratings boost
    # authors — so a title you own/like as an ebook can still be suggested as an
    # audiobook (and vice versa). The cross_format_taste setting opts into
    # sharing ratings across formats.
    xfmt = db.get_pref(user_id, "cross_format_taste", "0") == "1"
    # format isolation (unless cross-taste is on): only this format's ratings AND
    # signals shape audiobook picks, so a pass/DNF on an ebook doesn't suppress it.
    rate_where = "" if xfmt else " AND (format='audiobook' OR format IS NULL OR format='')"
    sig_where = rate_where
    with db.conn() as c:
        # exclusion + preference state (audiobook-scoped ownership/dedup)
        known = set()
        for row in c.execute("SELECT title, author FROM library WHERE gone_at IS NULL "
                             "AND (format='audiobook' OR format IS NULL OR format='')"):
            known.add(_key(row["title"], row["author"]))
        for tbl in ("requests", "suggestions"):
            for row in c.execute(f"SELECT title, author FROM {tbl} WHERE user_id=? "
                                 "AND (format='audiobook' OR format IS NULL OR format='')", (user_id,)):
                known.add(_key(row["title"], row["author"]))
        neg = {(s["kind"], s["value"].lower()): s["weight"]
               for s in c.execute(f"SELECT kind,value,weight FROM signals WHERE user_id=? AND weight<0{sig_where}", (user_id,))}
        pos = {(s["kind"], s["value"].lower()): s["weight"]
               for s in c.execute(f"SELECT kind,value,weight FROM signals WHERE user_id=? AND weight>0{sig_where}", (user_id,))}
        # 5-star ratings -> per-author/series preference boost (format-scoped)
        for r in c.execute(f"SELECT asin,stars,author FROM ratings WHERE user_id=?{rate_where}", (user_id,)):
            if r["author"]:
                k = ("author", r["author"].split(",")[0].lower())
                pos[k] = pos.get(k, 0) + (r["stars"] - 3) * 1.5
        # light household collaborative signal: authors other users in this
        # household rate highly get a gentle nudge (no-op on single-user installs)
        for r in c.execute(f"SELECT author, COUNT(*) n FROM ratings WHERE user_id<>? AND stars>=4 "
                           f"AND author<>''{rate_where} GROUP BY lower(substr(author,1,40))", (user_id,)):
            k = ("author", r["author"].split(",")[0].lower())
            pos[k] = pos.get(k, 0) + min(r["n"], 3) * 0.5   # +3 for 5★, -3 for 1★
        seed_lib = {row["item_id"]: dict(row) for row in
                    c.execute("SELECT item_id,title,author,asin FROM library")}
        # books the user removed from History & ratings must not seed suggestions
        hidden = {row["value"] for row in c.execute(
            "SELECT value FROM signals WHERE user_id=? AND kind='hist_hidden'", (user_id,))}

    if hidden:
        def _hidden_seed(s):
            m = seed_lib.get(s["item_id"]) or {}
            return db.rating_key(m.get("asin", ""), m.get("title", ""), m.get("author", "")) in hidden
        seeds = [s for s in seeds if not _hidden_seed(s)]

    # authors *this user* has actually listened to (NOT the whole server library —
    # that would pull in other people's / kids' libraries). Seeds only.
    read_authors = set()
    for s in seeds:
        m = seed_lib.get(s["item_id"])
        if m and m.get("author"):
            read_authors.add(m["author"].split(",")[0].strip().lower())

    # cold-start: thin/no history -> deterministic popular/curated fallback
    if len(seeds) < 2:
        log.info("user %s cold-start (%d seeds) -> discover fallback", user_id, len(seeds))
        cands = {b["asin"]: {"cand": b, "score": b.get("rating") or 3, "lane": "discover", "extra": "",
                             "reason": "Popular right now — listen to a few books and your picks get personal"}
                 for b in discover.popular() if b.get("asin")}
        return _finalize(user_id, cands, known, neg, max_new)

    now_ms = time.time() * 1000
    target_lang = db.get_meta("language", config.TARGET_LANGUAGE)   # user-set; "any" disables filter
    cands: dict[str, dict] = {}
    narrators_seen: dict[str, float] = {}
    genres_seen: dict[str, float] = {}
    # --- deterministic taste profile (no AI): explicit mood prefs (vibe picker /
    # feedback) + moods derived from the genres of the books you've read. Drives
    # mood matching + the serendipity bonus + the adventurousness dial.
    mood_profile = taste.mood_signals(user_id)   # moods are cross-format by design
    adv = taste.adventurousness(user_id)
    familiar_mult, discover_mult = taste.adv_multipliers(user_id)

    def consider(b: dict, base: float, lane: str, reason: str, extra: str = "", floor: bool = True):
        asin = b.get("asin")
        if not asin or _key(b["title"], b["author"]) in known:
            return
        if any(d in (b["title"] or "").lower() for d in DRAMATIZED):
            return                                          # skip dramatized/GraphicAudio variants
        if target_lang != "any" and (b.get("language") or "english").lower() != target_lang:
            return                                          # skip non-target-language editions
        if floor and (b.get("rating") or 5) < config.SUGGEST_RATING_FLOOR:
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
        # mood/pace match with the user's taste profile (can be negative for
        # disliked moods) + serendipity bonus for well-rated lesser-known books
        score += config.W_MOOD * taste.mood_overlap(b.get("categories"), mood_profile) * 0.15
        score += config.W_SERENDIPITY * taste.serendipity(b, adv)
        score *= _popularity_factor(b.get("num_ratings", 0))
        cur = cands.get(asin)
        if cur:
            cur["score"] += score                           # frequency across seeds compounds
        else:
            cands[asin] = {"cand": b, "score": score, "lane": lane, "reason": reason, "extra": extra}

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
        seed_genres = (ax or {}).get("genres") or []
        for g in seed_genres:
            if g:
                genres_seen[g] = genres_seen.get(g, 0) + rw
        # accrue the moods of what you actually read into the taste profile
        for m in taste.candidate_moods(seed_genres):
            mood_profile[m.lower()] = mood_profile.get(m.lower(), 0.0) + rw

        # series-next  -> "Series to finish"
        srs = (ax or {}).get("series")
        seq = (ax or {}).get("sequence")
        if srs and seq is not None:
            for b in audible.search(srs, num=20):
                if _norm(b.get("series")) == _norm(srs) and b.get("sequence") == seq + 1:
                    consider(b, config.W_SERIES_NEXT * rw * familiar_mult, "series",
                             f"Next in {srs} after “{title}”")
        # author backlist -> "More from authors you love"
        if author:
            for b in audible.by_author(author.split(",")[0], num=15):
                consider(b, config.W_AUTHOR_BACKLIST * rw * familiar_mult, "author",
                         f"More from {author.split(',')[0]}, an author you love")
        # sims -> "Readers also enjoyed" (known author) or "New authors to discover"
        if seed["finished"] and asin:
            for i, b in enumerate(audible.similar(asin, num=8)):
                a1 = (b.get("author") or "").split(",")[0].strip().lower()
                if a1 and a1 in read_authors:
                    consider(b, (config.W_SIMS_FREQ - i * 0.5) * rw, "enjoyed",
                             f"Readers who finished “{title}” also enjoyed this")
                else:
                    consider(b, (config.W_SIMS_FREQ - i * 0.5) * rw * 0.9 * discover_mult, "discover_author",
                             f"A new author for you — fans of “{title}” rate this highly")

    # narrator-following -> "Narrators you love"
    for nm, wt in sorted(narrators_seen.items(), key=lambda x: x[1], reverse=True)[:3]:
        for b in audible.search(nm, num=10):
            if nm.lower() in (b.get("narrator") or "").lower():
                consider(b, config.W_NARRATOR * wt, "narrator", f"Narrated by {nm}, whom you listen to often")

    top_genres = [g for g, _ in sorted(genres_seen.items(), key=lambda x: x[1], reverse=True)[:2]]
    today = str(datetime.date.today())

    # mood lane -> books that match your strongest reading moods (StoryGraph-style)
    MOOD_TERMS = {"funny": "humorous fantasy", "dark": "grimdark fantasy", "romantic": "romantasy",
                  "tense": "psychological thriller", "adventurous": "adventure fantasy",
                  "reflective": "literary science fiction", "epic": "epic fantasy", "cozy": "cozy fantasy",
                  "emotional": "emotional fiction", "whimsical": "whimsical fantasy", "mysterious": "mystery",
                  "fast-paced": "fast-paced thriller", "slow-paced": "literary fiction"}
    top_moods = [m for m, w in sorted(mood_profile.items(), key=lambda x: x[1], reverse=True)[:2] if w > 0]
    for mood in top_moods:
        term = MOOD_TERMS.get(mood)
        if term:
            for b in audible.search(term, num=10):
                consider(b, config.W_MOOD, "mood", f"A {mood} read — a mood you gravitate to")

    # genre lane -> "More in your favourite genres"
    for g in top_genres:
        for b in discover.genre_new([g], num_per=8)[:10]:
            consider(b, config.W_RATING * 1.2, "genre", f"Popular in {g}, a genre you enjoy")

    # hidden gems / off the beaten path -> well-rated but not mega-popular. The
    # serendipity bonus (in consider) + adventurousness amplify these.
    for g in top_genres:
        for b in audible.search(g, num=25):
            if (b.get("rating") or 0) >= 4.3 and 15 <= b.get("num_ratings", 0) <= 5000:
                consider(b, config.W_RATING * discover_mult, "hidden",
                         f"Off the beaten path — a highly-rated, lesser-known {g.lower()}")

    # award winners in your genres
    for g in top_genres:
        for b in audible.search(f"{g} award winning", num=8):
            consider(b, config.W_RATING, "awards", f"Award-winning {g.lower()}")

    # short / epic listens
    for g in top_genres[:1]:
        for b in audible.search(g, num=25):
            h = b.get("runtime_hours") or 0
            if 0 < h <= 6:
                consider(b, config.W_RATING * 0.8, "short", f"A short listen ({h}h) in {g}")
            elif h >= 20:
                consider(b, config.W_RATING * 0.8, "epic", f"An epic listen ({h:.0f}h) in {g}")

    # new & upcoming from authors you read — both not-yet-released AND recently
    # released (last ~120 days) so the Upcoming page is well-stocked.
    recent_cut = str(datetime.date.today() - datetime.timedelta(days=120))
    for a in list(read_authors)[:18]:
        for b in audible.by_author(a, num=10):
            rd = b.get("release_date") or ""
            if rd and rd >= recent_cut:
                label = (f"Coming {rd}" if rd > today else f"Newly out ({rd})")
                consider(b, config.W_AUTHOR_BACKLIST, "upcoming",
                         f"{label} from {(b['author'] or a).split(',')[0]}", extra=rd, floor=False)

    # from your reading list (Goodreads / Hardcover "want to read")
    for it in importlists.all_for_user():
        hit = audible.search(f"{it['title']} {it.get('author','')}", num=1)
        if hit:
            consider(hit[0], config.W_RATING * 1.5, "importlist",
                     "On your reading list" + (f" — {it['author']}" if it.get("author") else ""))

    return _finalize(user_id, cands, known, neg, max_new)


def _finalize(user_id: int, cands: dict, known: set, neg: dict, max_new: int,
              fmt: str = "audiobook") -> int:
    """Edition-dedup, then keep the top N PER LANE (author-diverse) so every
    category is represented rather than one lane crowding out the rest.
    `fmt` stamps the suggestion's media format (audiobook | ebook)."""
    from collections import defaultdict
    best_by_key: dict[str, dict] = {}
    for entry in cands.values():
        b = entry["cand"]
        k = _key(b["title"], b["author"])
        if k not in best_by_key or entry["score"] > best_by_key[k]["score"]:
            best_by_key[k] = entry

    by_lane = defaultdict(list)
    for entry in best_by_key.values():
        by_lane[entry["lane"]].append(entry)

    per_lane = config.SUGGEST_PER_LANE
    added = 0
    with db.conn() as c:
        for lane, entries in by_lane.items():
            if added >= max_new:           # honour the caller's overall cap
                break
            entries.sort(key=lambda x: x["score"], reverse=True)
            per_author: dict[str, int] = {}
            taken = 0
            for entry in entries:
                if taken >= per_lane or added >= max_new:
                    break
                b = entry["cand"]
                a = (b["author"] or "").split(",")[0].lower()
                if per_author.get(a, 0) >= config.SUGGEST_MAX_PER_AUTHOR:
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO suggestions "
                    "(user_id, asin, title, author, narrator, series, cover, reason, lane, score, extra, format) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, b["asin"], b["title"], b.get("author", ""), b.get("narrator", ""),
                     b.get("series", ""), b.get("cover", ""), entry["reason"], entry["lane"],
                     round(entry["score"], 2), entry.get("extra", ""), fmt))
                if c.execute("SELECT changes()").fetchone()[0]:
                    per_author[a] = per_author.get(a, 0) + 1
                    taken += 1
                    added += 1
    log.info("user %s: %d candidates -> %d new across %d lanes", user_id, len(cands), added, len(by_lane))
    return added
