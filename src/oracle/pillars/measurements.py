"""Pillar 4 — live wind measurements from two sources.

- **Bright Sky** (DWD OpenData wrapper): nearest synoptic station, ~13 km
  south of the lake. Synoptic, not per-shore.
- **Addicted-Sports Urfeld**: a private anemometer mounted on a buoy
  anchored over the deepest part of Walchensee (roughly mid-lake), ~1.6 m
  above the water. The webcam and temperature/pressure/humidity sensors
  are at the Panoramahotel Karwendelblick on the shore — that's where the
  scraped page is hosted, but the wind reading itself comes from the
  buoy. Scraped via CSRF-guarded JSON endpoint; direction is not exposed.

Both sources are called in parallel; one failing does not take out the
other. `fetch_latest` raises only if *all* sources fail. The Addicted-Sports
JSON also carries `wtemp` (lake surface temperature) on each row; we surface
that as an optional `water_temp_c` on the per-row types and a small
`LakeTempSnapshot` for the engine.
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
from oracle.pillars import client_scope

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
    # Populated only for the Addicted-Sports Urfeld reading. Bright Sky's DWD
    # station does not report lake temperature, so this stays None there.
    water_temp_c: float | None = None

    def to_dict(self) -> dict:
        return {
            "station": self.station,
            "role": self.role.value,
            "avg_knots": round(self.avg_knots, 2),
            "gust_knots": round(self.gust_knots, 2),
            "direction_deg": self.direction_deg,
            "water_temp_c": (
                round(self.water_temp_c, 2) if self.water_temp_c is not None else None
            ),
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
            water_temp_c=(
                float(w["water_temp_c"]) if w.get("water_temp_c") is not None else None
            ),
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
    """Call Bright Sky and Addicted-Sports in parallel. One failure is tolerated."""
    async with client_scope(client) as client:
        results = await asyncio.gather(
            _fetch_bright_sky(client),
            _fetch_urfeld(client),
            return_exceptions=True,
        )

    readings = [r for r in results if isinstance(r, WindReading)]
    errors = [r for r in results if isinstance(r, BaseException)]
    if not readings:
        raise RuntimeError(f"All station sources failed: {errors}")
    for err in errors:
        # Visible log line — still want to know a source is degraded.
        print(f"[measurements] source failed: {type(err).__name__}: {err}")

    # Project the lake-temperature signal out of the Urfeld row. If the
    # buoy failed or the latest usable row had no `wtemp`, lake_temp is
    # None and the engine's air_lake_delta rule won't fire.
    urfeld = next((r for r in readings if r.station == "Urfeld"), None)
    lake_temp: LakeTempSnapshot | None = None
    if urfeld is not None and urfeld.water_temp_c is not None:
        lake_temp = LakeTempSnapshot(
            surface_temp_c=urfeld.water_temp_c,
            measured_at=urfeld.measured_at,
            source_station=urfeld.station,
        )
    return LatestMeasurements(winds=readings, lake_temp=lake_temp)


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
    # `None` if the row had no `wtemp` field (metadata-only row, or the
    # server stopped reporting lake temperature). Tolerated — same as the
    # wsavg/wsmax skip, no single row is allowed to take out the backfill.
    water_temp_c: float | None = None


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
    water_temp = float(latest["wtemp"]) if "wtemp" in latest else None
    return WindReading(
        station="Urfeld",
        role=StationRole.SHORE,
        avg_knots=float(latest["wsavg"]),
        gust_knots=float(latest["wsmax"]),
        direction_deg=None,
        water_temp_c=water_temp,
        measured_at=datetime.fromisoformat(latest["tsdatetime"].replace(" ", "T")),
    )


async def fetch_urfeld_day_curve(
    day: date,
    client: httpx.AsyncClient | None = None,
) -> list[UrfeldSample]:
    """Retrospectively pull all Urfeld samples that fell inside `day` (local time).

    Used by the calibration logger to capture ground-truth wind after the fact.
    """
    async with client_scope(client, timeout=15.0) as client:
        entries = await _fetch_urfeld_entries(client, day, day + timedelta(days=1))

    samples: list[UrfeldSample] = []
    for entry in entries:
        measured_at = datetime.fromisoformat(entry["tsdatetime"].replace(" ", "T"))
        if measured_at.date() != day:
            continue
        # Metadata-only rows (missing wsavg/wsmax) show up occasionally; skip
        # them instead of aborting the whole fetch.
        if "wsavg" not in entry or "wsmax" not in entry:
            continue
        water_temp = float(entry["wtemp"]) if "wtemp" in entry else None
        samples.append(
            UrfeldSample(
                measured_at=measured_at,
                avg_knots=float(entry["wsavg"]),
                gust_knots=float(entry["wsmax"]),
                water_temp_c=water_temp,
            )
        )
    samples.sort(key=lambda s: s.measured_at)
    return samples


def _required(weather: dict, key: str) -> float:
    value = weather.get(key)
    if value is None:
        raise RuntimeError(f"Bright Sky response missing required field: {key}")
    return float(value)
