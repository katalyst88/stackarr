"""Shared test fixtures. No network: Chaptarr is a small in-memory fake that
the HTTP helpers are monkeypatched onto, and every test gets its own SQLite DB.

Run from the repo root:  pip install -r requirements-dev.txt && pytest"""
import os
import tempfile

# config reads STACKARR_DATA at import time — point it somewhere disposable
# before anything imports the package.
os.environ.setdefault("STACKARR_DATA", tempfile.mkdtemp(prefix="stackarr-test-"))
os.environ.setdefault("STACKARR_NO_SCHED", "1")
# create_app() refuses to start without these; nothing ever connects to them
for _k, _v in (("ABS_URL", "http://abs.test"), ("ABS_ADMIN_TOKEN", "test"),
               ("CHAPTARR_URL", "http://chaptarr.test"), ("CHAPTARR_API_KEY", "test-key")):
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402

from fakes import CHAPTARR, FakeChaptarr  # noqa: E402
from stackarr import chaptarr, db  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A fresh, migrated database for one test."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "stackarr.db"))
    db.init()
    return db


@pytest.fixture
def fake_chaptarr(tmp_db, monkeypatch):
    fake = FakeChaptarr()
    db.set_meta("chaptarr_url", CHAPTARR)
    db.set_meta("chaptarr_api_key", "test-key")
    db.set_meta("chaptarr_root_folder", "/audiobooks")
    db.set_meta("chaptarr_ebook_root_folder", "/books")
    monkeypatch.setattr(chaptarr.requests, "get", fake.get)
    monkeypatch.setattr(chaptarr.requests, "post", fake.post)
    monkeypatch.setattr(chaptarr.requests, "put", fake.put)
    monkeypatch.setattr(chaptarr.time, "sleep", lambda s: None)
    return fake


@pytest.fixture
def admin_client(tmp_db):
    """A Flask test client signed in as an admin. Mutating requests need a
    same-origin header (the CSRF guard): headers=fakes.SAME_ORIGIN."""
    from stackarr import create_app
    app = create_app()
    app.testing = True
    with db.conn() as c:
        uid = c.execute("INSERT INTO users (username, role) VALUES ('admin', 'admin')").lastrowid
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    client.uid = uid
    return client
