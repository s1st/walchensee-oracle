"""Pillar 3 — meteorological conditions.

Overnight cooling (clear skies) + forecasted solar radiation the following morning
together decide whether the thermal engine can spin up at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class MeteoSnapshot:
    day: date
    overnight_cloud_cover_pct: float   # lower = better radiative cooling
    morning_solar_radiation_wm2: float # higher = stronger daytime heating
    synoptic_wind_knots: float         # large-scale flow that can override the thermal


async def fetch_snapshot(day: date) -> MeteoSnapshot:
    raise NotImplementedError("meteo fetcher not yet implemented")
