# How Stackarr decides what to recommend

Stackarr's recommender is **fully deterministic — no AI, no model calls, no
black box.** Every suggestion is a real catalogue entry reached by an
explainable rule, and each carries the reason it was picked (shown on the card).
This document is the canonical description of how a pick gets made.

## Inputs (your taste)

The engine runs per user and reads only *your* signals:

- **Reading history** — the seeds. Audiobooks come from Audiobookshelf's
  finished/in-progress list; ebooks from Kavita/Calibre-Web reading progress and
  the Hardcover *read* shelf. Recent finishes count for more.
- **Ratings** (1–5★) — boost an author/series; 5★ pulls hard, 1★ pushes away.
- **Signals** — passes, did-not-finish (DNF), "already read", deletions from
  your library, and explicit 👍/👎 "more/less like this". Negatives hard-exclude
  or de-weight.
- **Vibe picks & moods** — the moods you choose at onboarding plus the moods of
  the books you actually read (see *Mood matching* below).
- **Your reading list** — Goodreads / Hardcover "want to read" shelves.

Everything is **format-isolated by default**: audiobook taste shapes audiobook
picks and ebook taste shapes ebook picks, independently. Turn on *Settings →
General → cross-format taste* to let them inform each other.

## Candidate sources (where books come from)

All free, keyless, no AI:

- **Audible catalogue** — search, by-author, product-by-ASIN, and the `/sims`
  "listeners also enjoyed" graph (audiobooks).
- **Google Books + Open Library** — search, by-author, and subject-based related
  (ebooks).
- **Audnexus** — series order + genres for an audiobook.

## The lanes (and the rule behind each)

| Lane | Rule |
|---|---|
| **Series to finish** | The next book in a series you're part-way through (strongest signal). |
| **Readers also enjoyed** | Audible `/sims` from a book you finished, where the author is one you already read. |
| **Matches your mood** | Books whose mood/pace matches your strongest reading moods. |
| **New authors to discover** | `/sims` from a finished book by an author you *haven't* read. |
| **More from authors you love** | Back-catalogue of authors you've read (weighted by how much you rated them). |
| **Narrators you love** | Other audiobooks narrated by narrators you listen to a lot. |
| **More in your favourite genres** | Well-rated, recent books in your top genres. |
| **Off the beaten path** | Highly-rated but *lesser-known* books in your genres (serendipity). |
| **New & upcoming** | Not-yet-released / just-released titles from authors you read. |
| **From your reading list** | Your Goodreads/Hardcover "want to read" shelf. |
| **Award winners / Short / Epic** | Rule-named slices of your top genres. |

## How a candidate is scored

Each candidate accumulates a score; the rule that surfaced it sets the base
weight, then these modifiers apply (all tunable via `W_*` env vars):

1. **Base lane weight** — e.g. series-next ≫ genre.
2. **Recency of the seed** — a book you finished last week counts more than one
   from two years ago.
3. **Author / series / narrator affinity** — from your ratings + signals.
4. **Average rating** — a floor (default 4.0★) plus a boost above it.
5. **Mood/pace overlap** (`W_MOOD`) — the book's moods (derived from its
   catalogue subjects) scored against your mood profile. Can go *negative* for
   moods you've DNF'd.
6. **Serendipity** (`W_SERENDIPITY`) — a bonus for well-rated, low-popularity
   books, scaled by your **adventurousness** dial.
7. **Popularity dampening** (`POPULARITY_DAMPEN`) — gently down-weights
   mega-bestsellers so it isn't all the obvious picks.
8. **Frequency** — a candidate surfaced by several of your seeds compounds.
9. **Hard excludes** — anything you own, passed, DNF'd, or deleted; and any
   dramatised/GraphicAudio edition; and (unless "any") non-target-language
   editions.

### The adventurousness dial (Comfort ↔ Discovery)

A per-user slider (default 50). It scales the split between **familiar** lanes
(series, author, narrator) and **discovery** lanes (new authors, off-the-beaten-
path, mood). Lower = safer, more of what you already love; higher = more
exploration and hidden gems.

### Mood matching (the StoryGraph-style edge)

Moods and pace (e.g. *dark, funny, fast-paced, cozy*) are derived
**deterministically** from a book's catalogue subjects via a curated keyword map
(`tagging.py`) — no AI. Your mood profile is built from the moods you pick at
onboarding plus the moods of every book you read. Candidates are then scored by
how well their moods overlap yours, and a DNF nudges the *whole mood* down, not
just that title.

## Finalising

Candidates are de-duplicated by edition, then the top N **per lane** are kept
with an author-diversity cap, so every category is represented rather than one
lane crowding out the rest. New picks are written to your queue and (optionally)
handed to Chaptarr on approval.

## Tuning

Weights are environment variables — change them without touching code:
`W_SERIES_NEXT`, `W_SIMS_FREQ`, `W_AUTHOR_BACKLIST`, `W_NARRATOR`, `W_RATING`,
`W_MOOD`, `W_SERENDIPITY`, `POPULARITY_DAMPEN`, `SUGGEST_RATING_FLOOR`,
`SUGGEST_MAX_PER_AUTHOR`, and the per-user adventurousness dial in Settings.
