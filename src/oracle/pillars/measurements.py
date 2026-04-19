"""Pillar 4 — live wind measurements.

Originally planned against Holfuy; reconnaissance showed no Holfuy stations
actually cover Walchensee. Switched to **Bright Sky** (free wrapper around
DWD OpenData) — it returns the nearest German synoptic station for a given
lat/lon. The closest station to the lake is Mittenwald-Buckelwie, ~13 km
south. This is synoptic, not per-shore — the follow-up scraper for the
Addicted-Sports Urfeld anemometer will add a real shore reading.

Bright Sky returns wind in km/h; we convert to knots.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from oracle.config import BRIGHT_SKY_CURRENT_URL, URFELD, StationRole

_KMH_TO_KNOTS = 0.5399568


@dataclass
class WindReading:
    station: str
    role: StationRole
    avg_knots: float
    gust_knots: float
    direction_deg: float
    measured_at: datetime


async def fetch_latest(client: httpx.AsyncClient | None = None) -> list[WindReading]:
    """Return the nearest DWD station's reading for Walchensee.

    First pass uses the Walchensee centroid (Urfeld coords). Returns a
    single-element list; Addicted-Sports shore readings will be added later.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(
            BRIGHT_SKY_CURRENT_URL,
            params={"lat": URFELD.lat, "lon": URFELD.lon},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            await client.aclose()

    return [_to_reading(payload)]


def _to_reading(payload: dict) -> WindReading:
    weather = payload["weather"]
    source_by_id = {src["id"]: src for src in payload.get("sources", [])}
    # Wind often comes from a fallback station if the primary doesn't report it.
    wind_source_id = weather.get("fallback_source_ids", {}).get(
        "wind_speed_10", weather["source_id"]
    )
    station_name = source_by_id.get(wind_source_id, {}).get("station_name", "DWD")

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


def _required(weather: dict, key: str) -> float:
    value = weather.get(key)
    if value is None:
        raise RuntimeError(f"Bright Sky response missing required field: {key}")
    return float(value)
