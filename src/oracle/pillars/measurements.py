"""Pillar 4 — live wind measurements from local stations.

Cross-references readings from Urfeld, Galerie and Sachenbach to detect the exact
moment the thermal wind ignites.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class WindReading:
    station: str
    avg_knots: float
    gust_knots: float
    direction_deg: float
    measured_at: datetime


async def fetch_latest() -> list[WindReading]:
    raise NotImplementedError("live wind fetcher not yet implemented")
