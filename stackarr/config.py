"""Stackarr configuration. Everything is environment-driven so the app
deploys anywhere with no code edits. Single source of truth for settings."""
import os

# Pre-public-release. The -pre suffix signals "not yet tested for public use";
# bump to 1.0.0 only after real-world testing.
VERSION = "0.1.24-pre"
RELEASE_STAGE = "pre-release · untested"


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


# --- core -------------------------------------------------------------------
PORT = int(os.environ.get("STACKARR_PORT", "8484"))
DATA_DIR = os.environ.get("STACKARR_DATA", "/config")
APP_NAME = os.environ.get("STACKARR_NAME", "Stackarr")
ACCENT = os.environ.get("STACKARR_ACCENT", "#d98c3f")          # amber; Seerr uses purple
# Subpath when reverse-proxied / embedded (e.g. "/stackarr"). Blank = root.
URL_BASE = "/" + os.environ.get("STACKARR_URL_BASE", "").strip("/") if os.environ.get("STACKARR_URL_BASE", "").strip("/") else ""

# --- logging ----------------------------------------------------------------
LOG_LEVEL = os.environ.get("STACKARR_LOG_LEVEL", "INFO").upper()
LOG_FILE = os.path.join(DATA_DIR, "stackarr.log")

# --- security ---------------------------------------------------------------
# Set when served over HTTPS (behind a reverse proxy) -> Secure cookies.
SECURE_COOKIES = _bool("STACKARR_HTTPS", False)
# Who may embed Stackarr in a frame (clickjacking control). Default own-origin
# only; set to a space-separated list (e.g. your nzb360/dashboard origin) to allow
# embedding, or "*" to allow anywhere (not recommended).
FRAME_ANCESTORS = os.environ.get("STACKARR_FRAME_ANCESTORS", "'self'")

# --- Audiobookshelf (auth + library + listening history) --------------------
# Users sign in with their own ABS credentials (multi-user). The admin token
# is used for server-wide ops (library listing, scans) and admin fallback.
ABS_URL = os.environ.get("ABS_URL", "").rstrip("/")
ABS_ADMIN_TOKEN = os.environ.get("ABS_ADMIN_TOKEN", "")
# ABS usernames that are Stackarr admins (auto-approve, see all queues).
ADMIN_USERS = _list("STACKARR_ADMINS")
# Limit which ABS libraries count as "owned"/seed history; empty = all book libs.
ABS_LIBRARY_IDS = _list("ABS_LIBRARY_IDS")

# --- Chaptarr (downstream: approved picks are handed here to grab) -----------
CHAPTARR_URL = os.environ.get("CHAPTARR_URL", "").rstrip("/")
CHAPTARR_API_KEY = os.environ.get("CHAPTARR_API_KEY", "")
# Root folder + profile ids Chaptarr should use when Stackarr adds a book.
CHAPTARR_ROOT_FOLDER = os.environ.get("CHAPTARR_ROOT_FOLDER", "")
CHAPTARR_QUALITY_PROFILE_ID = int(os.environ.get("CHAPTARR_QUALITY_PROFILE_ID", "2"))
CHAPTARR_METADATA_PROFILE_ID = int(os.environ.get("CHAPTARR_METADATA_PROFILE_ID", "1"))

# --- metadata sources (no API key needed; deterministic, no AI) -------------
AUDIBLE_DOMAIN = os.environ.get("AUDIBLE_DOMAIN", "com")           # com, co.uk, com.au, de…
AUDIBLE_API = f"https://api.audible.{AUDIBLE_DOMAIN}/1.0"
AUDNEXUS_API = os.environ.get("AUDNEXUS_API", "https://api.audnex.us")
AUDNEXUS_REGION = os.environ.get("AUDNEXUS_REGION", AUDIBLE_DOMAIN if AUDIBLE_DOMAIN != "com" else "us")

# --- suggestion engine ------------------------------------------------------
SUGGEST_ENABLED = _bool("STACKARR_SUGGEST", True)
SUGGEST_INTERVAL_HOURS = int(os.environ.get("STACKARR_SUGGEST_HOURS", "12"))   # default; user-overridable in Settings
SUGGEST_MAX_PENDING = int(os.environ.get("STACKARR_SUGGEST_MAX", "30"))
SUGGEST_PER_LANE = int(os.environ.get("STACKARR_PER_LANE", "12"))   # how many to keep per category
TARGET_LANGUAGE = os.environ.get("STACKARR_LANGUAGE", "english").lower()
SUGGEST_RATING_FLOOR = float(os.environ.get("STACKARR_RATING_FLOOR", "4.0"))
SUGGEST_MAX_PER_AUTHOR = int(os.environ.get("STACKARR_MAX_PER_AUTHOR", "2"))
LIBRARY_REFRESH_MINUTES = int(os.environ.get("STACKARR_LIBRARY_REFRESH_MIN", "30"))

# Signal weights (deterministic composite; tune without code changes).
W_SERIES_NEXT = float(os.environ.get("W_SERIES_NEXT", "10"))
W_SIMS_FREQ = float(os.environ.get("W_SIMS_FREQ", "8"))
W_AUTHOR_BACKLIST = float(os.environ.get("W_AUTHOR_BACKLIST", "6"))
W_NARRATOR = float(os.environ.get("W_NARRATOR", "4"))
W_RATING = float(os.environ.get("W_RATING", "3"))
W_RECENCY = float(os.environ.get("W_RECENCY", "2"))
POPULARITY_DAMPEN = float(os.environ.get("POPULARITY_DAMPEN", "0.15"))   # 0=off, higher=more anti-bestseller

# --- discover (genre-trending, deterministic) -------------------------------
DISCOVER_ENABLED = _bool("STACKARR_DISCOVER", True)

# --- import lists (Goodreads / Hardcover "want to read") --------------------
GOODREADS_RSS = os.environ.get("GOODREADS_RSS", "")               # to-read shelf RSS url(s), comma-sep ok
HARDCOVER_TOKEN = os.environ.get("HARDCOVER_TOKEN", "")

# --- notifications (Apprise engine + email themes + optional Discord bot) ----
APPRISE_URLS = _list("APPRISE_URLS")                              # any apprise targets
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_TO = os.environ.get("SMTP_TO", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")       # optional interactive bot
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")           # simple digest webhook


def validate() -> list[str]:
    problems = []
    if not ABS_URL or not ABS_ADMIN_TOKEN:
        problems.append("ABS_URL and ABS_ADMIN_TOKEN are required")
    if not CHAPTARR_URL or not CHAPTARR_API_KEY:
        problems.append("CHAPTARR_URL and CHAPTARR_API_KEY are required (Stackarr hands approved picks to Chaptarr)")
    return problems
