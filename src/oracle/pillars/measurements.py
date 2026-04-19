"""Pillar 4 — live wind measurements from two sources.

- **Bright Sky** (DWD OpenData wrapper): nearest synoptic station, ~13 km
  south of the lake. Synoptic, not per-shore.
- **Addicted-Sports Urfeld**: a private anemometer on the Panoramahotel
  Karwendelblick buoy — the actual Urfeld shore reading. Scraped via
  CSRF-guarded JSON endpoint; direction is not exposed.

Both sources are called in parallel; one failing does not take out the
other. `fetch_latest` raises only if *all* sources fail.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from oracle.config import (
    ADDICTED_SPORTS_BASE_URL,
    BRIGHT_SKY_CURRENT_URL,
    URFELD,
    StationRole,
)

_KMH_TO_KNOTS = 0.5399568
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_CSRF_META_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')


@dataclass
class WindReading:
    station: str
    role: StationRole
    avg_knots: float
    gust_knots: float
    direction_deg: float | None
    measured_at: datetime


async def fetch_latest(client: httpx.AsyncClient | None = None) -> list[WindReading]:
    """Call Bright Sky and Addicted-Sports in parallel. One failure is tolerated."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        results = await asyncio.gather(
            _fetch_bright_sky(client),
            _fetch_urfeld(client),
            return_exceptions=True,
        )
    finally:
        if owns_client:
            await client.aclose()

    readings = [r for r in results if isinstance(r, WindReading)]
    errors = [r for r in results if isinstance(r, BaseException)]
    if not readings:
        raise RuntimeError(f"All station sources failed: {errors}")
    for err in errors:
        # Visible log line — still want to know a source is degraded.
        print(f"[measurements] source failed: {type(err).__name__}: {err}")
    return readings


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


async def _fetch_urfeld(client: httpx.AsyncClient) -> WindReading:
    page = await client.get(
        f"{ADDICTED_SPORTS_BASE_URL}/webcam/walchensee/urfeld/",
        headers={"User-Agent": _UA},
    )
    page.raise_for_status()
    match = _CSRF_META_RE.search(page.text)
    if not match:
        raise RuntimeError("csrf-token meta tag not found on Urfeld page")
    token = match.group(1)
    # Session cookies set by the HTML response are already on `client` and
    # will be carried automatically on the next request.

    # Window must include "now" — the endpoint returns entries inside
    # [startimg, stopimg]. Span today → tomorrow so any current reading is
    # always captured regardless of the hour of day.
    today = date.today()
    tomorrow = today + timedelta(days=1)
    data_url = f"{ADDICTED_SPORTS_BASE_URL}/fileadmin/webcam/src/getWeatherData.php"
    response = await client.get(
        data_url,
        params={
            "startimg": f"{today:%Y/%m/%d}/0000",
            "stopimg": f"{tomorrow:%Y/%m/%d}/0000",
            "graph": "true",
            "wc": "walchensee",
            "lang": "DE",
        },
        headers={
            "User-Agent": _UA,
            "CsrfToken": token,  # case-sensitive; lowercase variants are rejected
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": f"{ADDICTED_SPORTS_BASE_URL}/webcam/walchensee/urfeld/",
        },
    )
    response.raise_for_status()
    body = response.json()

    if body.get("status") and body["status"] != "OK":
        raise RuntimeError(f"Addicted-Sports rejected request: {body['status']}")
    entries: dict = body.get("measurment") or {}
    if not entries:
        raise RuntimeError("Addicted-Sports returned empty measurement set")

    latest_key = max(entries, key=lambda k: int(entries[k]["utctstamp"]))
    latest = entries[latest_key]

    return WindReading(
        station="Urfeld",
        role=StationRole.SHORE,
        avg_knots=float(latest["wsavg"]),
        gust_knots=float(latest["wsmax"]),
        direction_deg=None,  # not exposed by this endpoint
        measured_at=datetime.fromisoformat(latest["tsdatetime"].replace(" ", "T")),
    )


def _required(weather: dict, key: str) -> float:
    value = weather.get(key)
    if value is None:
        raise RuntimeError(f"Bright Sky response missing required field: {key}")
    return float(value)
