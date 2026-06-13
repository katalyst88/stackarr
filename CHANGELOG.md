# Changelog

All notable changes to Stackarr.

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
