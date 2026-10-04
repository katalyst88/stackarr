# Contributing to Stackarr

Thanks for your interest! Stackarr is a small, deterministic (no-AI) audiobook
recommendation add-on for [Chaptarr](https://chaptarr.com). It was largely
**vibecoded** with an AI assistant — so contributions that tighten, simplify,
or harden the code are very welcome.

## Dev setup

```bash
git clone https://github.com/katalyst88/stackarr && cd stackarr
cp .env.example .env          # fill in Audiobookshelf + Chaptarr details
pip install -r requirements.txt
python run.py                 # http://localhost:8484
```

Or with Docker:

```bash
docker compose up -d --build
```

## Tests

The unit tests need no running services — Chaptarr and the library sources are
small in-memory fakes, and each test gets a throwaway SQLite DB.

```bash
pip install -r requirements-dev.txt
pytest                         # from the repo root
```

The search-card UI check runs `static/app.js` under jsdom:

```bash
npm install --no-save jsdom && node tests/js/search-card.mjs
```

## Project layout

- `stackarr/recommend.py` — the deterministic recommendation engine (the heart).
- `stackarr/absclient.py` / `audible.py` / `audnexus.py` — data sources.
- `stackarr/chaptarr.py` — the handoff to Chaptarr.
- `stackarr/routes.py` — pages + JSON API. `templates/` + `static/` — the UI.

## Guidelines

- **No AI in the recommendation path** — every pick must be a real catalog
  entry reached by an explainable rule. Keep it deterministic.
- Match the existing style; keep config env-driven (`stackarr/config.py`).
- Bump `VERSION` in `config.py` and add a `CHANGELOG.md` entry with your change.
- Add a test under `tests/` for a behaviour fix, and test against a real
  Audiobookshelf + Chaptarr where you can.

## Releasing

Every release must follow [`RELEASING.md`](RELEASING.md). Updating the docs and
**regenerating the GitHub Pages demo** (`python tools/build_demo.py`, commit
`docs/`) are mandatory parts of cutting a release, not afterthoughts.

## Reporting issues

Use the issue templates. Include your Stackarr version (shown in the sidebar /
`/api/health`), and relevant lines from the in-app **Settings → Logs**.
