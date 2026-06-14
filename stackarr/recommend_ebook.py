"""The ebook recommendation engine — the ebook counterpart of recommend.py.

Same deterministic, no-AI philosophy: every pick is a real catalogue entry
reached by an explainable rule. Seeds come from the connected ebook sources'
reading history (Kavita progress, Calibre-Web read flags) plus the Hardcover
*read* shelf, joined to the library snapshot for titles. Candidates are pulled
from the ebook metadata module (Google Books + Open Library) and pushed through
recommend._finalize stamped format='ebook'.

Ebooks have no narrator and no reliable "listeners also enjoyed" graph, so the
lanes are a subset: author-backlist, subject-similar ("readers also enjoyed"),
your reading list, and a genre/popular fallback. Series-next is best-effort
(ebook catalogues rarely expose structured series order)."""
import logging

from . import (backends, config, db, ebookmeta, importlists, recommend, taste)

log = logging.getLogger("stackarr.recommend_ebook")

_key = recommend._key
_norm = recommend._norm


def _ebook_seeds(user: dict) -> list[dict]:
    """Reading history across every connected ebook source, joined to the
    library for title/author, newest first. Each: {title, author, finished}."""
    raw = []
    for b in backends.sources("ebook"):
        try:
            raw += b.reading_history(user)
        except Exception as e:
            log.warning("ebook reading_history failed for %s: %s", b.id, e)
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in
               c.execute("SELECT item_id,title,author FROM library WHERE format='ebook'")}
    seeds, seen = [], set()
    for h in sorted(raw, key=lambda x: x.get("last_update", 0), reverse=True):
        m = lib.get(h["item_id"]) or {}
        title = m.get("title", "")
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        seeds.append({"title": title, "author": m.get("author", ""),
                      "finished": bool(h.get("finished"))})
    # Hardcover shelves — cross-device ebooks ABS/Kavita never saw. Read =
    # finished seed; Currently-reading = strong in-progress seed.
    tok = ebookmeta.hardcover_token()
    for it in ebookmeta.hardcover_read(tok):
        t = (it.get("title") or "")
        if t and t.lower() not in seen:
            seen.add(t.lower())
            seeds.append({"title": t, "author": it.get("author", ""), "finished": True})
    for it in ebookmeta.hardcover_reading(tok):
        t = (it.get("title") or "")
        if t and t.lower() not in seen:
            seen.add(t.lower())
            seeds.append({"title": t, "author": it.get("author", ""), "finished": False})
    return seeds


def run(user_id: int, max_new: int | None = None) -> int:
    """Generate pending ebook suggestions for one user. Returns count added.
    `max_new=None` means 'use the default cap'; an explicit 0 means 'no room'."""
    if max_new is None:
        max_new = config.SUGGEST_MAX_PENDING
    if max_new <= 0:
        return 0
    user = db.get_user(user_id)
    if not user:
        return 0

    seeds = _ebook_seeds(user)

    # Format-isolated by default: ebook picks dedupe only against owned/suggested/
    # requested EBOOKS (so a book you own/like as an audiobook can still be
    # suggested as an ebook — "give me the option for either"), and only ebook
    # ratings inform author boost. cross_format_taste opts into sharing.
    xfmt = db.get_pref(user_id, "cross_format_taste", "0") == "1"
    rate_where = "" if xfmt else " AND format='ebook'"
    # signals: keep ebook-specific AND format-agnostic ones (a plain "ignore"),
    # only drop audiobook-specific signals so a DNF there doesn't suppress ebooks.
    sig_where = "" if xfmt else " AND (format='ebook' OR format IS NULL OR format='')"
    with db.conn() as c:
        known = set()
        for row in c.execute("SELECT title, author FROM library WHERE gone_at IS NULL AND format='ebook'"):
            known.add(_key(row["title"], row["author"]))
        for tbl in ("requests", "suggestions"):
            for row in c.execute(f"SELECT title, author FROM {tbl} WHERE user_id=? AND format='ebook'", (user_id,)):
                known.add(_key(row["title"], row["author"]))
        neg = {(s["kind"], s["value"].lower()): s["weight"]
               for s in c.execute(f"SELECT kind,value,weight FROM signals WHERE user_id=? AND weight<0{sig_where}", (user_id,))}
        pos = {(s["kind"], s["value"].lower()): s["weight"]
               for s in c.execute(f"SELECT kind,value,weight FROM signals WHERE user_id=? AND weight>0{sig_where}", (user_id,))}
        for r in c.execute(f"SELECT stars,author FROM ratings WHERE user_id=?{rate_where}", (user_id,)):
            if r["author"]:
                k = ("author", r["author"].split(",")[0].lower())
                pos[k] = pos.get(k, 0) + (r["stars"] - 3) * 1.5

    target_lang = db.get_meta("language", config.TARGET_LANGUAGE)
    mood_profile = taste.mood_signals(user_id)   # moods are cross-format by design
    adv = taste.adventurousness(user_id)
    cands: dict[str, dict] = {}

    def consider(b: dict, base: float, lane: str, reason: str):
        bid = b.get("id") or b.get("asin")
        if not bid:
            return
        if _key(b.get("title", ""), b.get("author", "")) in known:
            return
        # only ever suggest the first book of a series (see recommend.py); ebook
        # catalogues rarely expose sequence, so this only fires when it's known.
        # 'importlist' is exempt — a book you explicitly want-to-read is deliberate.
        if lane not in ("importlist",) and b.get("series"):
            seq = b.get("sequence")
            try:
                if seq is not None and float(seq) > 1:
                    return
            except (TypeError, ValueError):
                pass
        # ebook languages are ISO codes (en/de/…); map the user's word to a code
        lang = (b.get("language") or "").lower()
        want = {"english": "en", "german": "de", "spanish": "es", "french": "fr",
                "italian": "it", "dutch": "nl", "portuguese": "pt", "japanese": "ja"}.get(target_lang)
        if target_lang != "any" and want and lang and lang != want:
            return
        first_author = (b.get("author") or "").split(",")[0].lower()
        if ("author", first_author) in neg or ("asin", bid.lower()) in neg:
            return
        score = base + pos.get(("author", first_author), 0)
        if b.get("rating"):
            score += (b["rating"] - config.SUGGEST_RATING_FLOOR) * config.W_RATING
        score += config.W_MOOD * taste.mood_overlap(b.get("categories"), mood_profile) * 0.15
        score += config.W_SERENDIPITY * taste.serendipity(b, adv)
        b = {**b, "asin": bid, "narrator": ""}      # pipeline keys on asin
        cur = cands.get(bid)
        if cur:
            cur["score"] += score
        else:
            cands[bid] = {"cand": b, "score": score, "lane": lane, "reason": reason, "extra": ""}

    read_authors = set()
    for seed in seeds[:15]:
        title, author = seed["title"], seed["author"]
        # resolve the seed to a real catalogue entry for author/categories/id
        resolved = (ebookmeta.search(f"{title} {author}".strip(), num=1) or [None])[0]
        if resolved:
            author = author or resolved.get("author", "")
            for m in taste.candidate_moods(resolved.get("categories")):
                mood_profile[m.lower()] = mood_profile.get(m.lower(), 0.0) + 1.0
        if author:
            read_authors.add(author.split(",")[0].strip().lower())

        if author:                                   # author backlist
            for b in ebookmeta.by_author(author.split(",")[0], num=12):
                consider(b, config.W_AUTHOR_BACKLIST, "author",
                         f"More from {author.split(',')[0]}, an author you read")
        if resolved and seed["finished"]:            # subject-similar
            for i, b in enumerate(ebookmeta.similar(resolved, num=8)):
                a1 = (b.get("author") or "").split(",")[0].strip().lower()
                if a1 and a1 in read_authors:
                    consider(b, config.W_SIMS_FREQ - i * 0.5, "enjoyed",
                             f"Readers who enjoyed “{title}” also read this")
                else:
                    consider(b, (config.W_SIMS_FREQ - i * 0.5) * 0.9, "discover_author",
                             f"A new author for you — fans of “{title}” rate this")

    # your reading list (Goodreads / Hardcover want-to-read) as ebook picks
    for it in importlists.all_for_user():
        hit = ebookmeta.search(f"{it['title']} {it.get('author','')}".strip(), num=1)
        if hit:
            consider(hit[0], config.W_RATING * 1.5, "importlist",
                     "On your reading list" + (f" — {it['author']}" if it.get("author") else ""))

    # cold-start / thin history -> popular ebooks in default subjects
    if len(cands) < 6:
        for subj in ("fantasy", "science fiction", "mystery", "literature"):
            for b in ebookmeta.ol_subject(subj, num=12):
                if ebookmeta._is_modern(b):
                    consider(b, (b.get("rating") or 3), "discover",
                             f"Popular in {subj} — read more and your picks get personal")

    return recommend._finalize(user_id, cands, known, neg, max_new, fmt="ebook")
