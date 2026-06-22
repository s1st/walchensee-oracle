"""Forecast engine — pulls all pillars, runs rules, aggregates into a verdict."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

import httpx

from oracle.config import (
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_HISTORICAL_FORECAST_URL,
    SOFT_VETO_BAR,
    StationRole,
)
from oracle.knowledge import rules
from oracle.knowledge.rules import Severity, Signal, Verdict
from oracle.pillars import measurements, meteo, pressure
from oracle.pillars.measurements import (
    LakeTempSnapshot,
    UrfeldSample,
    WindReading,
)
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureSnapshot


ReplaySource = Literal["historical-forecast", "reanalysis"]

_REPLAY_HOSTS = {
    "historical-forecast": OPEN_METEO_HISTORICAL_FORECAST_URL,
    "reanalysis": OPEN_METEO_ARCHIVE_URL,
}


@dataclass
class Forecast:
    overall: Signal
    verdicts: list[Verdict]
    # Raw pillar inputs — kept so the calibration logger can replay / re-score
    # past decisions with different thresholds without re-fetching.
    pressure: PressureSnapshot
    meteo: MeteoSnapshot
    winds: list[WindReading]
    # Lake surface temperature from the most recent buoy reading. None when
    # the buoy is down or its latest usable row didn't carry `wtemp` —
    # tolerated, the air_lake_delta rule simply won't fire.
    lake_temp: LakeTempSnapshot | None
    # Replay metadata. `replay_day` is the day the rules were re-run for;
    # `replay_source` is which archive fed the pressure/meteo pillars.
    # Both None on live forecasts.
    replay_day: date | None = None
    replay_source: ReplaySource | None = None


def apply_rules(
    snapshot: PressureSnapshot,
    meteo_snap: MeteoSnapshot,
    winds: list[WindReading],
    lake_temp: LakeTempSnapshot | None,
    *,
    now: datetime | None = None,
) -> list[Verdict]:
    """Pure function: pillar snapshots in, fourteen verdicts out.

    Extracted so calibration tooling can re-run the rule layer against a
    record's stored `inputs` block without re-fetching the upstream APIs.
    `lake_temp` may be None when the buoy is down or its latest usable row
    lacked `wtemp`; the air_lake_delta rule handles that as MAYBE.

    `now` is the timestamp the air_lake_delta rule uses to check the
    buoy reading's staleness. Pass the replay day's noon for replay runs
    so a 4-year-old buoy reading isn't flagged as "stale" relative to
    the wall clock. Live runs leave it None to default to datetime.now().
    """
    return [
        rules.thermik(snapshot),
        rules.foehn_override(snapshot),
        rules.overnight_cooling(meteo_snap),
        rules.solar_radiation(meteo_snap),
        rules.dew_point_spread(meteo_snap),
        rules.boundary_layer_height(meteo_snap),
        rules.post_rain_moisture(meteo_snap),
        rules.atmospheric_stability(meteo_snap),
        rules.daytime_clouds(meteo_snap),
        rules.no_insolation(meteo_snap),
        rules.upper_level_wind(meteo_snap),
        rules.synoptic_override(meteo_snap),
        rules.thermal_ignition(winds),
        rules.air_lake_delta(lake_temp, meteo_snap, now=now),
    ]


async def run_forecast(day: date) -> Forecast:
    async with httpx.AsyncClient(timeout=10.0) as client:
        snapshot, meteo_snap, latest = await asyncio.gather(
            pressure.fetch_snapshot(client=client),
            meteo.fetch_snapshot(day, client=client),
            measurements.fetch_latest(client=client),
        )
    winds = latest.winds
    verdicts = apply_rules(snapshot, meteo_snap, winds, latest.lake_temp)
    return Forecast(
        overall=aggregate(verdicts),
        verdicts=verdicts,
        pressure=snapshot,
        meteo=meteo_snap,
        winds=winds,
        lake_temp=latest.lake_temp,
    )


async def run_replay(
    day: date,
    *,
    source: ReplaySource = "historical-forecast",
) -> Forecast:
    """Re-run the rules against the historical forecast (or reanalysis) for `day`.

    Pressure + meteo pull from the archive host. Wind readings are empty
    (no buoy source available); `thermal_ignition` and `air_lake_delta`
    will return MAYBE. The result is tagged `replay_day` + `replay_source`
    so the logger can route it to `runs/replay/<date>.json`.
    """
    host = _REPLAY_HOSTS[source]
    async with httpx.AsyncClient(timeout=15.0) as client:
        snapshot, meteo_snap = await asyncio.gather(
            pressure.fetch_snapshot(client=client, host=host, target_day=day),
            meteo.fetch_snapshot(day, client=client, host=host),
        )

    winds, lake_temp = _project_buoy_day_curve([])
    # For replay, anchor `now` to noon on the target day so the
    # air_lake_delta staleness check measures against the replay day, not
    # the wall clock. A 4-year-old historical buoy reading is "current"
    # in the context of a 4-year-old replay.
    now = datetime.combine(day, time(12, 0))
    verdicts = apply_rules(snapshot, meteo_snap, winds, lake_temp, now=now)
    return Forecast(
        overall=aggregate(verdicts),
        verdicts=verdicts,
        pressure=snapshot,
        meteo=meteo_snap,
        winds=winds,
        lake_temp=lake_temp,
        replay_day=day,
        replay_source=source,
    )


def _project_buoy_day_curve(samples: list[UrfeldSample]) -> tuple[list[WindReading], LakeTempSnapshot | None]:
    """Project the buoy's day-curve into the engine-visible shapes.

    The last sample of the day is treated as "the latest known reading at
    the end of the day" — same proxy for liveness that the production
    code uses, just anchored to the target day. Returns ([WindReading],
    LakeTempSnapshot|None); the snapshot is None when the curve had no
    usable samples or no `wtemp` on the last one (same tolerance as the
    live path).
    """
    if not samples:
        return [], None
    latest = samples[-1]
    reading = WindReading(
        station="Urfeld",
        role=StationRole.SHORE,
        avg_knots=latest.avg_knots,
        gust_knots=latest.gust_knots,
        direction_deg=None,
        water_temp_c=latest.water_temp_c,
        air_temp_c=latest.air_temp_c,
        dew_point_c=latest.dew_point_c,
        rel_humidity_pct=latest.rel_humidity_pct,
        pressure_hpa=latest.pressure_hpa,
        rain_mm=latest.rain_mm,
        measured_at=latest.measured_at,
    )
    lake_temp: LakeTempSnapshot | None = None
    if latest.water_temp_c is not None:
        lake_temp = LakeTempSnapshot(
            surface_temp_c=latest.water_temp_c,
            measured_at=latest.measured_at,
            source_station="Urfeld",
        )
    return [reading], lake_temp


def aggregate(verdicts: list[Verdict]) -> Signal:
    """Consensus aggregation, severity-aware.

    Only HARD vetos (Föhn, synoptic > 25 kt, opposing/decoupling
    upper-level flow, thunderstorm-risk LI) flip the overall to
    NO_GO. A *single* SOFT veto on its own does not downgrade.

    The 2-soft-veto bar (the rule-of-thumb in the project's
    pre-replay history) was wrong on the n=3,331 replay
    baseline. The replay data shows the rules' soft vetos are
    too noisy individually: the per-rule FP-veto rates are
    30-70% in many cases, so a "2 of N soft vetos say NO_GO"
    threshold catches mostly noise. The data-fitted optimum
    is 5-soft-veto, which catches only the cases where the
    noise has converged to real consensus. Hard-error rate
    (NO_GO predicted for an actually-fired day) is unchanged
    at 2.9% across all bars; the bar only affects the
    SOFT-veto downgrade path. Sensitivity table (peak
    label, n=3,263, post-threshold-tune):
      bar=1:  41.3%  (2957 MAYBE days)
      bar=2:  45.4%  (1952 MAYBE)  ← was the project default
      bar=3:  47.2%  (1248)
      bar=4:  47.8%  (656)
      bar=5:  48.3%  (167)        ← new default
      bar=7+: 47.2%  (no aggregator effect — baseline)

    Per the data, raising the bar from 2 to 5 gives +2.9pp
    on the headline with no change in hard-error rate. Past
    bar=5, accuracy plateaus / declines as the aggregator
    stops triggering for most days. The 'no aggregator
    effect' baseline (bar=7+) is 47.2% — the aggregator's
    job is to find the sweet spot, which is 5.

    MAYBE emissions from individual rules remain advisory
    only and don't trigger the downgrade.
    """
    if any(
        v.signal is Signal.NO_GO and v.severity is Severity.HARD for v in verdicts
    ):
        return Signal.NO_GO
    soft_no_gos = sum(
        1 for v in verdicts
        if v.signal is Signal.NO_GO and v.severity is Severity.SOFT
    )
    if soft_no_gos >= SOFT_VETO_BAR:
        return Signal.MAYBE
    return Signal.GO
