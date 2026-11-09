"""A new attempt on a reused request row starts clean: the last release
check ("what this download needed") was about the previous attempt."""
import json

from stackarr import db, releasecheck


def test_a_new_attempt_clears_the_last_release_check(tmp_db):
    with db.conn() as c:
        rid = db.record_request(c, 1, {"title": "The Pilot", "author": "Will Wight"}, "ebook", "available")
    db.set_meta(f"relcheck:{rid}", json.dumps({"kind": "none", "needs": [{"key": "no_release"}], "at": 1}))
    with db.conn() as c:
        again = db.record_request(c, 1, {"title": "The Pilot", "author": "Will Wight"}, "ebook", "handed")
    assert again == rid and releasecheck.result(rid) is None
