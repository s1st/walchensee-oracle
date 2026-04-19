"""Forecast engine — pulls all pillars, runs rules, aggregates into a verdict."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

from oracle.knowledge import rules
from oracle.knowledge.rules import Signal, Verdict
from oracle.pillars import measurements, meteo, pressure


@dataclass
class Forecast:
    overall: Signal
    verdicts: list[Verdict]


async def run_forecast(day: date) -> Forecast:
    snapshot, meteo_snap, winds = await asyncio.gather(
        pressure.fetch_snapshot(),
        meteo.fetch_snapshot(day),
        measurements.fetch_latest(),
    )
    verdicts = [
        rules.alpenpumpe_threshold(snapshot),
        rules.foehn_override(snapshot),
        rules.synoptic_override(meteo_snap),
        rules.thermal_ignition(winds),
    ]
    return Forecast(overall=_aggregate(verdicts), verdicts=verdicts)


def _aggregate(verdicts: list[Verdict]) -> Signal:
    if any(v.signal is Signal.NO_GO for v in verdicts):
        return Signal.NO_GO
    if all(v.signal is Signal.GO for v in verdicts):
        return Signal.GO
    return Signal.MAYBE
