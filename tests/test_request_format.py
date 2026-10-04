"""routes._request_format — a catalogue row's `format` is Audible's edition
string, not a media type."""
import pytest

from fakes import set_formats
from stackarr import routes


@pytest.mark.parametrize("value,want", [
    ("Unabridged", "both"), ("Original_Recording", "both"), (None, "both"),
    ("audiobook", "audiobook"), ("ebook", "ebook"), ("both", "both"), (" Ebook ", "ebook"),
])
def test_both_install(tmp_db, value, want):
    set_formats("both")
    assert routes._request_format({"format": value} if value is not None else {}) == want


def test_single_format_install_ignores_other_media(tmp_db):
    set_formats("audiobook")
    assert routes._request_format({"format": "ebook"}) == "audiobook"
    assert routes._request_format({"format": "both"}) == "audiobook"
    assert routes._request_format({"format": "Unabridged"}) == "audiobook"
