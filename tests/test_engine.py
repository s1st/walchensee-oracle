"""Aggregator behaviour: consensus semantics — HARD vetos always block, SOFT
vetos downgrade only once two or more pile up, MAYBE emissions are advisory."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import patch

import pytest

from oracle.config import StationRole
from oracle.engine import aggregate, run_replay
from oracle.knowledge.rules import Severity, Signal, Verdict


def _v(signal: Signal, severity: Severity = Severity.NONE, rule: str = "x") -> Verdict:
    return Verdict(rule=rule, signal=signal, reason_en="", reason_de="", severity=severity)


def test_all_go_aggregates_to_go():
    assert aggregate([_v(Signal.GO), _v(Signal.GO)]) is Signal.GO


def test_hard_veto_blocks_everything():
    verdicts = [_v(Signal.GO), _v(Signal.NO_GO, Severity.HARD), _v(Signal.GO)]
    assert aggregate(verdicts) is Signal.NO_GO


def test_hard_wins_over_soft():
    verdicts = [
        _v(Signal.NO_GO, Severity.SOFT, rule="overnight_cooling"),
        _v(Signal.NO_GO, Severity.HARD, rule="foehn_override"),
        _v(Signal.GO),
    ]
    assert aggregate(verdicts) is Signal.NO_GO


def test_single_soft_veto_does_not_downgrade():
    # The whole point of consensus aggregation: one over-sensitive rule
    # shouldn't override eight rules that say GO.
    verdicts = [_v(Signal.GO)] * 8 + [_v(Signal.NO_GO, Severity.SOFT)]
    assert aggregate(verdicts) is Signal.GO


def test_two_soft_vetos_downgrade_to_maybe():
    # Two converging negative signals = real concern, downgrade.
    verdicts = [_v(Signal.GO)] * 7 + [_v(Signal.NO_GO, Severity.SOFT)] * 2
    assert aggregate(verdicts) is Signal.MAYBE


def test_many_soft_vetos_still_only_maybe():
    # Soft alone never reaches NO_GO regardless of count.
    verdicts = [_v(Signal.NO_GO, Severity.SOFT)] * 5
    assert aggregate(verdicts) is Signal.MAYBE


def test_maybe_emissions_alone_do_not_downgrade():
    # Rules emitting MAYBE are advisory; absent any SOFT NO_GO they don't
    # block consensus GO. (A rule that's genuinely confident in "no" should
    # emit NO_GO, not MAYBE.)
    verdicts = [_v(Signal.GO)] * 6 + [_v(Signal.MAYBE)] * 3
    assert aggregate(verdicts) is Signal.GO


def test_one_soft_plus_maybes_is_still_go():
    # Single soft veto plus any number of MAYBEs is below the downgrade
    # threshold — counts soft NO_GOs only.
    verdicts = [_v(Signal.GO)] * 5 + [_v(Signal.NO_GO, Severity.SOFT)] + [_v(Signal.MAYBE)] * 3
    assert aggregate(verdicts) is Signal.GO


# --- run_replay --------------------------------------------------------
# The replay path stitches historical-forecast pressure/meteo with the
# historical Urfeld buoy day-curve. We don't need the actual APIs in this
# test — we patch the three pillar calls and verify the dispatch + the
# resulting Forecast is tagged with replay metadata.


_REPLAY_DAY = date(2021, 6, 15)
_NOW = datetime(2021, 6, 15, 11, 0)


def _fake_pressure() -> Any:
    from oracle.pillars.pressure import PressureReading, PressureSnapshot
    return PressureSnapshot(
        thermik_north=PressureReading("Munich", 1018.4, _NOW),
        thermik_south=PressureReading("Innsbruck", 1016.0, _NOW),
        foehn_south=PressureReading("Bolzano", 1020.5, _NOW),
    )


def _fake_meteo() -> Any:
    from oracle.pillars.meteo import MeteoSnapshot
    return MeteoSnapshot(
        day=_REPLAY_DAY,
        overnight_cloud_cover_pct=20.0,
        morning_solar_radiation_wm2=750.0,
        synoptic_wind_knots=5.0,
        min_dew_point_spread_c=9.0,
        max_boundary_layer_height_m=1200.0,
        soil_moisture_m3m3=0.20,
        rained_yesterday=False,
        yesterday_precipitation_mm=0.0,
        max_lifted_index=2.0,
        min_lifted_index=2.0,
        max_cape_j_kg=0.0,
        max_daytime_low_cloud_pct=25.0,
        wind_850_direction_at_peak_deg=20.0,
        max_wind_700_knots=15.0,
        morning_air_temp_c=14.0,
    )


def _fake_buoy() -> list:
    from oracle.pillars.measurements import UrfeldSample
    return [
        UrfeldSample(measured_at=datetime(2021, 6, 15, 10, 0), avg_knots=3.0, gust_knots=5.0,
                     water_temp_c=18.0, air_temp_c=12.0),
        UrfeldSample(measured_at=datetime(2021, 6, 15, 14, 0), avg_knots=8.0, gust_knots=12.0,
                     water_temp_c=18.5, air_temp_c=15.0),
    ]


@pytest.mark.asyncio
async def test_run_replay_passes_archive_host_to_pillars():
    """The host param must reach both pressure and meteo with the archive URL."""
    calls: dict[str, Any] = {}

    async def fake_pressure(*, client, host, target_day):
        calls["pressure_host"] = host
        calls["pressure_target_day"] = target_day
        return _fake_pressure()

    async def fake_meteo(day, *, client, host):
        calls["meteo_host"] = host
        calls["meteo_day"] = day
        return _fake_meteo()

    async def fake_curve(day, *, client):
        calls["curve_day"] = day
        return _fake_buoy()

    with patch("oracle.engine.pressure.fetch_snapshot", side_effect=fake_pressure), \
         patch("oracle.engine.meteo.fetch_snapshot", side_effect=fake_meteo), \
         patch("oracle.engine.fetch_urfeld_day_curve", side_effect=fake_curve):
        result = await run_replay(_REPLAY_DAY, source="historical-forecast")

    from oracle.config import OPEN_METEO_HISTORICAL_FORECAST_URL
    assert calls["pressure_host"] == OPEN_METEO_HISTORICAL_FORECAST_URL
    assert calls["meteo_host"] == OPEN_METEO_HISTORICAL_FORECAST_URL
    assert calls["pressure_target_day"] == _REPLAY_DAY
    assert calls["meteo_day"] == _REPLAY_DAY
    assert calls["curve_day"] == _REPLAY_DAY

    assert result.replay_day == _REPLAY_DAY
    assert result.replay_source == "historical-forecast"
    # Live forecasts carry no replay metadata.
    assert result.overall in (Signal.GO, Signal.MAYBE, Signal.NO_GO)


@pytest.mark.asyncio
async def test_run_replay_reanalysis_source_picks_archive_url():
    from oracle.config import OPEN_METEO_ARCHIVE_URL
    calls: dict[str, Any] = {}

    async def fake_pressure(*, client, host, target_day):
        calls["pressure_host"] = host
        return _fake_pressure()

    async def fake_meteo(day, *, client, host):
        calls["meteo_host"] = host
        return _fake_meteo()

    with patch("oracle.engine.pressure.fetch_snapshot", side_effect=fake_pressure), \
         patch("oracle.engine.meteo.fetch_snapshot", side_effect=fake_meteo), \
         patch("oracle.engine.fetch_urfeld_day_curve", return_value=_fake_buoy()):
        result = await run_replay(_REPLAY_DAY, source="reanalysis")

    assert calls["pressure_host"] == OPEN_METEO_ARCHIVE_URL
    assert calls["meteo_host"] == OPEN_METEO_ARCHIVE_URL
    assert result.replay_source == "reanalysis"


@pytest.mark.asyncio
async def test_run_replay_projects_buoy_curve_into_engine_shapes():
    """The day-curve's last sample must surface as the WindReading +
    LakeTempSnapshot the engine consumes."""
    with patch("oracle.engine.pressure.fetch_snapshot", return_value=_fake_pressure()), \
         patch("oracle.engine.meteo.fetch_snapshot", return_value=_fake_meteo()), \
         patch("oracle.engine.fetch_urfeld_day_curve", return_value=_fake_buoy()):
        result = await run_replay(_REPLAY_DAY)

    # Last sample of the fixture has water_temp_c=18.5, avg_knots=8.0.
    assert len(result.winds) == 1
    w = result.winds[0]
    assert w.station == "Urfeld"
    assert w.role is StationRole.SHORE
    assert w.avg_knots == 8.0
    assert w.water_temp_c == 18.5
    assert result.lake_temp is not None
    assert result.lake_temp.surface_temp_c == 18.5


@pytest.mark.asyncio
async def test_run_replay_handles_empty_buoy_curve():
    """Buoy outage on the target day → no winds, no lake temp. The replay
    still completes; rules that need a buoy reading simply don't fire."""
    with patch("oracle.engine.pressure.fetch_snapshot", return_value=_fake_pressure()), \
         patch("oracle.engine.meteo.fetch_snapshot", return_value=_fake_meteo()), \
         patch("oracle.engine.fetch_urfeld_day_curve", return_value=[]):
        result = await run_replay(_REPLAY_DAY)

    assert result.winds == []
    assert result.lake_temp is None
    assert result.replay_day == _REPLAY_DAY
