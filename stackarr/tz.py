"""The household's timezone — the one place dates are bucketed and shown in.

The reading heatmap, the yearly goal, "finished N days ago", the Home
"Continue" window, Insights and request timestamps all turn a moment into a
calendar day. They used the process timezone, and a container defaults to
UTC: a book finished at 07:30 in Melbourne (20:30 UTC the day before) landed
on yesterday's square. Every caller now asks this module instead.

The zone is Settings → General → Timezone (db.setting "timezone"), else the
TZ environment variable, else UTC — an IANA name, resolved with the stdlib
zoneinfo. Nothing here reads the process timezone except `from_db`, which has
to: SQLite's datetime('now','localtime') stamps rows in the process's local
time, so that is what those strings mean."""
import datetime
import functools
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from . import db

UTC = datetime.timezone.utc


@functools.lru_cache(maxsize=1)
def zones() -> tuple:
    """Every IANA zone name this system knows, sorted (the Settings picker)."""
    return tuple(sorted(z for z in available_timezones() if "/" in z or z == "UTC"))


def valid(name: str) -> bool:
    if not name or name != name.strip() or ".." in name or name.startswith("/"):
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def default_name() -> str:
    """The TZ environment variable when it names a zone, else UTC."""
    env = (os.environ.get("TZ") or "").lstrip(":")
    return env if valid(env) else "UTC"


def name() -> str:
    v = db.setting("timezone", "")
    return v if valid(v) else default_name()


@functools.lru_cache(maxsize=8)
def _zone(n: str):
    return ZoneInfo(n) if n != "UTC" else UTC


def zone():
    return _zone(name())


def now() -> datetime.datetime:
    return datetime.datetime.now(zone())


def today() -> datetime.date:
    return now().date()


def from_ms(ms) -> datetime.datetime:
    """An epoch-milliseconds stamp (Audiobookshelf's lastUpdate) in the zone."""
    return datetime.datetime.fromtimestamp((ms or 0) / 1000, tz=zone())


def day_of_ms(ms) -> str:
    """The calendar day (ISO) an epoch-ms moment falls on, in the zone."""
    return from_ms(ms).date().isoformat()


def from_db(s) -> datetime.datetime | None:
    """A stored "YYYY-MM-DD HH:MM:SS" (written with SQLite 'localtime', i.e.
    the process's local time) as an aware datetime in the zone."""
    try:
        naive = datetime.datetime.fromisoformat(str(s or "").strip().replace(" ", "T"))
    except ValueError:
        return None
    if naive.tzinfo is None:
        naive = naive.astimezone()               # the process-local time SQLite wrote
    return naive.astimezone(zone())


def db_day(s) -> str:
    """The calendar day (ISO) of a stored timestamp, in the zone ("" if unparseable)."""
    d = from_db(s)
    return d.date().isoformat() if d else ""


def fmt(s, pattern: str = "%Y-%m-%d %H:%M") -> str:
    """A stored timestamp formatted in the zone — the `localtime` template filter."""
    d = from_db(s)
    return d.strftime(pattern) if d else (str(s or "")[:16])


def fmt_epoch(sec, pattern: str = "%d %b %H:%M") -> str:
    """Epoch seconds, formatted in the zone ("" if missing)."""
    try:
        return datetime.datetime.fromtimestamp(float(sec), tz=zone()).strftime(pattern)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def days_ago(ms) -> int:
    """Whole calendar days between an epoch-ms moment and today, in the zone
    ("today" = 0, "yesterday" = 1), not 24-hour blocks."""
    return max((today() - from_ms(ms).date()).days, 0)
