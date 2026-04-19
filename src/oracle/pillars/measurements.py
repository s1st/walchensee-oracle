"""Pillar 4 — live wind measurements from local stations.

Cross-references readings from Krün (ignition reference), Herzogstand (ridge),
and the three shore stations (Urfeld, Galerie, Sachenbach) to detect the exact
moment the thermal ignites and how far the N→S fan has propagated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from oracle.config import StationRole


@dataclass
class WindReading:
    station: str
    role: StationRole
    avg_knots: float
    gust_knots: float
    direction_deg: float
    measured_at: datetime


async def fetch_latest() -> list[WindReading]:
    """Return current readings from all configured stations.

    Backend: Holfuy public API primary, Windy Stations API fallback.
    """
    raise NotImplementedError("Holfuy/Windy station fetcher not yet implemented")
