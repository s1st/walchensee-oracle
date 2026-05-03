"""Forecast engine — pulls all pillars, runs rules, aggregates into a verdict."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date

import httpx

from oracle.knowledge import rules
from oracle.knowledge.rules import Severity, Signal, Verdict
from oracle.pillars import measurements, meteo, pressure
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureSnapshot


@dataclass
class Forecast:
    overall: Signal
    verdicts: list[Verdict]
    # Raw pillar inputs — kept so the calibration logger can replay / re-score
    # past decisions with different thresholds without re-fetching.
    pressure: PressureSnapshot | None = None
    meteo: MeteoSnapshot | None = None
    winds: list[WindReading] = field(default_factory=list)


def apply_rules(
    snapshot: PressureSnapshot,
    meteo_snap: MeteoSnapshot,
    winds: list[WindReading],
) -> list[Verdict]:
    """Pure function: pillar snapshots in, twelve verdicts out.

    Extracted so calibration tooling can re-run the rule layer against a
    record's stored `inputs` block without re-fetching the upstream APIs.
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
    ]


async def run_forecast(day: date) -> Forecast:
    async with httpx.AsyncClient(timeout=10.0) as client:
        snapshot, meteo_snap, winds = await asyncio.gather(
            pressure.fetch_snapshot(client=client),
            meteo.fetch_snapshot(day, client=client),
            measurements.fetch_latest(client=client),
        )
    verdicts = apply_rules(snapshot, meteo_snap, winds)
    return Forecast(
        overall=aggregate(verdicts),
        verdicts=verdicts,
        pressure=snapshot,
        meteo=meteo_snap,
        winds=winds,
    )


def aggregate(verdicts: list[Verdict]) -> Signal:
    """Consensus aggregation, severity-aware.

    Only HARD vetos (Föhn, ≥15 kt synoptic, opposing/decoupling upper-level
    flow, thunderstorm-risk LI) flip the overall to NO_GO. A *single* SOFT
    veto on its own does not downgrade — placeholder thresholds firing one
    rule shouldn't override the consensus of nine others. Two or more SOFT
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
