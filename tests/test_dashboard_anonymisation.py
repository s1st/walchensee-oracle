"""Regression test: the dashboard projection must never emit windinfo usernames.

Logs in GCS keep full chat fields for calibration. Only the public HTML is
scrubbed — both the `author` field (stripped entirely) and any `@handle`
mentions embedded in message text.
"""
from __future__ import annotations

from oracle.dashboard.main import _public_view


def test_public_view_drops_author_field():
    record = {
        "chat_messages": [
            {"author": "SunsetSlalom", "posted_at": "2026-04-22T05:00", "text": "looks ok"},
        ]
    }
    result = _public_view(record)
    assert "author" not in result["chat_messages"][0]
    assert "channel" not in result["chat_messages"][0]


def test_public_view_strips_at_mentions_from_text():
    record = {
        "chat_messages": [
            {"author": "Fred", "posted_at": "2026-04-22T05:00",
             "text": "@SunsetSlalom: danke für das @Walchi-Update, war genau richtig"},
        ]
    }
    text = _public_view(record)["chat_messages"][0]["text"]
    assert "SunsetSlalom" not in text
    assert "Walchi" not in text
    assert text == "@…: danke für das @… richtig" or text.startswith("@…: danke für das @…")


def test_public_view_returns_none_for_none():
    assert _public_view(None) is None


def test_public_view_preserves_verdict_fields():
    record = {
        "overall": "no_go",
        "verdicts": [{"rule": "x", "signal": "no_go", "reason": "y"}],
        "chat_messages": [],
    }
    result = _public_view(record)
    assert result["overall"] == "no_go"
    assert result["verdicts"] == record["verdicts"]
