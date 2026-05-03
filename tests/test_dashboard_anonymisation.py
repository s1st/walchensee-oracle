"""Smoke tests for `_public_view`, the single seam between stored run records
and the rendered dashboard. The chat pillar was removed; this projection no
longer carries any third-party content. Tests stay as a guard rail in case
record-level redaction needs to be reintroduced later.
"""
from __future__ import annotations

from oracle.dashboard.main import _public_view


def test_public_view_returns_none_for_none():
    assert _public_view(None) is None


def test_public_view_preserves_verdict_fields():
    record = {
        "overall": "no_go",
        "verdicts": [{"rule": "x", "signal": "no_go", "reason": "y"}],
    }
    result = _public_view(record)
    assert result["overall"] == "no_go"
    assert result["verdicts"] == record["verdicts"]


def test_public_view_drops_legacy_chat_messages_field():
    """Older logs (pre-chat-removal) still have `chat_messages` on disk —
    the projection drops it so the template never sees stale third-party text."""
    record = {
        "overall": "go",
        "chat_messages": [{"author": "x", "text": "anything"}],
    }
    assert "chat_messages" not in _public_view(record)
