"""Per-day chat sentiment: the badge on Today / Tomorrow / Day after should
reflect what the community said ABOUT that day, not the same message pool."""
from __future__ import annotations

from datetime import date

from oracle.dashboard.main import (
    _chat_sentiment,
    _infer_day_reference,
    _messages_for_day,
)


def _msg(posted: str, text: str) -> dict:
    return {"posted_at": posted, "text": text, "author": "x"}


def test_infer_morgen_is_posted_plus_one():
    m = _msg("2026-04-22T08:00:00", "Walchensee morgen sollte gut laufen")
    assert _infer_day_reference(m) == date(2026, 4, 23)


def test_infer_uebermorgen_is_posted_plus_two():
    m = _msg("2026-04-22T08:00:00", "Walchensee übermorgen wird super")
    assert _infer_day_reference(m) == date(2026, 4, 24)


def test_infer_uebermorgen_takes_priority_over_morgen():
    m = _msg("2026-04-22T08:00:00", "Walchensee übermorgen, nicht morgen")
    assert _infer_day_reference(m) == date(2026, 4, 24)


def test_infer_heute_is_posted_day():
    m = _msg("2026-04-22T08:00:00", "Walchensee heute — Thermik läuft")
    assert _infer_day_reference(m) == date(2026, 4, 22)


def test_infer_weekday_means_next_occurrence():
    # Wednesday 2026-04-22, mentioning Freitag → 2026-04-24.
    m = _msg("2026-04-22T08:00:00", "Walchensee Freitag sieht top aus")
    assert _infer_day_reference(m) == date(2026, 4, 24)


def test_infer_no_reference_returns_none():
    m = _msg("2026-04-22T08:00:00", "Walchensee, wie sind die Bedingungen?")
    assert _infer_day_reference(m) is None


def test_messages_without_reference_default_to_today():
    today = date(2026, 4, 22)
    msgs = [
        _msg("2026-04-22T08:00:00", "Walchensee — weiß jemand was?"),
        _msg("2026-04-22T08:30:00", "Walchensee morgen sieht gut aus"),
    ]
    for_today = _messages_for_day(msgs, today, today)
    for_tomorrow = _messages_for_day(msgs, date(2026, 4, 23), today)
    assert len(for_today) == 1
    assert "weiß jemand" in for_today[0]["text"]
    assert len(for_tomorrow) == 1
    assert "morgen sieht" in for_tomorrow[0]["text"]


def test_sentiment_differs_across_days_from_same_pool():
    today = date(2026, 4, 22)
    msgs = [
        _msg("2026-04-22T07:00:00", "Walchensee heute: nicht gelohnt, flaute"),
        _msg("2026-04-22T07:30:00", "Walchensee morgen läuft super, richtig Thermik"),
        _msg("2026-04-22T08:00:00", "Walchensee übermorgen? bleibt aus, tot"),
    ]
    today_sent = _chat_sentiment(_messages_for_day(msgs, today, today))
    tomorrow_sent = _chat_sentiment(_messages_for_day(msgs, date(2026, 4, 23), today))
    dayafter_sent = _chat_sentiment(_messages_for_day(msgs, date(2026, 4, 24), today))

    assert today_sent["code"] == "negative"
    assert tomorrow_sent["code"] == "positive"
    assert dayafter_sent["code"] == "negative"
