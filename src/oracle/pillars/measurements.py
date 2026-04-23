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
# Self-identifying UA — better to be transparent than to pretend to be a
# browser. If an operator wants to block the scraper they can; we'd rather
# they open a dialogue than silently fingerprint us.
_UA = "walchi-oracle/0.1 (+https://github.com/s1st/walchensee-oracle; hobby)"
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


@dataclass
class UrfeldSample:
    """One row from the Addicted-Sports graph endpoint."""
    measured_at: datetime
    avg_knots: float
    gust_knots: float


async def _fetch_urfeld_entries(
    client: httpx.AsyncClient,
    window_start: date,
    window_end: date,
) -> list[dict]:
    """Shared HTTP flow: CSRF dance + call to getWeatherData.php.

    Returns the raw `measurment` entries the server delivers inside the
    [window_start 00:00, window_end 00:00] window.
    """
    page = await client.get(
        f"{ADDICTED_SPORTS_BASE_URL}/webcam/walchensee/urfeld/",
        headers={"User-Agent": _UA},
    )
    page.raise_for_status()
    match = _CSRF_META_RE.search(page.text)
    if not match:
        raise RuntimeError("csrf-token meta tag not found on Urfeld page")
    token = match.group(1)

    data_url = f"{ADDICTED_SPORTS_BASE_URL}/fileadmin/webcam/src/getWeatherData.php"
    response = await client.get(
        data_url,
        params={
            "startimg": f"{window_start:%Y/%m/%d}/0000",
            "stopimg": f"{window_end:%Y/%m/%d}/0000",
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
    return list(entries.values())


async def _fetch_urfeld(client: httpx.AsyncClient) -> WindReading:
    today = date.today()
    entries = await _fetch_urfeld_entries(client, today, today + timedelta(days=1))
    # Addicted-Sports occasionally emits metadata-only rows (timestamps but no
    # wsavg/wsmax). Skip them — one bad row shouldn't take out the whole pillar.
    usable = [e for e in entries if "wsavg" in e and "wsmax" in e]
    if not usable:
        raise RuntimeError("Addicted-Sports returned no entries with wind values")
    latest = max(usable, key=lambda e: int(e["utctstamp"]))
    return WindReading(
        station="Urfeld",
        role=StationRole.SHORE,
        avg_knots=float(latest["wsavg"]),
        gust_knots=float(latest["wsmax"]),
        direction_deg=None,
        measured_at=datetime.fromisoformat(latest["tsdatetime"].replace(" ", "T")),
    )


async def fetch_urfeld_day_curve(
    day: date,
    client: httpx.AsyncClient | None = None,
) -> list[UrfeldSample]:
    """Retrospectively pull all Urfeld samples that fell inside `day` (local time).

    Used by the calibration logger to capture ground-truth wind after the fact.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        entries = await _fetch_urfeld_entries(client, day, day + timedelta(days=1))
    finally:
        if owns_client:
            await client.aclose()

    samples: list[UrfeldSample] = []
    for entry in entries:
        measured_at = datetime.fromisoformat(entry["tsdatetime"].replace(" ", "T"))
        if measured_at.date() != day:
            continue
        # Metadata-only rows (missing wsavg/wsmax) show up occasionally; skip
        # them instead of aborting the whole fetch.
        if "wsavg" not in entry or "wsmax" not in entry:
            continue
        samples.append(
            UrfeldSample(
                measured_at=measured_at,
                avg_knots=float(entry["wsavg"]),
                gust_knots=float(entry["wsmax"]),
            )
        )
    samples.sort(key=lambda s: s.measured_at)
    return samples


def _required(weather: dict, key: str) -> float:
    value = weather.get(key)
    if value is None:
        raise RuntimeError(f"Bright Sky response missing required field: {key}")
    return float(value)
