"""What a download needs (needs.py): Chaptarr's raw rejection and queue words
→ a reason a reader understands, without naming any tracker."""
import json

import pytest

from stackarr import config, needs


@pytest.mark.parametrize("raw, key", [
    # release rejections (what Chaptarr says about each release it found)
    ("Indexer VIP-only torrent requires VIP membership", "vip"),
    ("Release is for Elite members only", "vip"),
    ("User class Power User too low for this torrent", "vip"),
    ("Only freeleech releases are allowed", "freeleech"),
    ("Not freeleech and no FL wedge available", "freeleech"),
    ("Ratio too low to download", "ratio"),
    ("Insufficient upload credit", "ratio"),
    ("Too many unsatisfied torrents", "ratio"),
    ("Not enough bonus points to buy this", "bonus"),
    ("Not enough seeders: 0. Minimum seeders: 1", "no_seeders"),
    ("Size 4.1 GB is larger than maximum allowed 2 GB", "profile"),
    ("Quality EPUB is not wanted in profile", "profile"),
    ("Wrong language: German", "profile"),
    ("Indexer is unavailable due to failures", "indexer_down"),
    ("Rate limit reached, try again later", "indexer_down"),
    ("HTTP 503 Service Unavailable", "indexer_down"),
    # download-client state (Chaptarr's queue messages)
    ("Tracker: Unregistered torrent", "removed"),
    ("Torrent not found on tracker", "removed"),
    ("The download is stalled with no connections", "no_seeders"),
    ("Tracker: your IP is not registered with this tracker", "connectable"),
    ("Not connectable: port closed", "connectable"),
    ("Seedbox IP not authorised", "connectable"),
    ("Client is not whitelisted on this tracker", "client"),
    ("Banned client version", "client"),
])
def test_each_reason_reads_in_plain_words(raw, key):
    got = needs.classify([raw])
    assert [n["key"] for n in got] == [key], raw
    assert got[0]["label"] and got[0]["text"] and got[0]["raw"] == raw


def test_a_size_number_is_not_an_outage():
    assert needs.classify(["Size 502 MB below minimum"])[0]["key"] == "profile"


def test_unknown_words_are_shown_as_they_are():
    got = needs.classify(["Blocklisted release", "Blocklisted release"])
    assert got == [{"key": "other", "label": "Needs attention", "text": "Blocklisted release",
                    "scope": "release", "raw": "Blocklisted release"}]


def test_one_need_per_kind_in_rule_order():
    got = needs.classify(["Ratio too low", "VIP only", "VIP-only torrent", "Something odd"])
    assert [n["key"] for n in got] == ["vip", "ratio", "other"]
    assert needs.badge(got) == "Needs VIP +2"
    assert needs.retry_hours(got) == 24
    assert needs.cleared_by_settings(got)
    assert not needs.cleared_by_settings([needs.no_release("ebook")])
    assert needs.retry_hours([needs._need(needs.rule("indexer_down"))]) == 1


def test_no_release_in_the_format_asked_for():
    n = needs.no_release("ebook")
    assert n["label"] == "No ebook release" and "ebook" in n["text"]


def test_rules_file_adds_and_replaces(tmp_path, monkeypatch):
    f = tmp_path / "rules.json"
    f.write_text(json.dumps([
        {"key": "vip", "label": "Needs Gold", "text": "Gold members only.", "scope": "account",
         "patterns": [r"gold only"]},
        {"key": "hnr", "label": "Hit and run", "text": "Seed what you have first.", "scope": "account",
         "patterns": [r"seed obligations"]},
    ]))
    monkeypatch.setattr(config, "NEEDS_RULES_FILE", str(f))
    needs.reload()
    try:
        assert needs.classify(["Gold only torrent"])[0]["label"] == "Needs Gold"
        assert needs.classify(["VIP only"])[0]["key"] == "other"          # replaced: the old words no longer match
        assert needs.classify(["Unmet seed obligations"])[0]["key"] == "hnr"   # new keys are tried first
    finally:
        monkeypatch.setattr(config, "NEEDS_RULES_FILE", "")
        needs.reload()


def test_a_broken_rules_file_falls_back_to_the_built_ins(tmp_path, monkeypatch):
    f = tmp_path / "rules.json"
    f.write_text(json.dumps([{"key": "x", "patterns": ["("]}]))
    monkeypatch.setattr(config, "NEEDS_RULES_FILE", str(f))
    needs.reload()
    try:
        assert needs.classify(["VIP only"])[0]["key"] == "vip"
    finally:
        monkeypatch.setattr(config, "NEEDS_RULES_FILE", "")
        needs.reload()
