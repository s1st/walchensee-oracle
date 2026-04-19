"""Pillar 3 — meteorological conditions.

Overnight cooling (clear skies) + forecasted solar radiation the following
morning together decide whether the thermal engine can spin up at all. The
850 hPa wind is our proxy for synoptic flow above the boundary layer — if
that's already strong, it will override any local thermal cell.

Backend: Open-Meteo `forecast` endpoint, hourly variables in local time
(Europe/Berlin) so our window filters use physical hours without timezone math.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

import httpx

from oracle.config import OPEN_METEO_URL, URFELD


@dataclass
class MeteoSnapshot:
    day: date
    overnight_cloud_cover_pct: float   # 22:00 prev → 06:00 target, mean
    morning_solar_radiation_wm2: float # 09:00–13:00 target, hourly max
    synoptic_wind_knots: float         # 09:00–13:00 target, hourly max at 850 hPa


_OVERNIGHT = (time(22, 0), time(6, 0))
_MORNING = (time(9, 0), time(13, 0))


async def fetch_snapshot(day: date, client: httpx.AsyncClient | None = None) -> MeteoSnapshot:
    yesterday = day - timedelta(days=1)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(
            OPEN_METEO_URL,
            params={
                "latitude": URFELD.lat,
                "longitude": URFELD.lon,
                "hourly": "cloud_cover,shortwave_radiation,wind_speed_850hPa",
                "wind_speed_unit": "kn",
                "timezone": "Europe/Berlin",
                "start_date": yesterday.isoformat(),
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
    clouds = hourly["cloud_cover"]
    radiation = hourly["shortwave_radiation"]
    wind_850 = hourly["wind_speed_850hPa"]

    yesterday = target - timedelta(days=1)
    overnight_start = datetime.combine(yesterday, _OVERNIGHT[0])
    overnight_end = datetime.combine(target, _OVERNIGHT[1])
    morning_start = datetime.combine(target, _MORNING[0])
    morning_end = datetime.combine(target, _MORNING[1])

    overnight_clouds = _in_window(times, clouds, overnight_start, overnight_end)
    morning_radiation = _in_window(times, radiation, morning_start, morning_end, inclusive_end=True)
    morning_wind = _in_window(times, wind_850, morning_start, morning_end, inclusive_end=True)

    if not overnight_clouds or not morning_radiation or not morning_wind:
        raise RuntimeError(
            f"Open-Meteo did not return expected hourly windows for {target.isoformat()}"
        )

    return MeteoSnapshot(
        day=target,
        overnight_cloud_cover_pct=sum(overnight_clouds) / len(overnight_clouds),
        morning_solar_radiation_wm2=max(morning_radiation),
        synoptic_wind_knots=max(morning_wind),
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
