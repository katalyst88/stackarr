"""Pending picks are retired once they're owned, requested or finished — and a
full queue of stale picks no longer stops the recommender. Field case: "The
Captain" (The Last Horizon #1) finished in Audiobookshelf and in the library as
"The Captain: The Last Horizon, Book 1", still offered as a fresh pick, with
the queue at its 30-pick cap so no new picks were ever generated."""
import pytest

from fakes import SAME_ORIGIN
from stackarr import absclient, config, db, picks, recommend, scheduler


def _user(name="reader", role="user", token="tok"):
    with db.conn() as c:
        return c.execute("INSERT INTO users (username, role, abs_token) VALUES (?,?,?)",
                         (name, role, token)).lastrowid


def _pick(uid, title, author, asin="", fmt="audiobook", lane="author"):
    with db.conn() as c:
        return c.execute("INSERT INTO suggestions (user_id,asin,title,author,lane,format,score) "
                         "VALUES (?,?,?,?,?,?,1)", (uid, asin, title, author, lane, fmt)).lastrowid


def _lib(item_id, title, author, fmt="audiobook", asin=""):
    with db.conn() as c:
        c.execute("INSERT INTO library (item_id,title,author,asin,format) VALUES (?,?,?,?,?)",
                  (item_id, title, author, asin, fmt))


def _status(sid):
    with db.conn() as c:
        return c.execute("SELECT status FROM suggestions WHERE id=?", (sid,)).fetchone()["status"]


def _signals(uid):
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) n FROM signals WHERE user_id=?", (uid,)).fetchone()["n"]


def test_owned_pick_is_retired_as_owned_not_rejected(tmp_db):
    uid = _user()
    sid = _pick(uid, "The Captain", "Will Wight")
    _lib("li_1", "The Captain: The Last Horizon, Book 1", "Will Wight")
    assert picks.retire(uid) == {"owned": 1}
    assert _status(sid) == "owned"
    assert _signals(uid) == 0              # not a "not interested"


def test_owned_is_format_scoped(tmp_db):
    uid = _user()
    ebook_pick = _pick(uid, "The Captain", "Will Wight", fmt="ebook")
    _lib("li_1", "The Captain: The Last Horizon, Book 1", "Will Wight", fmt="audiobook")
    assert picks.retire(uid) == {}
    assert _status(ebook_pick) == "pending"   # owning the audiobook isn't owning the eBook


def test_requested_outside_the_card_is_retired(tmp_db):
    uid = _user()
    sid = _pick(uid, "The Engineer", "Will Wight")
    both = _pick(uid, "The Knight", "Will Wight", fmt="ebook")
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,title,author,status,format) VALUES (?,?,?,?,?)",
                  (uid, "The Engineer", "Will Wight", "handed", "audiobook"))
        c.execute("INSERT INTO requests (user_id,title,author,status,format) VALUES (?,?,?,?,?)",
                  (uid, "The Knight", "Will Wight", "pending_approval", "both"))
    assert picks.retire(uid) == {"requested": 2}
    assert _status(sid) == _status(both) == "requested"


@pytest.mark.parametrize("status", ["failed", "denied"])
def test_failed_or_denied_request_leaves_the_pick(tmp_db, status):
    uid = _user()
    sid = _pick(uid, "The Pilot", "Will Wight")
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,title,author,status,format) VALUES (?,?,?,?,?)",
                  (uid, "The Pilot", "Will Wight", status, "audiobook"))
    assert picks.retire(uid) == {}
    assert _status(sid) == "pending"


def test_another_users_request_doesnt_retire_my_pick(tmp_db):
    me, other = _user("me"), _user("other")
    sid = _pick(me, "The Engineer", "Will Wight")
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,title,author,status,format) VALUES (?,?,?,?,?)",
                  (other, "The Engineer", "Will Wight", "handed", "audiobook"))
    assert picks.retire(me) == {}
    assert _status(sid) == "pending"


def test_finished_in_abs_is_retired_across_formats(tmp_db, monkeypatch):
    uid = _user()
    _lib("li_1", "The Captain: The Last Horizon, Book 1", "Will Wight")
    audio = _pick(uid, "The Captain", "Will Wight", asin="B0BVMW49D1")
    ebook = _pick(uid, "The Captain", "Will Wight", asin="gb:abc", fmt="ebook")
    monkeypatch.setattr(absclient, "listening_history",
                        lambda tok: [{"item_id": "li_1", "finished": True, "progress": 1, "last_update": 1}])
    assert picks.retire_for(db.get_user(uid)) == {"finished": 2}
    assert _status(audio) == _status(ebook) == "finished"


def test_read_shelf_counts_as_finished(tmp_db):
    uid = _user()
    sid = _pick(uid, "Mother of Learning", "Domagoj Kurmaić", fmt="ebook")
    db.shelf_set(uid, "t-mother-of-learning-domagoj-kurmai", "read", "Mother of Learning", "Domagoj Kurmaić")
    assert picks.retire(uid) == {"finished": 1}
    assert _status(sid) == "finished"


@pytest.mark.parametrize("title,author", [
    ("The Captain", "Someone Else"),               # same title, other author
    ("The Captain", ""),                           # no author -> no title match
    ("House", "Will Wight"),                       # a prefix is not the work
])
def test_lookalikes_are_not_retired(tmp_db, title, author):
    uid = _user()
    sid = _pick(uid, title, author)
    _lib("li_1", "The Captain: The Last Horizon, Book 1", "Will Wight")
    _lib("li_2", "House of Blades (Unabridged)", "Will Wight")
    assert picks.retire(uid) == {}
    assert _status(sid) == "pending"


def test_retired_picks_are_not_resuggested(tmp_db):
    uid = _user()
    _pick(uid, "The Captain", "Will Wight", asin="B0BVMW49D1")
    _lib("li_1", "The Captain: The Last Horizon, Book 1", "Will Wight")
    picks.retire(uid)
    # the recommender's dedupe still sees the retired row
    added = recommend._finalize(uid, {"B0BVMW49D1": {
        "cand": {"asin": "B0BVMW49D1", "title": "The Captain", "author": "Will Wight"},
        "score": 9, "lane": "author", "reason": "x"}}, None, {}, 5)
    assert added == 0


def test_full_queue_of_stale_picks_no_longer_blocks_the_recommender(tmp_db, monkeypatch):
    uid = _user()
    monkeypatch.setattr(config, "SUGGEST_MAX_PENDING", 3)
    monkeypatch.setattr(absclient, "listening_history", lambda tok: [])
    for i in range(3):
        _pick(uid, f"Owned Book {i}", "Will Wight", asin=f"B0OWNED00{i}")
        _lib(f"li_{i}", f"Owned Book {i}", "Will Wight", asin=f"B0OWNED00{i}")
    room = []
    monkeypatch.setattr(recommend, "run", lambda user_id, max_new=None: room.append(max_new) or 0)
    db.set_meta("formats", "audiobook")
    scheduler.run_for_user(uid, force=True)
    assert room == [3]                       # before: pending == cap -> returned without running


def test_library_refresh_retires_for_every_user(tmp_db, monkeypatch):
    a, b = _user("a"), _user("b")
    sa, sb = _pick(a, "The Captain", "Will Wight"), _pick(b, "The Captain", "Will Wight")
    _lib("li_1", "The Captain: The Last Horizon, Book 1", "Will Wight")
    monkeypatch.setattr(absclient, "listening_history", lambda tok: [])
    monkeypatch.setattr(scheduler.backends, "sources", lambda mode: [])
    scheduler.refresh_library()
    assert _status(sa) == _status(sb) == "owned"


def _signed_in(client_app, uid):
    client = client_app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    return client


def test_home_drops_a_finished_pick_and_book_page_says_finished(tmp_db, monkeypatch):
    from stackarr import audible, audnexus, create_app, tagging
    uid = _user(role="admin")
    stale = _pick(uid, "The Captain", "Will Wight", asin="B0BVMW49D1")
    fresh = _pick(uid, "Iron Prince", "Bryce O'Connor", asin="B0FRESH001")
    _lib("li_1", "The Captain: The Last Horizon, Book 1", "Will Wight")
    calls = []
    monkeypatch.setattr(absclient, "listening_history", lambda tok: calls.append(tok) or [
        {"item_id": "li_1", "finished": True, "progress": 1, "last_update": 1}])
    monkeypatch.setattr(scheduler, "start", lambda: None)
    app = create_app()
    app.testing = True
    client = _signed_in(app, uid)

    html = client.get("/home").get_data(as_text=True)
    assert "Iron Prince" in html and "Fresh picks" in html
    assert f"Stackarr.decide({stale}," not in html
    assert _status(stale) == "finished" and _status(fresh) == "pending"
    assert len(calls) == 1                  # one ABS history fetch for the whole page

    monkeypatch.setattr(audible, "by_asin", lambda a: {"asin": a, "title": "The Captain", "author": "Will Wight"})
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    page = client.get("/book/B0BVMW49D1").get_data(as_text=True)
    assert "✓ Finished" in page
