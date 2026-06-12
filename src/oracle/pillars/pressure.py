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
    series = await fetch_hourly_range(target_day, target_day, client=client, host=host)
    return snapshot_at_morning(series, target_day)


# Replay sampling hour: 08:00 local (Europe/Berlin), the `oracle-forecast`
# Cloud Run schedule. The thermik/Föhn deltas evolve over the morning, and
# MIN_THERMIK_DELTA_HPA was fitted against 08:00 samples — a different
# replay hour would make the deltas incomparable.
REPLAY_PRESSURE_LOCAL_TIME = time(8, 0)


@dataclass(frozen=True)
class PressureHourlyRange:
    """Hourly `pressure_msl` timeseries for the three stations over a date
    range, on a shared local-time (Europe/Berlin) axis. Produced by
    `fetch_hourly_range`; consumed per-day by `snapshot_at_morning`."""
    times: list[datetime]
    values_by_station: dict[str, list[float | None]]


async def fetch_hourly_range(
    start: date,
    end: date,
    client: httpx.AsyncClient | None = None,
    *,
    host: str | None = None,
    models: str | None = None,
) -> PressureHourlyRange:
    """Pull the three stations' hourly pressure for a whole date range in
    one batched request. Used by replay (single day and batch); `models`
    pins a specific Open-Meteo model for cross-era scoring runs."""
    params: dict = {
        "latitude": ",".join(f"{s.lat}" for s in _STATIONS),
        "longitude": ",".join(f"{s.lon}" for s in _STATIONS),
        "hourly": "pressure_msl",
        "timezone": "Europe/Berlin",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    if models is not None:
        params["models"] = models
    async with client_scope(client) as client:
        response = await client.get(host or OPEN_METEO_URL, params=params)
        response.raise_for_status()
        payload = response.json()

    locations = payload if isinstance(payload, list) else [payload]
    times: list[datetime] | None = None
    values_by_station: dict[str, list[float | None]] = {}
    for station, loc in zip(_STATIONS, locations, strict=True):
        hourly = loc["hourly"]
        station_times = [datetime.fromisoformat(t) for t in hourly["time"]]
        if times is None:
            times = station_times
        elif station_times != times:
            raise RuntimeError(
                f"Replay: hourly time axis for {station.name} differs from the "
                "other stations — refusing to mix misaligned series"
            )
        values_by_station[station.name] = hourly["pressure_msl"]
    assert times is not None
    return PressureHourlyRange(times=times, values_by_station=values_by_station)


def snapshot_at_morning(series: PressureHourlyRange, target_day: date) -> PressureSnapshot:
    """Pick the 08:00-local reading of `target_day` for each station out of
    a `fetch_hourly_range` series — the hour the live job samples."""
    target_hour = datetime.combine(target_day, REPLAY_PRESSURE_LOCAL_TIME)
    try:
        idx = series.times.index(target_hour)
    except ValueError:
        raise RuntimeError(
            f"Replay: pressure hour {target_hour.isoformat()} not in hourly timeseries "
            f"(times span {series.times[0]} → {series.times[-1]}). "
            "This day is probably outside the archive coverage."
        )
    readings = []
    for station in _STATIONS:
        value = series.values_by_station[station.name][idx]
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
