"""Forecast engine — pulls all pillars, runs rules, aggregates into a verdict."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date

from oracle.knowledge import rules
from oracle.knowledge.rules import Signal, Verdict
from oracle.pillars import chat, measurements, meteo, pressure
from oracle.pillars.chat import ChatMessage
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureSnapshot


@dataclass
class Forecast:
    overall: Signal
    verdicts: list[Verdict]
    chat_messages: list[ChatMessage] = field(default_factory=list)
    # Raw pillar inputs — kept so the calibration logger can replay / re-score
    # past decisions with different thresholds without re-fetching.
    pressure: PressureSnapshot | None = None
    meteo: MeteoSnapshot | None = None
    winds: list[WindReading] = field(default_factory=list)


async def run_forecast(day: date) -> Forecast:
    snapshot, meteo_snap, winds, messages = await asyncio.gather(
        pressure.fetch_snapshot(),
        meteo.fetch_snapshot(day),
        measurements.fetch_latest(),
        _fetch_chat_tolerant(),
    )
    verdicts = [
        rules.thermik(snapshot),
        rules.foehn_override(snapshot),
        rules.overnight_cooling(meteo_snap),
        rules.solar_radiation(meteo_snap),
        rules.dew_point_spread(meteo_snap),
        rules.boundary_layer_height(meteo_snap),
        rules.post_rain_moisture(meteo_snap),
        rules.synoptic_override(meteo_snap),
        rules.thermal_ignition(winds),
    ]
    return Forecast(
        overall=_aggregate(verdicts),
        verdicts=verdicts,
        chat_messages=messages,
        pressure=snapshot,
        meteo=meteo_snap,
        winds=winds,
    )


async def _fetch_chat_tolerant() -> list[ChatMessage]:
    """Chat is qualitative and optional — failures must not take out the forecast."""
    try:
        return await chat.fetch_recent_messages(limit=10)
    except Exception as exc:
        print(f"[chat] source failed: {type(exc).__name__}: {exc}")
        return []


def _aggregate(verdicts: list[Verdict]) -> Signal:
    if any(v.signal is Signal.NO_GO for v in verdicts):
        return Signal.NO_GO
    if all(v.signal is Signal.GO for v in verdicts):
        return Signal.GO
    return Signal.MAYBE
