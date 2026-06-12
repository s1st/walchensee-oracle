"""Pillar 3 — meteorological conditions.

Overnight cooling (clear skies) + forecasted solar radiation the following
morning together decide whether the thermal engine can spin up at all. The
850 hPa wind is our proxy for synoptic flow above the boundary layer — if
that's already strong, it will override any local thermal cell.

Three additional factors (added from `docs/future-factors.md`):

- **Dew-point spread** (T − Td) in the morning window controls how much solar
  energy goes into sensible heating vs. evaporation.
- **Boundary layer height** sets how deep the thermal cell can become.
- **Soil moisture + yesterday's rain** captures the "2nd sunny day after rain"
  rule — wet ground diverts solar energy into evaporation.

Backend: Open-Meteo `forecast` endpoint, hourly variables in local time
(Europe/Berlin) so our window filters use physical hours without timezone math.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx

from oracle.config import OPEN_METEO_URL, RAINED_YESTERDAY_MM, URFELD
from oracle.pillars import client_scope


@dataclass
class MeteoSnapshot:
    day: date
    overnight_cloud_cover_pct: float    # 22:00 prev → 06:00 target, mean
    morning_solar_radiation_wm2: float  # 09:00–13:00 target, hourly max
    synoptic_wind_knots: float | None  # 09:00–13:00 target, hourly max at 850 hPa
    min_dew_point_spread_c: float       # 09:00–13:00 target, hourly min(T − Td)
    max_boundary_layer_height_m: float | None  # 09:00–13:00 target, hourly max
    soil_moisture_m3m3: float | None  # target day 09:00 soil_moisture_0_to_1cm
    rained_yesterday: bool              # target-1 day total precipitation ≥ threshold
    yesterday_precipitation_mm: float   # raw value for the log
    # Medium-priority signals (added from docs/future-factors.md):
    # All Optional — the historical-forecast API (IFS HRES) doesn't model
    # surface soil moisture, BLH, or the pressure-level fields for
    # pre-2021 days. Rules that need these emit MAYBE when None.
    max_lifted_index: float | None      # 09:00–13:00; > +6 = too stable
    min_lifted_index: float | None      # 09:00–13:00; < −2 = storm risk
    max_cape_j_kg: float | None         # 09:00–13:00; captured for future calibration
    max_daytime_low_cloud_pct: float    # 09:00–13:00; low clouds shade slopes
    wind_850_direction_at_peak_deg: float | None  # direction at the morning 850 hPa speed peak
    max_wind_700_knots: float | None    # 09:00–13:00; 700 hPa crossflow aloft
    # Mean of 09:00–13:00 2 m air temperatures — paired with the buoy's water
    # temperature for the air_lake_delta rule. `None` for records written before
    # this field shipped; the rule treats that as "no signal" (MAYBE).
    morning_air_temp_c: float | None = None

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "overnight_cloud_cover_pct": self.overnight_cloud_cover_pct,
            "morning_solar_radiation_wm2": self.morning_solar_radiation_wm2,
            "synoptic_wind_knots": self.synoptic_wind_knots,
            "min_dew_point_spread_c": self.min_dew_point_spread_c,
            "max_boundary_layer_height_m": self.max_boundary_layer_height_m,
            "soil_moisture_m3m3": self.soil_moisture_m3m3,
            "rained_yesterday": self.rained_yesterday,
            "yesterday_precipitation_mm": self.yesterday_precipitation_mm,
            "max_lifted_index": self.max_lifted_index,
            "min_lifted_index": self.min_lifted_index,
            "max_cape_j_kg": self.max_cape_j_kg,
            "max_daytime_low_cloud_pct": self.max_daytime_low_cloud_pct,
            "wind_850_direction_at_peak_deg": self.wind_850_direction_at_peak_deg,
            "max_wind_700_knots": self.max_wind_700_knots,
            "morning_air_temp_c": (
                round(self.morning_air_temp_c, 2)
                if self.morning_air_temp_c is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, m: dict) -> "MeteoSnapshot":
        return cls(
            day=date.fromisoformat(m["day"]),
            overnight_cloud_cover_pct=float(m["overnight_cloud_cover_pct"]),
            morning_solar_radiation_wm2=float(m["morning_solar_radiation_wm2"]),
            synoptic_wind_knots=(
                float(m["synoptic_wind_knots"])
                if m.get("synoptic_wind_knots") is not None else None
            ),
            min_dew_point_spread_c=float(m["min_dew_point_spread_c"]),
            max_boundary_layer_height_m=(
                float(m["max_boundary_layer_height_m"])
                if m.get("max_boundary_layer_height_m") is not None else None
            ),
            soil_moisture_m3m3=(
                float(m["soil_moisture_m3m3"])
                if m.get("soil_moisture_m3m3") is not None else None
            ),
            rained_yesterday=bool(m["rained_yesterday"]),
            yesterday_precipitation_mm=float(m["yesterday_precipitation_mm"]),
            max_lifted_index=(
                float(m["max_lifted_index"]) if m.get("max_lifted_index") is not None else None
            ),
            min_lifted_index=(
                float(m["min_lifted_index"]) if m.get("min_lifted_index") is not None else None
            ),
            max_cape_j_kg=(
                float(m["max_cape_j_kg"]) if m.get("max_cape_j_kg") is not None else None
            ),
            max_daytime_low_cloud_pct=float(m["max_daytime_low_cloud_pct"]),
            wind_850_direction_at_peak_deg=(
                float(m["wind_850_direction_at_peak_deg"])
                if m.get("wind_850_direction_at_peak_deg") is not None else None
            ),
            max_wind_700_knots=(
                float(m["max_wind_700_knots"]) if m.get("max_wind_700_knots") is not None else None
            ),
            morning_air_temp_c=(
                float(m["morning_air_temp_c"])
                if m.get("morning_air_temp_c") is not None
                else None
            ),
        )


_OVERNIGHT = (time(22, 0), time(6, 0))
_MORNING = (time(9, 0), time(13, 0))

_HOURLY_VARS = ",".join([
    "cloud_cover",
    "shortwave_radiation",
    "wind_speed_850hPa",
    "temperature_2m",
    "dew_point_2m",
    "boundary_layer_height",
    "soil_moisture_0_to_1cm",
    "precipitation",
    "cape",
    "lifted_index",
    "cloud_cover_low",
    "wind_speed_700hPa",
    "wind_direction_850hPa",
])


async def fetch_snapshot(
    day: date,
    client: httpx.AsyncClient | None = None,
    *,
    host: str | None = None,
) -> MeteoSnapshot:
    """Pull the meteo pillar for `day`. `host` defaults to the live forecast
    URL; pass `OPEN_METEO_HISTORICAL_FORECAST_URL` or `OPEN_METEO_ARCHIVE_URL`
    to replay against an archive. Query schema is identical across hosts.
    """
    # Span day-1 (overnight window) back through day-3 (for yesterday's rain
    # check + headroom on soil moisture). Single request.
    start = day - timedelta(days=1)
    async with client_scope(client) as client:
        response = await client.get(
            host or OPEN_METEO_URL,
            params={
                "latitude": URFELD.lat,
                "longitude": URFELD.lon,
                "hourly": _HOURLY_VARS,
                "wind_speed_unit": "kn",
                "timezone": "Europe/Berlin",
                "start_date": start.isoformat(),
                "end_date": day.isoformat(),
            },
        )
        response.raise_for_status()
        payload = response.json()

    return _parse(payload, day)


async def fetch_hourly_range(
    start: date,
    end: date,
    client: httpx.AsyncClient | None = None,
    *,
    host: str | None = None,
    models: str | None = None,
) -> dict:
    """Pull the raw hourly payload for a whole date range in one request.

    Spans [start-1, end] — the extra leading day feeds the first target's
    overnight-cloud and yesterday-rain windows. Used by batch replay to
    avoid one request per day; slice per-day snapshots out of the result
    with `snapshot_from_range`. `models` pins a specific Open-Meteo model
    (recommended for cross-era scoring runs; default is Best Match).
    """
    params: dict = {
        "latitude": URFELD.lat,
        "longitude": URFELD.lon,
        "hourly": _HOURLY_VARS,
        "wind_speed_unit": "kn",
        "timezone": "Europe/Berlin",
        "start_date": (start - timedelta(days=1)).isoformat(),
        "end_date": end.isoformat(),
    }
    if models is not None:
        params["models"] = models
    async with client_scope(client) as client:
        response = await client.get(host or OPEN_METEO_URL, params=params)
        response.raise_for_status()
        return response.json()


def parse_times(payload: dict) -> list[datetime]:
    """Parse the shared hourly time axis of a range payload once, so
    `snapshot_from_range` can bisect instead of re-parsing per day."""
    return [datetime.fromisoformat(t) for t in payload["hourly"]["time"]]


def snapshot_from_range(payload: dict, times: list[datetime], target: date) -> MeteoSnapshot:
    """Slice one day's snapshot out of a `fetch_hourly_range` payload.

    Cuts the [target-1 00:00, target 23:00] window out of every hourly
    array and hands the slice to the same `_parse` the single-day fetch
    uses — windowing semantics stay identical by construction.
    """
    lo = bisect_left(times, datetime.combine(target - timedelta(days=1), time(0, 0)))
    hi = bisect_right(times, datetime.combine(target, time(23, 0)))
    if lo == hi:
        raise RuntimeError(
            f"Open-Meteo range payload does not cover {target.isoformat()} "
            f"(times span {times[0]} → {times[-1]})"
        )
    hourly = payload["hourly"]
    sliced = {"hourly": {key: values[lo:hi] for key, values in hourly.items()}}
    return _parse(sliced, target)


def _parse(payload: dict, target: date) -> MeteoSnapshot:
    hourly = payload["hourly"]
    times = [datetime.fromisoformat(t) for t in hourly["time"]]
    yesterday = target - timedelta(days=1)

    overnight_start = datetime.combine(yesterday, _OVERNIGHT[0])
    overnight_end = datetime.combine(target, _OVERNIGHT[1])
    morning_start = datetime.combine(target, _MORNING[0])
    morning_end = datetime.combine(target, _MORNING[1])
    yesterday_start = datetime.combine(yesterday, time(0, 0))
    yesterday_end = datetime.combine(target, time(0, 0))

    overnight_clouds = _in_window(times, hourly["cloud_cover"], overnight_start, overnight_end)
    morning_radiation = _in_window(
        times, hourly["shortwave_radiation"], morning_start, morning_end, inclusive_end=True
    )
    morning_wind = _in_window(
        times, hourly["wind_speed_850hPa"], morning_start, morning_end, inclusive_end=True
    )
    morning_temp = _in_window(
        times, hourly["temperature_2m"], morning_start, morning_end, inclusive_end=True
    )
    morning_dew = _in_window(
        times, hourly["dew_point_2m"], morning_start, morning_end, inclusive_end=True
    )
    morning_blh = _in_window(
        times, hourly["boundary_layer_height"], morning_start, morning_end, inclusive_end=True
    )
    morning_li = _in_window(
        times, hourly["lifted_index"], morning_start, morning_end, inclusive_end=True
    )
    morning_cape = _in_window(
        times, hourly["cape"], morning_start, morning_end, inclusive_end=True
    )
    morning_low_clouds = _in_window(
        times, hourly["cloud_cover_low"], morning_start, morning_end, inclusive_end=True
    )
    morning_wind_700 = _in_window(
        times, hourly["wind_speed_700hPa"], morning_start, morning_end, inclusive_end=True
    )
    yesterday_rain = _in_window(times, hourly["precipitation"], yesterday_start, yesterday_end)

    required_windows = {
        "cloud_cover": overnight_clouds,
        "shortwave_radiation": morning_radiation,
        "temperature_2m": morning_temp,
        "dew_point_2m": morning_dew,
        "cloud_cover_low": morning_low_clouds,
    }
    missing = [name for name, window in required_windows.items() if not window]
    if missing:
        raise RuntimeError(
            f"Open-Meteo did not return expected hourly windows for "
            f"{target.isoformat()}: {', '.join(missing)}"
        )

    # Direction at the morning's peak 850 hPa wind — captures the dominant
    # upper-level flow without the circular-mean headaches of averaging angles.
    # Historical-forecast (IFS HRES) may not expose wind_speed_850hPa for
    # older years; tolerate the missing window and let the rule emit MAYBE.
    wind_850_dir: float | None
    if morning_wind:
        peak_hour_idx = _argmax_in_window(
            times, hourly["wind_speed_850hPa"], morning_start, morning_end
        )
        wind_850_dir = float(hourly["wind_direction_850hPa"][peak_hour_idx])
    else:
        wind_850_dir = None

    # Soil moisture sampled at morning_start (09:00 target day). The
    # historical-forecast API (IFS HRES) does not model surface soil
    # moisture — the value is always None there. We tolerate that and let
    # `post_rain_moisture` emit MAYBE rather than raising. Same for BLH.
    soil_moisture = _value_at(times, hourly["soil_moisture_0_to_1cm"], morning_start)

    # Pair temp/dew by hour index, not by zipping the None-filtered windows —
    # archive responses can null one array but not the other, and a naive zip
    # would then pair temperatures with dew points from different hours.
    spreads = [
        float(t) - float(d)
        for ts, t, d in zip(times, hourly["temperature_2m"], hourly["dew_point_2m"], strict=True)
        if morning_start <= ts <= morning_end and t is not None and d is not None
    ]
    if not spreads:
        raise RuntimeError(
            f"Open-Meteo returned no paired temperature/dew-point hours in the "
            f"morning window for {target.isoformat()}"
        )
    yesterday_mm = sum(yesterday_rain)
    mean_morning_air_temp = sum(morning_temp) / len(morning_temp) if morning_temp else None

    return MeteoSnapshot(
        day=target,
        overnight_cloud_cover_pct=sum(overnight_clouds) / len(overnight_clouds),
        morning_solar_radiation_wm2=max(morning_radiation),
        synoptic_wind_knots=max(morning_wind) if morning_wind else None,
        min_dew_point_spread_c=min(spreads),
        max_boundary_layer_height_m=max(morning_blh) if morning_blh else None,
        soil_moisture_m3m3=soil_moisture,
        rained_yesterday=yesterday_mm >= RAINED_YESTERDAY_MM,
        yesterday_precipitation_mm=round(yesterday_mm, 2),
        max_lifted_index=max(morning_li) if morning_li else None,
        min_lifted_index=min(morning_li) if morning_li else None,
        max_cape_j_kg=max(morning_cape) if morning_cape else None,
        max_daytime_low_cloud_pct=max(morning_low_clouds),
        wind_850_direction_at_peak_deg=wind_850_dir,
        max_wind_700_knots=max(morning_wind_700) if morning_wind_700 else None,
        morning_air_temp_c=mean_morning_air_temp,
    )


def _in_window(
    times: list[datetime],
    values: list,
    start: datetime,
    end: datetime,
    inclusive_end: bool = False,
) -> list[float]:
    def keep(t: datetime) -> bool:
        return start <= t <= end if inclusive_end else start <= t < end

    return [float(v) for t, v in zip(times, values, strict=True) if v is not None and keep(t)]


def _argmax_in_window(
    times: list[datetime], values: list, start: datetime, end: datetime
) -> int:
    """Return the index (into the original arrays) of the max value within the window."""
    best_idx = -1
    best_val = float("-inf")
    for i, (t, v) in enumerate(zip(times, values, strict=True)):
        if v is None or not (start <= t <= end):
            continue
        if v > best_val:
            best_val = v
            best_idx = i
    if best_idx < 0:
        raise RuntimeError("window is empty for argmax lookup")
    return best_idx


def _value_at(times: list[datetime], values: list, target: datetime) -> float | None:
    for t, v in zip(times, values, strict=True):
        if t == target and v is not None:
            return float(v)
    return None
