"""Forecast engine — pulls all pillars, runs rules, aggregates into a verdict."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

import httpx

from oracle.knowledge import rules
from oracle.knowledge.rules import Severity, Signal, Verdict
from oracle.pillars import measurements, meteo, pressure
from oracle.pillars.measurements import LakeTempSnapshot, WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureSnapshot


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


def apply_rules(
    snapshot: PressureSnapshot,
    meteo_snap: MeteoSnapshot,
    winds: list[WindReading],
    lake_temp: LakeTempSnapshot | None,
) -> list[Verdict]:
    """Pure function: pillar snapshots in, thirteen verdicts out.

    Extracted so calibration tooling can re-run the rule layer against a
    record's stored `inputs` block without re-fetching the upstream APIs.
    `lake_temp` may be None when the buoy is down or its latest usable row
    lacked `wtemp`; the air_lake_delta rule handles that as MAYBE.
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
        rules.upper_level_wind(meteo_snap),
        rules.synoptic_override(meteo_snap),
        rules.thermal_ignition(winds),
        rules.air_lake_delta(lake_temp, meteo_snap),
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


def aggregate(verdicts: list[Verdict]) -> Signal:
    """Consensus aggregation, severity-aware.

    Only HARD vetos (Föhn, ≥15 kt synoptic, opposing/decoupling upper-level
    flow, thunderstorm-risk LI) flip the overall to NO_GO. A *single* SOFT
    veto on its own does not downgrade — one soft rule firing shouldn't
    override the consensus of nine others. Two or more SOFT
    vetos do downgrade to MAYBE: that's where the negative signals start
    converging into something real. MAYBE emissions from individual rules are
    advisory only and don't trigger a downgrade by themselves.
    """
    if any(
        v.signal is Signal.NO_GO and v.severity is Severity.HARD for v in verdicts
    ):
        return Signal.NO_GO
    soft_no_gos = sum(
        1 for v in verdicts
        if v.signal is Signal.NO_GO and v.severity is Severity.SOFT
    )
    if soft_no_gos >= 2:
        return Signal.MAYBE
    return Signal.GO
