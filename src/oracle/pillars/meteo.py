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

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx

from oracle.config import OPEN_METEO_URL, RAINED_YESTERDAY_MM, URFELD


@dataclass
class MeteoSnapshot:
    day: date
    overnight_cloud_cover_pct: float    # 22:00 prev → 06:00 target, mean
    morning_solar_radiation_wm2: float  # 09:00–13:00 target, hourly max
    synoptic_wind_knots: float          # 09:00–13:00 target, hourly max at 850 hPa
    min_dew_point_spread_c: float       # 09:00–13:00 target, hourly min(T − Td)
    max_boundary_layer_height_m: float  # 09:00–13:00 target, hourly max
    soil_moisture_m3m3: float           # target day 09:00 soil_moisture_0_to_1cm
    rained_yesterday: bool              # target-1 day total precipitation ≥ threshold
    yesterday_precipitation_mm: float   # raw value for the log
    # Medium-priority signals (added from docs/future-factors.md):
    max_lifted_index: float             # 09:00–13:00; > +6 = too stable
    min_lifted_index: float             # 09:00–13:00; < −2 = storm risk
    max_cape_j_kg: float                # 09:00–13:00; captured for future calibration
    max_daytime_low_cloud_pct: float    # 09:00–13:00; low clouds shade slopes
    wind_850_direction_at_peak_deg: float  # direction at the morning 850 hPa speed peak
    max_wind_700_knots: float           # 09:00–13:00; 700 hPa crossflow aloft


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


async def fetch_snapshot(day: date, client: httpx.AsyncClient | None = None) -> MeteoSnapshot:
    # Span day-1 (overnight window) back through day-3 (for yesterday's rain
    # check + headroom on soil moisture). Single request.
    start = day - timedelta(days=1)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(
            OPEN_METEO_URL,
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
    finally:
        if owns_client:
            await client.aclose()

    return _parse(payload, day)


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

    if (
        not overnight_clouds
        or not morning_radiation
        or not morning_wind
        or not morning_temp
        or not morning_dew
        or not morning_blh
        or not morning_li
        or not morning_low_clouds
        or not morning_wind_700
    ):
        raise RuntimeError(
            f"Open-Meteo did not return expected hourly windows for {target.isoformat()}"
        )

    # Direction at the morning's peak 850 hPa wind — captures the dominant
    # upper-level flow without the circular-mean headaches of averaging angles.
    peak_hour_idx = _argmax_in_window(
        times, hourly["wind_speed_850hPa"], morning_start, morning_end
    )
    wind_850_dir = float(hourly["wind_direction_850hPa"][peak_hour_idx])

    # Soil moisture sampled at morning_start (09:00 target day).
    soil_moisture = _value_at(times, hourly["soil_moisture_0_to_1cm"], morning_start)
    if soil_moisture is None:
        raise RuntimeError("Open-Meteo did not return soil moisture at morning start")

    spreads = [t - d for t, d in zip(morning_temp, morning_dew)]
    yesterday_mm = sum(yesterday_rain)

    return MeteoSnapshot(
        day=target,
        overnight_cloud_cover_pct=sum(overnight_clouds) / len(overnight_clouds),
        morning_solar_radiation_wm2=max(morning_radiation),
        synoptic_wind_knots=max(morning_wind),
        min_dew_point_spread_c=min(spreads),
        max_boundary_layer_height_m=max(morning_blh),
        soil_moisture_m3m3=soil_moisture,
        rained_yesterday=yesterday_mm >= RAINED_YESTERDAY_MM,
        yesterday_precipitation_mm=round(yesterday_mm, 2),
        max_lifted_index=max(morning_li),
        min_lifted_index=min(morning_li),
        max_cape_j_kg=max(morning_cape) if morning_cape else 0.0,
        max_daytime_low_cloud_pct=max(morning_low_clouds),
        wind_850_direction_at_peak_deg=wind_850_dir,
        max_wind_700_knots=max(morning_wind_700),
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
