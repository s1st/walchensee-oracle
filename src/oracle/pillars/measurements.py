"""Pillar 4 — live wind measurements.

- **Bright Sky** (DWD OpenData wrapper): nearest synoptic station, ~13 km
  south of the lake. Synoptic, not per-shore.

`UrfeldSample` and `LakeTempSnapshot` remain as types for reading historical
backfill records stored in the calibration log; no live buoy fetch is performed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from oracle.config import (
    BRIGHT_SKY_CURRENT_URL,
    URFELD,
    StationRole,
)
from oracle.pillars import client_scope

_KMH_TO_KNOTS = 0.5399568


@dataclass
class WindReading:
    station: str
    role: StationRole
    avg_knots: float
    gust_knots: float
    direction_deg: float | None
    measured_at: datetime
    # Populated only for the Addicted-Sports Urfeld reading. Bright Sky's DWD
    # station does not report any of the buoy-side fields below, so they
    # stay None there. The buoy payload exposes a richer sensor set than
    # the oracle currently uses for rules — the extra fields are captured
    # anyway, as raw inputs preserved for replay (see
    # docs/future-buoy-signals.md). All optional, all tolerantly skipped
    # if the row is metadata-only or the server omits the field.
    water_temp_c: float | None = None
    air_temp_c: float | None = None
    dew_point_c: float | None = None
    rel_humidity_pct: float | None = None
    # Local station pressure as posted. NOT MSL-reduced — the buoy sits at
    # ~830 m, so this is ~100 hPa below the cross-station pressure pillar's
    # Open-Meteo anchors. Stored as-is for replay; do not compare across
    # stations without altitude correction.
    pressure_hpa: float | None = None
    # Last-interval rain amount (mm) as posted by the on-site gauge. The
    # cadence is whatever the server uses between samples (~10 min). Kept
    # for replay; the current `post_rain_moisture` rule uses Open-Meteo
    # grid precipitation.
    rain_mm: float | None = None

    def to_dict(self) -> dict:
        return {
            "station": self.station,
            "role": self.role.value,
            "avg_knots": round(self.avg_knots, 2),
            "gust_knots": round(self.gust_knots, 2),
            "direction_deg": self.direction_deg,
            "water_temp_c": _round_or_none(self.water_temp_c),
            "air_temp_c": _round_or_none(self.air_temp_c),
            "dew_point_c": _round_or_none(self.dew_point_c),
            "rel_humidity_pct": _round_or_none(self.rel_humidity_pct),
            "pressure_hpa": _round_or_none(self.pressure_hpa),
            "rain_mm": _round_or_none(self.rain_mm),
            "measured_at": self.measured_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, w: dict) -> "WindReading":
        return cls(
            station=w["station"],
            role=StationRole(w["role"]),
            avg_knots=float(w["avg_knots"]),
            gust_knots=float(w["gust_knots"]),
            direction_deg=w.get("direction_deg"),
            water_temp_c=_float_or_none(w.get("water_temp_c")),
            air_temp_c=_float_or_none(w.get("air_temp_c")),
            dew_point_c=_float_or_none(w.get("dew_point_c")),
            rel_humidity_pct=_float_or_none(w.get("rel_humidity_pct")),
            pressure_hpa=_float_or_none(w.get("pressure_hpa")),
            rain_mm=_float_or_none(w.get("rain_mm")),
            measured_at=datetime.fromisoformat(w["measured_at"]),
        )


@dataclass
class LakeTempSnapshot:
    """Current lake surface temperature as last reported by the buoy.

    `surface_temp_c` is `None` if the buoy reading is missing or didn't
    carry a `wtemp` field for the latest usable row. Lake temperature
    changes ~1 °C/day, so this reading is also a sound proxy for the next
    couple of days — the engine's `air_lake_delta` rule uses the most
    recent value as the forecast lake temperature.
    """
    surface_temp_c: float | None
    measured_at: datetime | None
    source_station: str

    def to_dict(self) -> dict:
        return {
            "surface_temp_c": (
                round(self.surface_temp_c, 2)
                if self.surface_temp_c is not None
                else None
            ),
            "measured_at": self.measured_at.isoformat() if self.measured_at else None,
            "source_station": self.source_station,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LakeTempSnapshot":
        c = d.get("surface_temp_c")
        m = d.get("measured_at")
        return cls(
            surface_temp_c=float(c) if c is not None else None,
            measured_at=datetime.fromisoformat(m) if m else None,
            source_station=d.get("source_station", "Urfeld"),
        )


@dataclass
class LatestMeasurements:
    """Envelope returned by `fetch_latest` — wind readings + a single
    lake-temperature projection, both pulled from the same buoy call.

    `lake_temp` is `None` when the buoy scrape failed *or* the latest
    usable row didn't carry a `wtemp` field. Callers should treat both
    cases as "no lake-temp signal for this run" (same tolerance as a
    missing wind reading).
    """
    winds: list[WindReading]
    lake_temp: LakeTempSnapshot | None


async def fetch_latest(
    client: httpx.AsyncClient | None = None,
) -> LatestMeasurements:
    """Call Bright Sky. Lake temperature is unavailable without the buoy."""
    async with client_scope(client) as client:
        reading = await _fetch_bright_sky(client)
    return LatestMeasurements(winds=[reading], lake_temp=None)


async def _fetch_bright_sky(client: httpx.AsyncClient) -> WindReading:
    response = await client.get(
        BRIGHT_SKY_CURRENT_URL,
        params={"lat": URFELD.lat, "lon": URFELD.lon},
    )
    response.raise_for_status()
    payload = response.json()

    weather = payload["weather"]
    sources_by_id = {src["id"]: src for src in payload.get("sources", [])}
    wind_source_id = weather.get("fallback_source_ids", {}).get(
        "wind_speed_10", weather["source_id"]
    )
    station_name = sources_by_id.get(wind_source_id, {}).get("station_name", "DWD")

    avg_kmh = _required(weather, "wind_speed_10")
    gust_kmh = _required(weather, "wind_gust_speed_10")
    direction = _required(weather, "wind_direction_10")

    return WindReading(
        station=station_name,
        role=StationRole.IGNITION_REFERENCE,
        avg_knots=avg_kmh * _KMH_TO_KNOTS,
        gust_knots=gust_kmh * _KMH_TO_KNOTS,
        direction_deg=float(direction),
        measured_at=datetime.fromisoformat(weather["timestamp"]),
    )


@dataclass
class UrfeldSample:
    """One row from the historical Urfeld buoy calibration log.

    Used only for reading data already stored in the calibration log
    (ground_truth.machine). No live fetch is performed.
    """
    measured_at: datetime
    avg_knots: float
    gust_knots: float
    water_temp_c: float | None = None
    air_temp_c: float | None = None
    dew_point_c: float | None = None
    rel_humidity_pct: float | None = None
    pressure_hpa: float | None = None
    rain_mm: float | None = None


def _required(weather: dict, key: str) -> float:
    value = weather.get(key)
    if value is None:
        raise RuntimeError(f"Bright Sky response missing required field: {key}")
    return float(value)


def _round_or_none(value: float | None) -> float | None:
    """Round to 2 dp for the JSON log; preserve None so missing fields stay
    distinguishable from a literal 0.0 in the stored record."""
    return round(value, 2) if value is not None else None


def _float_or_none(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
