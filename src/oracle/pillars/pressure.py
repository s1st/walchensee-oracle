"""Pillar 2 — cross-Alps pressure pairs.

Two pairs matter at Walchensee:

1. **Thermik** (Munich − Innsbruck) — the north-minus-south pumping that
   drives the thermal engine. Positive delta = favourable. Meteorologists
   call this phenomenon "Alpenpumpe"; the windsurfing community just calls
   it Thermik, so the code uses that name.
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
from oracle.pillars import client_scope


@dataclass
class PressureReading:
    station: str
    hpa: float
    measured_at: datetime


@dataclass
class PressureSnapshot:
    thermik_north: PressureReading  # Munich
    thermik_south: PressureReading  # Innsbruck (also serves as Föhn north)
    foehn_south: PressureReading    # Bolzano

    @property
    def thermik_delta_hpa(self) -> float:
        return self.thermik_north.hpa - self.thermik_south.hpa

    @property
    def foehn_delta_hpa(self) -> float:
        return self.foehn_south.hpa - self.thermik_south.hpa

    def to_dict(self) -> dict:
        return {
            "munich_hpa": self.thermik_north.hpa,
            "innsbruck_hpa": self.thermik_south.hpa,
            "bolzano_hpa": self.foehn_south.hpa,
            "thermik_delta_hpa": round(self.thermik_delta_hpa, 2),
            "foehn_delta_hpa": round(self.foehn_delta_hpa, 2),
            "measured_at": self.thermik_north.measured_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, p: dict) -> "PressureSnapshot":
        measured = datetime.fromisoformat(p["measured_at"])
        return cls(
            thermik_north=PressureReading("Munich", float(p["munich_hpa"]), measured),
            thermik_south=PressureReading("Innsbruck", float(p["innsbruck_hpa"]), measured),
            foehn_south=PressureReading("Bolzano", float(p["bolzano_hpa"]), measured),
        )


_STATIONS: tuple[Station, ...] = (MUNICH, INNSBRUCK_N, BOLZANO)


async def fetch_snapshot(client: httpx.AsyncClient | None = None) -> PressureSnapshot:
    async with client_scope(client) as client:
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

    # Open-Meteo returns a list when multiple locations are requested.
    locations = payload if isinstance(payload, list) else [payload]
    readings = [_to_reading(station, loc) for station, loc in zip(_STATIONS, locations, strict=True)]
    munich, innsbruck, bolzano = readings
    return PressureSnapshot(
        thermik_north=munich,
        thermik_south=innsbruck,
        foehn_south=bolzano,
    )


def _to_reading(station: Station, location_payload: dict) -> PressureReading:
    current = location_payload["current"]
    return PressureReading(
        station=station.name,
        hpa=float(current["pressure_msl"]),
        measured_at=datetime.fromisoformat(current["time"]),
    )
