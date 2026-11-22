"""Settings → Timezone: every date bucket uses the household's zone, not the
container's. The field case: a book finished at 07:30 in Melbourne on
2026-10-12 is 2026-10-11 20:30 UTC — in a UTC container it landed on the 11th."""
import datetime
import time

import pytest

from fakes import SAME_ORIGIN
from stackarr import absclient, db, tz

MEL = "Australia/Melbourne"
FINISH = datetime.datetime(2026, 10, 11, 20, 30, tzinfo=datetime.timezone.utc)   # 07:30 AEDT on the 12th
FINISH_MS = int(FINISH.timestamp() * 1000)


@pytest.fixture
def utc_process(monkeypatch):
    """The container's own clock is UTC (as on CT 100 before TZ was set)."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    time.tzset()


@pytest.fixture
def melbourne(tmp_db, monkeypatch, utc_process):
    db.set_meta("timezone", MEL)
    # "now" = 09:00 the same Melbourne morning
    fixed = datetime.datetime(2026, 10, 12, 9, 0, tzinfo=tz._zone(MEL))
    monkeypatch.setattr(tz, "now", lambda: fixed)
    return fixed


def test_zone_falls_back_from_setting_to_tz_env_to_utc(tmp_db, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert tz.name() == "UTC"
    monkeypatch.setenv("TZ", MEL)
    assert tz.name() == MEL
    db.set_meta("timezone", "Europe/Berlin")
    assert tz.name() == "Europe/Berlin"
    db.set_meta("timezone", "../../etc/passwd")                 # never a path
    assert tz.name() == MEL


def test_a_melbourne_morning_finish_lands_on_its_own_day(melbourne):
    assert tz.day_of_ms(FINISH_MS) == "2026-10-12"
    assert tz.days_ago(FINISH_MS) == 0                          # "today", not "yesterday"
    db.set_meta("timezone", "UTC")
    assert tz.day_of_ms(FINISH_MS) == "2026-10-11"


def test_stored_timestamps_convert_from_process_local(melbourne):
    # SQLite datetime('now','localtime') in a UTC process wrote this
    assert tz.db_day("2026-10-11 20:30:00") == "2026-10-12"
    assert tz.fmt("2026-10-11 20:30:00") == "2026-10-12 07:30"


def test_insights_heatmap_and_goal_use_the_zone(admin_client, melbourne, monkeypatch):
    with db.conn() as c:
        c.execute("UPDATE users SET abs_token='tok' WHERE id=?", (admin_client.uid,))
        c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES "
                  "('li_cap','The Captain: The Last Horizon, Book 1','Will Wight','','audiobook')")
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: [
        {"item_id": "li_cap", "finished": True, "progress": 1, "last_update": FINISH_MS, "reading": False}])
    monkeypatch.setattr(absclient, "listening_stats", lambda tok: {"total_seconds": 0})
    db.set_meta(f"goal_{admin_client.uid}", "24")
    html = admin_client.get("/insights").get_data(as_text=True)
    assert 'title="2026-10-12: 1 finished"' in html and 'title="2026-10-11: 0 finished"' in html
    assert '<span class="goal-bar-num">1</span>' in html


def test_settings_offers_a_searchable_zone_list_and_rejects_junk(admin_client, monkeypatch):
    monkeypatch.setenv("TZ", MEL)
    html = admin_client.get("/settings").get_data(as_text=True)
    assert 'id="tz-input" list="tz-list" value="Australia/Melbourne"' in html
    assert '<option value="Australia/Melbourne">' in html and '<option value="Europe/Berlin">' in html
    bad = admin_client.post("/api/settings", json={"timezone": "Mars/Olympus"}, headers=SAME_ORIGIN)
    assert bad.status_code == 400 and db.get_meta("timezone", "") == ""
    ok = admin_client.post("/api/settings", json={"timezone": "Europe/Berlin"}, headers=SAME_ORIGIN)
    assert ok.get_json()["ok"] and tz.name() == "Europe/Berlin"


def test_request_history_times_are_shown_in_the_zone(admin_client, melbourne):
    with db.conn() as c:
        for st, when in (("failed", "2026-10-11 20:30:00"), ("handed", "2026-10-11 22:00:00")):
            c.execute("INSERT INTO requests (user_id,asin,title,author,status,format,created_at,updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?)", (admin_client.uid, "B0F8CJQZM3", "The Pilot", "Will Wight",
                                                    st, "audiobook", when, when))
    html = admin_client.get("/requests").get_data(as_text=True)
    assert "2026-10-12 07:30" in html
