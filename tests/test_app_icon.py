"""The Stackarr icon stays in view. It lived only in the sidebar's brand row,
and the sidebar hid it twice: collapsed (`body.nav-collapsed .brand img
{display:none}`) and at ≤820 px, where the sidebar becomes the bottom tab bar
and `.brand` is hidden — so on a phone, or with the menu collapsed, there was
no Stackarr icon anywhere on the page."""
import os
import re

CSS = open(os.path.join(os.path.dirname(__file__), "..", "stackarr", "static", "style.css"), encoding="utf-8").read()


def test_topbar_carries_the_icon(admin_client, monkeypatch):
    from stackarr import absclient
    monkeypatch.setattr(absclient, "listening_history", lambda tok, **k: [])
    html = admin_client.get("/home").get_data(as_text=True)
    assert re.search(r'<a class="topbar-brand" href="/home"[^>]*><img src="/static/icon.svg"', html)


def test_collapsed_sidebar_keeps_the_logo():
    rule = next(l for l in CSS.splitlines() if l.startswith("body.nav-collapsed .navlink span"))
    assert ".brand img" not in rule


def test_phone_layout_shows_the_topbar_icon():
    phone = CSS.split("@media (max-width: 820px)", 1)[1].split("\n}\n", 1)[0]
    assert ".topbar-brand { display: block; }" in phone
    assert ".topbar-brand { display: none;" in CSS.split("@media (max-width: 820px)", 1)[0]
