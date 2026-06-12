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
JSON exposes a richer sensor set than the rules layer currently uses: each
row carries `temp` (air), `wtemp` (water), `dp` (dew point), `rh` (humidity),
`rp` (local pressure, not MSL) and `rain` (last-interval rain). We surface
all of them as optional fields on `WindReading` and `UrfeldSample` and
round-trip them through the calibration log, so future rules can be fit
against buoy-side data without re-fetching — see
`docs/future-buoy-signals.md` for what's queued. `LakeTempSnapshot` is the
one field the engine uses today (the `air_lake_delta` rule).
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
    """One row from the Addicted-Sports graph endpoint.

    All buoy-side fields beyond the wind pair are optional and tolerated
    as missing (metadata-only row, server temporarily omitting the field,
    or sensor offline). See `WindReading` for the per-field docstrings.
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
        water_temp_c=_buoy_field(latest, "wtemp"),
        air_temp_c=_buoy_field(latest, "temp"),
        dew_point_c=_buoy_field(latest, "dp"),
        rel_humidity_pct=_buoy_field(latest, "rh"),
        pressure_hpa=_buoy_field(latest, "rp"),
        rain_mm=_buoy_field(latest, "rain"),
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
        samples.append(
            UrfeldSample(
                measured_at=measured_at,
                avg_knots=float(entry["wsavg"]),
                gust_knots=float(entry["wsmax"]),
                water_temp_c=_buoy_field(entry, "wtemp"),
                air_temp_c=_buoy_field(entry, "temp"),
                dew_point_c=_buoy_field(entry, "dp"),
                rel_humidity_pct=_buoy_field(entry, "rh"),
                pressure_hpa=_buoy_field(entry, "rp"),
                rain_mm=_buoy_field(entry, "rain"),
            )
        )
    samples.sort(key=lambda s: s.measured_at)
    return samples


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
    """Tolerate the buoy payload quirks: missing key, JSON null, or empty
    string. The scraper stringifies most buoy fields, so we coerce through
    `float()` only when there's something numeric to coerce."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _buoy_field(entry: dict, *keys: str) -> float | None:
    """First non-null value across a set of buoy field-name candidates.

    The same physical quantity has been exposed under different field names
    on the Addicted-Sports endpoint over the years. We accept any of them
    rather than fail on a rename — the union keeps historical rows
    readable if a backfill ever needs to be re-run.
    """
    for key in keys:
        if key in entry:
            parsed = _float_or_none(entry[key])
            if parsed is not None:
                return parsed
    return None
