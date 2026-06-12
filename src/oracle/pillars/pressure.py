"""Pillar 2 — cross-Alps pressure pairs.

Two pairs matter at Walchensee:

1. **Thermik** (Munich − Innsbruck) — the north-minus-south pumping that
   drives the thermal engine. Positive delta = favourable. Meteorologists
   call this phenomenon "Alpenpumpe"; the windsurfing community just calls
   it Thermik, so the code uses that name.
2. **Föhn** (Bolzano − Innsbruck) — south-minus-north; a positive delta signals
   Föhn risk, which suppresses the local thermal.

Backend: Open-Meteo `forecast` endpoint (live) or `historical-forecast-api`
(archive replay). All three stations fetched in one batched request using
MSL-reduced pressure (so elevation differences between Munich, Innsbruck
and Bolzano don't swamp the signal).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

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


async def fetch_snapshot(
    client: httpx.AsyncClient | None = None,
    *,
    host: str | None = None,
    target_day: date | None = None,
) -> PressureSnapshot:
    """Pull the three pressure anchors as a `PressureSnapshot`.

    Live mode (default): hits the live forecast host with `current=pressure_msl`.
    Replay mode (`target_day` set): uses hourly timeseries for the target day
    and picks the 08:00 Europe/Berlin reading — the hour the production
    `oracle-forecast` job samples `current` pressure (08:00 CET schedule),
    so replayed deltas stay comparable to the data-fitted thresholds.
    """
    if target_day is None:
        return await _fetch_live(client, host or OPEN_METEO_URL)
    return await _fetch_replay(client, host or OPEN_METEO_URL, target_day)


async def _fetch_live(
    client: httpx.AsyncClient | None, host: str
) -> PressureSnapshot:
    async with client_scope(client) as client:
        response = await client.get(
            host,
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


async def _fetch_replay(
    client: httpx.AsyncClient | None, host: str, target_day: date
) -> PressureSnapshot:
    """Replay-mode pressure fetch: hourly timeseries for the target day,
    morning reading. Works against both the historical-forecast and
    archive hosts — query schema is identical to the live one."""
    # Pick the hour the live job samples: 08:00 local (Europe/Berlin), the
    # `oracle-forecast` Cloud Run schedule. The thermik/Föhn deltas evolve
    # over the morning, and MIN_THERMIK_DELTA_HPA was fitted against 08:00
    # samples — a different replay hour would make the deltas incomparable.
    target_hour = datetime.combine(target_day, time(8, 0))

    async with client_scope(client) as client:
        response = await client.get(
            host,
            params={
                "latitude": ",".join(f"{s.lat}" for s in _STATIONS),
                "longitude": ",".join(f"{s.lon}" for s in _STATIONS),
                "hourly": "pressure_msl",
                "timezone": "Europe/Berlin",
                "start_date": target_day.isoformat(),
                "end_date": target_day.isoformat(),
            },
        )
        response.raise_for_status()
        payload = response.json()

    locations = payload if isinstance(payload, list) else [payload]
    readings = []
    for station, loc in zip(_STATIONS, locations, strict=True):
        hourly = loc["hourly"]
        times = [datetime.fromisoformat(t) for t in hourly["time"]]
        values = hourly["pressure_msl"]
        try:
            idx = times.index(target_hour)
        except ValueError:
            raise RuntimeError(
                f"Replay: pressure hour {target_hour.isoformat()} not in hourly timeseries "
                f"for {station.name} (times span {times[0]} → {times[-1]}). "
                "This day is probably outside the archive coverage."
            )
        value = values[idx]
        if value is None:
            raise RuntimeError(
                f"Replay: pressure at {target_hour.isoformat()} is null for {station.name}"
            )
        readings.append(PressureReading(
            station=station.name,
            hpa=float(value),
            measured_at=target_hour,
        ))
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
