"""Smoke tests for `_public_view`, the single seam between stored run records
and the rendered dashboard. The chat pillar was removed; this projection no
longer carries any third-party content. Tests stay as a guard rail in case
record-level redaction needs to be reintroduced later.
"""
from __future__ import annotations

from oracle.dashboard.main import (
    _historical_chart_payload,
    _public_view,
    _samples_from_record,
)


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


def _make_record(*, samples: list[dict] | None) -> dict:
    return {"ground_truth": {"machine": {"samples": samples} if samples is not None else None}}


def test_samples_from_record_parses_iso_timestamps():
    record = _make_record(samples=[
        {"t": "2026-04-22T08:00:00", "avg_kt": 5.0, "gust_kt": 8.0},
        {"t": "2026-04-22T12:00:00", "avg_kt": 14.0, "gust_kt": 19.0},
    ])
    samples = _samples_from_record(record)
    assert len(samples) == 2
    assert samples[0].avg_knots == 5.0
    assert samples[1].gust_knots == 19.0


def test_samples_from_record_handles_missing_block():
    assert _samples_from_record(None) == []
    assert _samples_from_record({}) == []
    assert _samples_from_record(_make_record(samples=None)) == []


def test_samples_from_record_skips_malformed_rows():
    record = _make_record(samples=[
        {"t": "2026-04-22T08:00:00", "avg_kt": 5.0, "gust_kt": 8.0},
        {"t": "not-a-date", "avg_kt": 1.0, "gust_kt": 2.0},
        {"avg_kt": 3.0},  # missing keys
    ])
    samples = _samples_from_record(record)
    assert len(samples) == 1


def test_historical_chart_payload_returns_per_lang_svg():
    record = _make_record(samples=[
        {"t": "2026-04-22T08:00:00", "avg_kt": 5.0, "gust_kt": 8.0},
        {"t": "2026-04-22T12:00:00", "avg_kt": 14.0, "gust_kt": 19.0},
    ])
    payload = _historical_chart_payload(record)
    assert payload is not None
    assert set(payload["chart_svg"].keys()) == {"de", "en"}
    assert payload["chart_svg"]["de"].startswith("<svg")


def test_historical_chart_payload_returns_none_when_too_few_samples():
    record = _make_record(samples=[
        {"t": "2026-04-22T08:00:00", "avg_kt": 5.0, "gust_kt": 8.0},
    ])
    assert _historical_chart_payload(record) is None
    assert _historical_chart_payload(None) is None
