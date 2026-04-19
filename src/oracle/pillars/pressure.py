"""Pillar 2 — cross-Alps pressure pairs.

Two pairs matter at Walchensee:

1. **Alpenpumpe** (Munich − Innsbruck) — the north-minus-south pumping that
   drives the thermal engine. Positive delta = favourable.
2. **Föhn** (Bolzano − Innsbruck) — south-minus-north; a positive delta signals
   Föhn risk, which suppresses the local thermal.

Backend: Open-Meteo `forecast` endpoint. All three stations fetched in one
batched request using MSL-reduced pressure (so elevation differences between
Munich, Innsbruck and Bolzano don't swamp the signal).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from oracle.config import BOLZANO, INNSBRUCK_N, MUNICH, OPEN_METEO_URL, Station


@dataclass
class PressureReading:
    station: str
    hpa: float
    measured_at: datetime


@dataclass
class PressureSnapshot:
    alpenpumpe_north: PressureReading  # Munich
    alpenpumpe_south: PressureReading  # Innsbruck (also serves as Föhn north)
    foehn_south: PressureReading       # Bolzano

    @property
    def alpenpumpe_delta_hpa(self) -> float:
        return self.alpenpumpe_north.hpa - self.alpenpumpe_south.hpa

    @property
    def foehn_delta_hpa(self) -> float:
        return self.foehn_south.hpa - self.alpenpumpe_south.hpa


_STATIONS: tuple[Station, ...] = (MUNICH, INNSBRUCK_N, BOLZANO)


async def fetch_snapshot(client: httpx.AsyncClient | None = None) -> PressureSnapshot:
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(
            OPEN_METEO_URL,
            params={
                "latitude": ",".join(f"{s.lat}" for s in _STATIONS),
                "longitude": ",".join(f"{s.lon}" for s in _STATIONS),
                "current": "pressure_msl",
                "timezone": "UTC",
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            await client.aclose()

    # Open-Meteo returns a list when multiple locations are requested.
    locations = payload if isinstance(payload, list) else [payload]
    readings = [_to_reading(station, loc) for station, loc in zip(_STATIONS, locations, strict=True)]
    munich, innsbruck, bolzano = readings
    return PressureSnapshot(
        alpenpumpe_north=munich,
        alpenpumpe_south=innsbruck,
        foehn_south=bolzano,
    )


def _to_reading(station: Station, location_payload: dict) -> PressureReading:
    current = location_payload["current"]
    return PressureReading(
        station=station.name,
        hpa=float(current["pressure_msl"]),
        measured_at=datetime.fromisoformat(current["time"]),
    )
