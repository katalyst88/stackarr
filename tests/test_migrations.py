"""db.init() migrations on an existing database."""
from stackarr import db


def _formats():
    with db.conn() as c:
        return {r["title"]: r["format"] for r in c.execute("SELECT title, format FROM requests")}


def test_edition_strings_in_request_format_become_audiobook(tmp_db):
    rows = {"a": "Unabridged", "b": "Abridged", "c": "Original_Recording", "d": "",
            "e": None, "f": "audiobook", "g": "ebook", "h": "both", "i": " Ebook "}
    with db.conn() as c:
        c.execute("DELETE FROM meta WHERE k='req_fmt_media'")    # an install from before it
        for title, fmt in rows.items():
            c.execute("INSERT INTO requests (user_id, title, format) VALUES (1, ?, ?)", (title, fmt))

    db.init()

    assert _formats() == {"a": "audiobook", "b": "audiobook", "c": "audiobook", "d": "audiobook",
                          "e": "audiobook", "f": "audiobook", "g": "ebook", "h": "both",
                          "i": "ebook"}


def test_runs_once(tmp_db):
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id, title, format) VALUES (1, 'x', 'Unabridged')")
    db.init()                                   # already migrated when the DB was created
    assert _formats() == {"x": "Unabridged"}
