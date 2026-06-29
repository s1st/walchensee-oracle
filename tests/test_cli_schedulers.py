"""Gating for the piggybacked cache refreshers in the CLI.

`_refresh_stats_panel_weekly_best_effort` rides the nightly backfill job but
must only fire on Sundays, in-season, in a cloud environment — and never raise
(it's a vanity refresh that must not break the backfill). These tests pin that
contract without touching Open-Meteo, the runs bucket, or the ML bundle.
"""
from __future__ import annotations

from datetime import date, timedelta

import oracle.calibration as calibration
import oracle.logger as logger_mod
import oracle.replay as replay_mod
import oracle.stats_cache as stats_cache
from oracle import cli

SUNDAY_IN_SEASON = date(2026, 6, 28)   # Sunday, June → in ACTIVE_SEASON_MONTHS
MONDAY_IN_SEASON = date(2026, 6, 29)   # Monday, June
SUNDAY_OFF_SEASON = date(2026, 1, 11)  # Sunday, January → out of season


def _wire(monkeypatch, today: date, *, cloud: bool):
    """Pin date.today(), the cloud-env signal, and spy on the refresh chain.

    Returns a dict of call-counters so a test can assert whether the heavy
    replay → rescore → stats-update chain ran.
    """
    class _FakeDate(date):
        @classmethod
        def today(cls) -> date:
            return today

    monkeypatch.setattr(cli, "date", _FakeDate)
    for var in ("CLOUD_RUN_JOB", "K_SERVICE", "LOG_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    if cloud:
        monkeypatch.setenv("CLOUD_RUN_JOB", "oracle-backfill")

    calls: dict = {"replay": [], "rescore": [], "stats": 0}

    async def fake_replay(start, end, **kw):
        calls["replay"].append((start, end))

    def fake_rescore(**kw):
        calls["rescore"].append(kw)
        return {"rewritten": [], "skipped": [], "flipped": [], "unchanged": []}

    def fake_stats(store=None):
        calls["stats"] += 1
        return {"n": 1234}

    monkeypatch.setattr(replay_mod, "run_replay_batch", fake_replay)
    monkeypatch.setattr(calibration, "rescore_all", fake_rescore)
    monkeypatch.setattr(stats_cache, "write_cache", fake_stats)
    monkeypatch.setattr(logger_mod, "default_store", lambda: object())
    return calls


def test_weekly_foldin_runs_on_sunday_in_season_in_cloud(monkeypatch):
    calls = _wire(monkeypatch, SUNDAY_IN_SEASON, cloud=True)
    cli._refresh_stats_panel_weekly_best_effort()
    assert calls["stats"] == 1
    assert calls["rescore"] and calls["rescore"][0]["replayed"] is True
    # Trailing-14-day window, rescore restricted to the same cutoff.
    cutoff = SUNDAY_IN_SEASON - timedelta(days=14)
    assert calls["replay"] == [(cutoff, SUNDAY_IN_SEASON)]
    assert calls["rescore"][0]["since"] == cutoff


def test_weekly_foldin_skips_on_non_sunday(monkeypatch):
    calls = _wire(monkeypatch, MONDAY_IN_SEASON, cloud=True)
    cli._refresh_stats_panel_weekly_best_effort()
    assert calls == {"replay": [], "rescore": [], "stats": 0}


def test_weekly_foldin_skips_off_season(monkeypatch):
    calls = _wire(monkeypatch, SUNDAY_OFF_SEASON, cloud=True)
    cli._refresh_stats_panel_weekly_best_effort()
    assert calls == {"replay": [], "rescore": [], "stats": 0}


def test_weekly_foldin_skips_outside_cloud(monkeypatch):
    calls = _wire(monkeypatch, SUNDAY_IN_SEASON, cloud=False)
    cli._refresh_stats_panel_weekly_best_effort()
    assert calls == {"replay": [], "rescore": [], "stats": 0}


def test_weekly_foldin_swallows_errors(monkeypatch):
    _wire(monkeypatch, SUNDAY_IN_SEASON, cloud=True)

    def boom(store=None):
        raise RuntimeError("GCS down")

    monkeypatch.setattr(stats_cache, "write_cache", boom)
    # Must not raise — the backfill must survive a failed fold-in.
    cli._refresh_stats_panel_weekly_best_effort()
