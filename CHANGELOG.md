# Changelog

All notable changes to Stackarr.

## [Unreleased]

### Consistency pass (field report 2026-10-12)

One answer per book on every page, and the things that "didn't run fluidly".

#### Fixed
- **Rating stars did nothing** on History, the book page and the Suggestions
  quick-rate card (also Find missing, Clear, Remove, Add all by author): their
  `onclick="…{{ x|tojson }}…"` attribute ended at tojson's first `"`. Single-
  quoted now, with a template guard test. Ratings — and Insights' average and
  moods — update again.
- **Cards that linked nowhere.** Home's Read shelf linked 2 of 23 books (only
  ones with an ASIN; the rest reloaded Home). Every card now opens a book
  page: its id, an id Stackarr already holds for the work, or `/book/find`,
  which resolves the title on click (one catalogue search, remembered).
- **Recently added** linked to the server's internal ABS address
  (`http://audiobookshelf:80`). It opens the Stackarr book page; "Open in
  Audiobookshelf" appears only when the new optional Settings → Audiobookshelf
  → Public URL (or `ABS_PUBLIC_URL`) is set.
- **Duplicate requests.** One entry per book on Requests and Recent requests,
  its formats each with a status, earlier attempts folded underneath. A new
  attempt updates the book's row for that format instead of adding one; asking
  again while it's open makes no second Chaptarr search. Existing rows are
  grouped on display — no migration, nothing deleted.
- **"Requested" here, "Downloading" there.** `status.py` decides for every page:
  In library / Downloading (% and ETA) / Importing / Stalled / Waiting ("Held
  by Chaptarr: …") / Requested / Not found yet (a week with no usable release)
  / Needs approval / Failed / Not approved / Upcoming. Chaptarr's queue is read
  once per 30 s, only when something on screen is on order.
- **A book started today wasn't "Currently reading"**: unfinished books under
  25% progress were dropped. Now from the first minute.
- **Insights** counted library duplicates (30 finished for 13 books) and
  disagreed with Home's goal ring; it now uses Home's once-per-book finishes.
- **Foreign-language editions** cluttered author, search, browse and series
  pages (Matt Dinniman: 23 of 36). Lists follow Settings → language (default
  English; "any" keeps all) and show one card per work; series maps prefer the
  language and drop "Embargo title" placeholders.
- Covers: no inline Audible search per coverless ABS item; Discover scroll and
  search results get the blank-cover fallback. Book page lookups run in
  parallel and are kept ten minutes.

### Series follow-through

Series follow-through and honest fresh picks, reported with field evidence
and patches.

### Added
- **"Next in ‹series›" on the book page**: the next three books after the
  highest one you've finished in Audiobookshelf (or after the book you're
  viewing), each marked Finished / In library / Requested / Upcoming, or with a
  Request button. Series order comes from Audible's series product (one call
  for the order, one batch lookup for the books), falling back to Chaptarr's
  `seriesTitle` for ebook-only series. Cached for a day (misses for an hour).
- **"Continue ‹series›" card on Home** for the book you finished most
  recently (last 14 days) whose series has more after it.
- **Request remaining**, on the strip, the Home card and every Up Next card.
  A confirm dialog lists every book of the series in order: the ones to
  request ticked (with their formats), the rest greyed out with the reason
  (In library / Requested / Finished / Out ‹date›). Only ticked books are
  sent — at most 10 per click, one request row per book per format so
  each flips to available on its own. Chaptarr monitors only those books and
  runs one search per format; nothing else by the author is touched. The
  non-admin approval gate still applies (one pending row per book).

### Fixed
- **Stale fresh picks.** A pending suggestion that was since requested, added
  to the library or finished in Audiobookshelf stayed on Home forever. Such
  picks are now retired on each library refresh, before each recommender run
  and before Home renders, with their own status (`owned` / `requested` /
  `finished`) — not `rejected`, so retiring is never a "not interested".
- **The recommender stopped running for users with a full queue.** Pending
  picks are capped (`STACKARR_SUGGEST_MAX`, 30); a queue full of stale picks
  left no room, so no new picks were ever generated. Retiring frees the room.
- **"Chaptarr couldn't find ‹author›"** for an author Chaptarr already
  manages: its author lookup can come back empty on back-to-back calls.
  Requests now reuse the existing Chaptarr author (exact name) before looking
  anyone up.
- **The book page showed a finished book as unread** unless it was on
  Stackarr's own Read shelf; it now also reads Audiobookshelf.
- **Every Add button appeared twice on the book page**: the bottom bar
  repeated them next to "Back to top". It now only scrolls back up.

### Deprecated
- `POST /api/series/add` ("Get full series"): it monitors every Chaptarr row
  in the series, owned ones included, and falls back to the author's whole
  backlist. Unchanged for API callers, answers with `Deprecation: true`; the UI
  uses `POST /api/series/request-remaining`.

## [1.6.9] - 2026-10-05

Fixes from running Stackarr against Chaptarr's media-split schema (an
audiobook row and an ebook row per work) with `STACKARR_FORMATS=both`,
reported with patches and live test results by Mr Hynesy. Also ships the
cover-serving fix that had been sitting uncommitted since 2026-09-21.

### Fixed
- **Requesting one book no longer makes Chaptarr want the author's whole
  bibliography.** Authors were added (and reused for a new format) with
  "monitor existing books: All"; they're now set to *Selected*, and only the
  requested book's edition is monitored and searched. Nothing a user monitored
  themselves is unmonitored.
- **Requests now move from "handed" to "available".** The library check matches
  titles in both directions (Audible's "Title: Subtitle" vs the library's
  "Title"), resolves legacy formats, handles `both` requests, and asks Chaptarr
  whether it has imported the requested edition — so ebooks filed where no
  connected library looks are picked up too. Existing rows whose format holds an
  Audible edition string ("Unabridged") are migrated to `audiobook`.
- **Books you own showed a Request button.** Library titles are often decorated
  ("The Captain: The Last Horizon, Book 1", "House of Blades (Unabridged)") and
  usually have no ASIN, and ownership was an exact title match. A shared,
  subtitle-aware matcher (`stackarr/titles.py`) now backs the owned check, the
  book page's owned formats, mark-read write-back, request availability and the
  recommenders' "already have it" filter.
- **Wrong author picked** from Chaptarr's fuzzy author lookup (request and
  mark-read): the author actually named now wins over the top hit.
- **Wrong-media edition searched**: a request now searches the row for the
  requested format, found by `mediaType` without an extra indexer search.
- **eBooks filed in the audiobook root folder** for authors first added for
  audiobooks — new optional `CHAPTARR_EBOOK_ROOT_FOLDER`.
- **Requests failed for any title with a subtitle** ("Pandora's Star:
  Commonwealth Saga 1" vs Chaptarr's "Pandora's Star").
- **Audible's edition string used as the media format**, so a `both` install
  never requested the eBook from a search/Discover card.
- **Search and Discover results weren't clickable** (and did nothing on phones).
- **"Get the rest of the series" and approved series/author requests failed**:
  the series name was matched as a book title. The series' books are now
  monitored and searched (the whole author if Chaptarr doesn't list the series).

- **Covers cost the page its worker threads.** `/coverart` is the target of an
  `<img>`, and it did an external metadata lookup on the request thread — ~59 of
  them per `/home` render, one measured at 20.58s against 8 worker threads, so
  the whole page crawled rather than just the pictures. Misses were never
  cached, so every render repeated the lot and exhausted Google Books' keyless
  daily quota. Owned books now serve the art the house already holds: the cover
  link Calibre-Web's OPDS feed sends for every entry (and Kavita's series cover)
  is kept in a new `library.cover` column and served same-origin from
  `/libcover/<ref>`. `/coverart` never calls out on the request thread; unowned
  books go to a background worker, a miss is remembered for 24h, and a
  daily-quota 429 opens a circuit breaker instead of being retried per image.

### Added
- A portable test suite (`pytest`, no running services needed) — see
  CONTRIBUTING.md.

## [1.6.8] - 2026-07-04

Security hardening from the same field report's attached findings doc (thanks
Mr Hynesy) — all low-severity / defence-in-depth.

### Security
- **Settings secrets gated server-side.** `settings_page` now computes the
  `conn` (service creds), `smtp`, and webhook-URL blocks only for admins, instead
  of building them for every caller and relying on template `{% if is_admin %}`
  guards alone — so one future ungated field can't leak the ABS admin token / SMTP
  password to a non-admin.
- **DOM-XSS sink closed.** The series "find missing books" list now escapes
  catalogue titles and URL-encodes ASINs before they hit `innerHTML`.
- **CSRF guard fails closed.** A cookie-authenticated mutating request with no
  Origin *and* no Referer is now rejected instead of allowed (browsers always send
  one same-origin; only forged cross-site requests strip both). Header-authenticated
  automation (X-Api-Key / KOReader kosync) carries no session cookie and stays exempt.
- **KOReader kosync key namespaced.** Progress is stored under a hashed
  `kosync_<sha1(user)>_<sha1(doc)>` key so a crafted `document` value can't collide
  with another kosync account's row (was `kosync_{user}_{doc}`, an ambiguous join).

### Added
- **Cooldown on expensive endpoints.** `/api/library/refresh`,
  `/api/requests/check`, and `/api/run-now` are now per-user rate-limited (20–30s)
  so they can't be hammered into a resource-abuse loop; they return 429 with a
  friendly "try again in Ns" message.

## [1.6.7] - 2026-07-04

Fixes from a user field report (thanks Mr Hynesy).

### Fixed
- **Star ratings now stick.** Rating a library book (Suggestions onboarding /
  History) wrote under a *recomputed* slug that differed from the key the page
  rendered from, so the write landed somewhere History never read back — it
  looked saved but wasn't. `api_rate` now trusts the canonical `t-…` key the
  client sends instead of recomputing it.
- **Rating errors are no longer swallowed.** The front-end showed a success
  toast regardless of the response; `rate()` / `submitReview()` now check the
  result, surface the real error, and revert the optimistic star fill on failure.
- **Collapsed sidebar can be expanded again.** The collapse toggle hid *itself*
  when collapsed, trapping you in the narrow rail. It now stays visible and flips
  its arrow (« ⇄ »).
- **Sidebar stays put.** As a stretched flex child it scrolled away despite
  `position: sticky`; `align-self: flex-start` lets sticky pin it.
- **Home "Read" shelf populates.** Finished audiobooks/ebooks now seed the Read
  shelf the same way in-progress titles seed Reading, so it matches the
  read-this-year count instead of sitting at 0.

### Security
- **Settings → Reading is admin-gated.** The shared Goodreads RSS / Hardcover
  token were rendered to every signed-in user (read-only; writes were already
  admin-only). The panel and the values are now admin-only.

### UI
- Native `prompt()` dialogs (library sign-in, deny-request reason) replaced with
  an in-app modal that matches the rest of the UI.

## [1.6.0] - 2026-06-14

### Recommendation quality (deterministic, no AI) — see [RECOMMENDATIONS.md](RECOMMENDATIONS.md)
- **Mood/pace matching** — a taste profile (your picked vibes + the moods of what
  you read, derived from catalogue subjects) scores every candidate by mood
  overlap. New "Matches your mood" lane.
- **Serendipity** — a bonus for well-rated, lesser-known books + an "Off the
  beaten path" lane, to beat the "too obvious" problem.
- **Adventurousness dial** (Settings) — Comfort ↔ Discovery.
- **Smarter negatives** — a DNF nudges down the whole *mood*, not just the title.
- **Mood-aware cold start** — a vibe picker shapes picks before any history.
- **Format-isolated taste** by default (audiobook vs ebook), cross-format opt-in.
  Light household collaborative boost.

### Reading features
- **My shelves** (Want / Reading / Read) + a **reading-goal ring**.
- **Insights** rebuilt: format split, GitHub-style **activity heatmap**, goal
  ring, **mood profile**.
- **Author** and **narrator** pages (Follow an author → new-release radar);
  **Upcoming** page; **Surprise me**; **series completion bars** on Up Next.
- **Reviews** gain helpful-votes + spoiler tags; **mood tags** + **content
  warnings** on book pages; **More / Less like this**; **get the other format**.
- **Check libraries** re-scans ABS/Kavita/Calibre for books added outside
  Chaptarr; requests skip Chaptarr if already owned.

### UI
- Format filter + per-card format **icons** across Suggestions / Up Next /
  Requests / History / Shelves / Upcoming; mobile header + request-row redesign.

## [1.5.1] - 2026-06-13
- Removed the "Recently rated by readers" row from the home page.

## [1.5.0] - 2026-06-13

### Added
- **Multi-format support: eBooks alongside audiobooks.** A format toggle
  (Settings → General → Library format): `audiobook` (default), `ebook`, or
  `both`. Default keeps existing installs unchanged until flipped.
- **Pluggable library backends.** New `backends/` package with a `Backend`
  interface; Audiobookshelf stays the login + audiobook source, with **Kavita**
  (API key — reliable per-user reading progress) and **Calibre-Web** (OPDS;
  read/unread only — the UI says so) as connected eBook sources in Settings →
  Connections.
- **eBook recommendation engine** (`recommend_ebook.py`): seeds from connected
  eBook sources' reading history + the Hardcover *read* shelf; candidates from
  **Google Books + Open Library** (no API key); author-backlist, subject-similar,
  reading-list and popular lanes (subject fallback filters out public-domain
  classics so picks stay contemporary).
- **Format-aware UI** in `both` mode: per-card format badges + an All/Audiobooks/
  eBooks filter; single-format installs never render the other format's chrome.
  eBook picks hand off to Chaptarr in the eBook media type/profiles.
- **Shared ratings & reviews**: community average ★ and everyone's written
  reviews on each book page, plus a **Recently rated by readers** home row.
- **New-release radar**: optional alert when an author you rate 4–5★ publishes
  (Settings → Notifications; off by default).

### Fixed / hardened
- Book detail pages fall back to cached title/author and **retry** the keyless
  eBook catalogue fetches, so a flaky Google Books / Open Library call no longer
  shows "Unknown".
- Audited with 882 automated click-throughs across every page/function
  (0 console / HTTP / server errors).

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
- **History & ratings** redesigned as a clean, scannable list:
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
- Docs accuracy pass.

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
