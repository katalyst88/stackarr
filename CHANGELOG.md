# Changelog

All notable changes to Stackarr.

## [1.4.0] - 2026-06-13

### Added
- **Up Next** (new nav item): a series tracker — every series you're collecting,
  how far you are, and the next book with its state (in library / requested /
  ready to add). Built from your library's series metadata (read straight from
  Audiobookshelf) plus the engine's series picks.
- **Taste** (new nav item): see and undo everything that shapes your picks —
  ratings, **did-not-finish**, passed/ignored, already-read seeds, and removed
  books — in one place. DNF is a new negative signal.
- **Quick-rate onboarding**: when your ratings are sparse, the home page shows a
  card to rate books you've already listened to, instantly sharpening picks.
- **Availability notifications**: get told when a requested book lands in your
  Audiobookshelf library. Off by default; fans out across email / Discord /
  Apprise / a new **custom webhook** channel.
- **Auto-add to Chaptarr** (Settings → Suggestions): optional tiered
  auto-approval — Off (default) / Conservative (next-in-series) / Moderate
  (series + loved authors + reading list) / Aggressive (any strong pick), each
  capped per cycle. Skips owned books and pauses if Chaptarr can't add.
- Library now stores **series, sequence, and narrator** (from Audiobookshelf).

### Changed
- A round of **animation polish**: staggered list/card entrances, hover lift on
  series cards, a star "pop" on rating, nav micro-interactions — all suppressed
  under `prefers-reduced-motion`.

## [1.3.0] - 2026-06-13

### Changed
- **History & ratings** redesigned as a clean, scannable list (Seer-styled):
  square cover thumbnail · title/author · a prominent 1–5★ rating on the right.
  Unrated books float to the top; rating one sinks it to the bottom "done" pile.
  Stars light up to the cursor on hover and fill in the app's accent colour.

### Added
- Rating now works for **library books with no ASIN** (most of them): ratings
  key on a stable title+author slug when there's no ASIN, so every read book is
  ratable. The title/author are captured on the rating so the recommender's
  author boost still applies.
- **Remove from history** (✕ on each row). A removed book is gone for good — it
  no longer shows in History even though it's still finished in Audiobookshelf,
  **and it no longer seeds suggestions**.
- **Settings → My reading → "Hide books from history after rating"**: when on,
  rating a book removes it from the list instead of sinking it to the bottom.

### Fixed
- **Goodreads reading-list import** was failing for everyone — Goodreads 403s the
  default `python-requests` User-Agent. Now sends a browser User-Agent; the
  public per-shelf RSS imports correctly.

## [1.2.0] - 2026-06-13

### Added
- **Android APK** — a configurable WebView client (enter your server URL on first
  launch) as an alternative to the PWA. Built by CI and attached to each Release.

## [1.1.0] - 2026-06-13

### Added
- **History & ratings** page (new sidebar item): every book you've finished in
  Audiobookshelf, rated, or marked read, shown as cards with an always-visible
  1–5★ rating control that feeds the recommender. Included in the demo.

### Fixed
- Reading-list import (`importlists.all_for_user`) now honours the Goodreads /
  Hardcover values set in **Settings** (DB), not just the env defaults — the
  in-app reading-list panel was previously inert.

## [1.0.0] - 2026-06-13
First stable release.

### Added
- Static **demo site** on GitHub Pages (`tools/build_demo.py` → `docs/`), built
  from the real templates with sample data and no backend.
- `RELEASING.md`: docs + demo regeneration are now mandatory release steps.

### Changed
- "Approve" renamed to **"Add book"** throughout.
- Home gains an **"Authors you might like"** card row (suggested authors only) →
  author grid with "add all books by this author".
- Docs accuracy pass; "Seerr" references corrected to "Seer".

### Fixed
- CI: `docker-publish` now sets up Buildx so the gha build cache works.

## [0.1.23-pre] - 2026-06-13
First public-candidate build (pre-release, vibecoded).

### Added
- Deterministic, **no-AI** recommendation engine across 13 lanes: series-next,
  authors you love, readers also enjoyed, new authors to discover, narrators,
  genres, hidden gems, award winners, short/epic listens, new & upcoming, and
  your reading list. Per-lane output with author diversity + popularity dampening.
- **Multi-user** login via Audiobookshelf accounts; per-user taste, approval queue.
- Approved picks handed to **Chaptarr**; nothing downloaded directly.
- Home merges personal lanes + genre cards + Recently Added / Recent Requests +
  a Discover gallery. Book detail pages, genre/author browse ("add all by author"),
  search typeahead, per-row "See all" grids.
- Insights (Spotify-wrapped style), 1–5★ ratings, "already read" seeding,
  Goodreads/Hardcover import, email (3 themes) / Discord / Apprise digests
  (off by default), in-app connection + SMTP settings with Test buttons, logs panel.
- Installable PWA, light/dark themes, responsive, nzb360-webview friendly.
- CI publishes the image to GHCR; Docker + compose.

### Notes
- Pre-release: not yet tested for general use.
