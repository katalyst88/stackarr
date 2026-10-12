"""The book page shows its grab actions once. The below-the-fold bar used to
repeat them, so every Add button appeared twice; it now only scrolls back up."""
import pytest

from stackarr import audible, audnexus, db, tagging


@pytest.mark.parametrize("mode,label", [("both", 'Stackarr.grabFormat('), ("audiobook", 'Stackarr.grabFormat(')])
def test_add_buttons_appear_once(admin_client, monkeypatch, mode, label):
    db.set_meta("formats", mode)
    monkeypatch.setattr(audible, "by_asin", lambda a: {"asin": a, "title": "Soulsmith", "author": "Will Wight"})
    monkeypatch.setattr(audnexus, "book", lambda a: None)
    monkeypatch.setattr(tagging, "fetch_for", lambda *a, **k: {})
    html = admin_client.get("/book/B0SOULSMTH").get_data(as_text=True)
    bottom = html.split('class="book-actions-bottom"', 1)[1].split("</div>", 1)[0]
    assert "Back to top" in bottom and label not in bottom
    # one Request per format row (work-first page: an Audiobook and an eBook row in both mode)
    assert html.count(label) == (2 if mode == "both" else 1)
