# Stackarr

![status](https://img.shields.io/badge/status-pre--release%20·%20untested-orange)
![version](https://img.shields.io/badge/version-0.1.0--pre-blue)

> 🚧 **Pre-release (v0.1.0-pre).** Not yet tested for general use — running and
> being shaken out by its author first. Versions stay `0.x.y-pre` until a real
> `1.0.0`. Don't rely on it in production yet.

**The recommendation shelf for [Chaptarr](https://chaptarr.com).** Stackarr reads
your [Audiobookshelf](https://www.audiobookshelf.org/) listening history and
suggests your next audiobook — series-next, author backlists, "listeners also
enjoyed," narrators you love — then hands the ones you approve to Chaptarr to
download. Think Overseerr, but for what to *listen to next*, and with **no AI**.

> ⚠️ **Built with AI / "vibecoded".** Stackarr was written largely by an AI
> coding assistant as a companion add-on to Chaptarr. It works and it's tested,
> but treat it accordingly: read the code before you trust it, and expect rough
> edges. PRs and issues welcome.

> Stackarr does **not** download anything itself. It is a *recommendation and
> approval* layer — Chaptarr (the Readarr-style book/audiobook manager) does the
> searching, grabbing and importing. Stackarr just decides *what's worth getting*
> and asks Chaptarr to fetch it.

## Why it exists

Chaptarr manages your audiobook library brilliantly but has no taste engine —
nothing that looks at what you've actually listened to and says "you'll want this
next." Stackarr is that engine.

## Features

- 🎧 **Suggestions from your real listening history** — finished books and ones
  you're part-way through, weighted by how recently you listened.
- 🧠 **Deterministic, explainable, no AI** — every pick is a real catalog entry
  reached by a rule (next-in-series, author backlist, Audible's own
  "listeners also enjoyed," narrator-following) and shows you *why* it's there.
- ✅ **Approval queue** — approve or pass; approvals go straight to Chaptarr.
- 👍 **Learns from you** — passes, DNFs and books you delete become negative
  signals; optional **1–5★ ratings** and a "**I've already read this**" entry
  (no download) sharpen future picks.
- 👥 **Multi-user** — everyone signs in with their **own Audiobookshelf account**
  and gets suggestions from *their* history. Admins can auto-approve.
- 🔭 **Discover** tab — genre-trending and new releases, plus search-to-add.
- ✉️ **Notifications** — email digests with three themes (light / dark / fun) and
  a live preview, a Discord webhook, and Apprise (100+ channels). All optional.
- 📲 **Installable PWA**, light/dark UI, and embeds cleanly in an **nzb360**
  webview or behind a reverse proxy.

## How suggestions work (and why there's no AI)

1. Your Audiobookshelf history is the seed (finished + >25% progress).
2. For each seed Stackarr checks: **series order** (you finished book 2 → book 3),
   **author backlist**, **Audible `/sims`** ("listeners also enjoyed"), and
   **narrators** you listen to often.
3. Candidates are scored by a transparent weighted formula — signal type ×
   recency, plus your ratings, an Audible rating floor, and **popularity
   dampening** so it isn't only bestsellers — then de-duplicated by edition
   (unabridged preferred over dramatized/GraphicAudio) and diversity-capped per
   author.
4. The top picks land in your approval queue, each with its reason.

No model, no API key, no hallucinated titles.

## Quick start

```bash
git clone https://github.com/katalyst/stackarr && cd stackarr
cp .env.example .env     # fill in Audiobookshelf + Chaptarr details
docker compose up -d
```

Open `http://your-host:8484` and sign in with your Audiobookshelf account.

### Requirements

- **Audiobookshelf** with an admin API token (`ABS_ADMIN_TOKEN`).
- **Chaptarr** with an API key, a root folder, and a download client configured
  *in Chaptarr*.
- (Optional) SMTP / Discord / Apprise for digests; Goodreads or Hardcover for
  "Want to Read" import.

### Key settings

| Variable | Purpose |
|---|---|
| `ABS_URL`, `ABS_ADMIN_TOKEN` | Audiobookshelf connection |
| `CHAPTARR_URL`, `CHAPTARR_API_KEY`, `CHAPTARR_ROOT_FOLDER` | where approved picks go |
| `STACKARR_ADMINS` | ABS usernames who can auto-approve / see all queues |
| `STACKARR_ACCENT` | UI accent colour |
| `STACKARR_URL_BASE` | subpath when reverse-proxied / embedded |
| `AUDIBLE_DOMAIN` | catalog region (`com`, `co.uk`, `com.au`, `de`, …) |

See [`.env.example`](.env.example) for the full annotated list.

## License

MIT
