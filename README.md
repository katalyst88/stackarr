# Stackarr

## 🔎 [**▶ Try the live demo →**](https://katalyst88.github.io/stackarr/)

**No install — runs in your browser.** A sample of the full UI (sample data,
actions disabled), hosted on GitHub Pages.

---

![version](https://img.shields.io/badge/version-1.0.0-brightgreen)
![docker](https://img.shields.io/badge/docker-ghcr.io%2Fkatalyst88%2Fstackarr-blue)

> **1.0** — used day-to-day by its author. Written largely with an AI assistant,
> so read the code before relying on it. Issues and PRs welcome.

**The recommendation shelf for Chaptarr.** Stackarr reads your
[Audiobookshelf](https://www.audiobookshelf.org/) listening history and suggests
your next audiobook — then hands the ones you pick to **Chaptarr** to download.
Think Seer, but for *what to listen to next*. **No AI in the recommendations** —
every pick is a real catalogue entry reached by an explainable rule.

Stackarr never touches a download client itself: Chaptarr (the Readarr-style
book/audiobook manager) does the searching, grabbing and importing.

---

## Features

- **13 recommendation rows**, all deterministic & explainable: Series to finish ·
  More from authors you love · Readers also enjoyed · New authors to discover ·
  Narrators you love · More in your favourite genres · Hidden gems · Award winners ·
  Short listens · Epic listens · New & upcoming · From your reading list · Popular.
- **Browse cards** for genres and *suggested* authors → full grid, with **"add all
  books by this author"**.
- **Book detail pages** (cover, series, narrator, rating, full description, genres).
- **Multi-user** — everyone signs in with their own Audiobookshelf account and gets
  suggestions from *their* history; admins can auto-approve.
- **Approve / Ignore / Already-read** with learning (passes, DNFs, deletions, and
  optional 1–5★ ratings feed back in). "I've already read this" seeding too.
- **Discover** gallery (endless scroll) + search-to-add typeahead.
- **Insights** (Spotify-wrapped style): hours listened, top authors, fun facts.
- **History & ratings** — a scannable list of every book you've finished, rated, or
  marked read (cover · title/author · a prominent 1–5★ control that sharpens future
  picks). Unrated float to the top; rated sink to the bottom. Remove any book from
  history (it stops seeding suggestions too), or hide rated books automatically.
- **Notifications** — email digests (3 themes + live preview), Discord webhook, and
  Apprise (100+ channels). All **off by default**.
- In-app **Settings** for service connections (Audiobookshelf, Chaptarr), SMTP,
  reading-list import (Goodreads/Hardcover), and a logs viewer — with Test buttons.
- Installable **PWA** or **[Android APK](https://github.com/katalyst88/stackarr/releases/latest)**,
  light/dark themes, responsive, embeds in **nzb360**.

## How recommendations work (no AI)

Your Audiobookshelf history (finished + in-progress) is the seed. For each seed
Stackarr checks series order, author backlist, Audible's own "listeners also
enjoyed" (`/sims`), narrators, and genres via the public **Audible** catalogue and
**Audnexus** — no API key, no model. Candidates are scored by a transparent
weighted formula (recency, your ratings, rating floor, popularity dampening),
de-duplicated by edition, diversity-capped per author, and shown with their reason.

## Quick start

```bash
# Option A — published image (recommended)
mkdir stackarr && cd stackarr
curl -O https://raw.githubusercontent.com/katalyst88/stackarr/main/.env.example
mv .env.example .env          # fill in Audiobookshelf + Chaptarr details
docker run -d --name stackarr -p 8484:8484 --env-file .env \
  -v ./config:/config ghcr.io/katalyst88/stackarr:latest

# Option B — build from source
git clone https://github.com/katalyst88/stackarr && cd stackarr
cp .env.example .env
docker compose up -d
```

Open `http://your-host:8484` and sign in with your Audiobookshelf account.
Images are published to **GitHub Container Registry** (`ghcr.io/katalyst88/stackarr`)
automatically on every release.

### Requirements

- **Audiobookshelf** with an admin API token.
- **Chaptarr** with an API key, a root folder, and a download client configured
  *in Chaptarr*.
- (Optional) SMTP / Discord / Apprise; Goodreads or Hardcover for "Want to Read".

### Key configuration

| Variable | Purpose |
|---|---|
| `ABS_URL`, `ABS_ADMIN_TOKEN` | Audiobookshelf connection (also editable in Settings) |
| `CHAPTARR_URL`, `CHAPTARR_API_KEY`, `CHAPTARR_ROOT_FOLDER` | where approved picks go |
| `STACKARR_ADMINS` | ABS usernames who can auto-approve / see all queues |
| `STACKARR_HTTPS=true` | set when behind HTTPS → Secure cookies |
| `STACKARR_FRAME_ANCESTORS` | who may embed Stackarr (default own-origin) |
| `STACKARR_ACCENT`, `STACKARR_URL_BASE`, `AUDIBLE_DOMAIN` | theming / subpath / region |

See [`.env.example`](.env.example) for the full annotated list.

## Android app

Prefer an app icon to the PWA? Grab `stackarr.apk` from the
**[latest release](https://github.com/katalyst88/stackarr/releases/latest)**
and sideload it. It's a thin, configurable WebView client — on first launch it
asks for your Stackarr URL (e.g. `http://192.168.1.10:8484`) and remembers it;
use the menu to change servers or reload. Built from `android/` by CI
(`.github/workflows/android.yml`).

## Security

Auth is delegated to Audiobookshelf; all routes require login; brute-force login
throttling, CSRF same-origin protection, `HttpOnly`/`SameSite` cookies (`Secure`
over HTTPS), and configurable frame-ancestors. See [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
