"""Unit test for the Open-Meteo meteo fetcher.

Fabricates a 48-hour hourly response covering (day-1, day) with sentinel
values chosen so each window's aggregate is easy to verify.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import pytest

from oracle.config import OPEN_METEO_URL
from oracle.pillars.meteo import fetch_snapshot


def _hourly_payload(target: date) -> dict:
    yesterday = target - timedelta(days=1)
    times: list[str] = []
    clouds: list[float] = []
    radiation: list[float] = []
    wind_850: list[float] = []

    for d in (yesterday, target):
        for h in range(24):
            t = datetime.combine(d, datetime.min.time()).replace(hour=h)
            times.append(t.isoformat(timespec="minutes"))
            # Overnight window (22:00 prev → 06:00 target): clouds=20, elsewhere=90.
            in_overnight = (d == yesterday and h >= 22) or (d == target and h < 6)
            clouds.append(20.0 if in_overnight else 90.0)
            # Morning window (09:00–13:00 target): radiation peaks at 750, else 0.
            in_morning = d == target and 9 <= h <= 13
            radiation.append(750.0 if h == 12 and in_morning else (400.0 if in_morning else 0.0))
            # 850 hPa wind: morning window max = 12.5 kt, nights quieter.
            wind_850.append(12.5 if h == 11 and in_morning else (5.0 if in_morning else 3.0))

    return {
        "hourly": {
            "time": times,
            "cloud_cover": clouds,
            "shortwave_radiation": radiation,
            "wind_speed_850hPa": wind_850,
        }
    }


@pytest.mark.asyncio
async def test_fetch_snapshot_aggregates_windows_correctly():
    target = date(2026, 5, 15)
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, json=_hourly_payload(target))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        snap = await fetch_snapshot(target, client=client)

    req = captured["req"]
    assert str(req.url).startswith(OPEN_METEO_URL)
    assert req.url.params["hourly"] == "cloud_cover,shortwave_radiation,wind_speed_850hPa"
    assert req.url.params["wind_speed_unit"] == "kn"
    assert req.url.params["start_date"] == "2026-05-14"
    assert req.url.params["end_date"] == "2026-05-15"

    assert snap.day == target
    assert snap.overnight_cloud_cover_pct == pytest.approx(20.0)  # all 8 hours = 20
    assert snap.morning_solar_radiation_wm2 == pytest.approx(750.0)  # max hourly
    assert snap.synoptic_wind_knots == pytest.approx(12.5)  # max hourly


@pytest.mark.asyncio
async def test_fetch_snapshot_raises_when_window_empty():
    target = date(2026, 5, 15)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hourly": {
            "time": [], "cloud_cover": [], "shortwave_radiation": [], "wind_speed_850hPa": []
        }})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="did not return expected hourly windows"):
            await fetch_snapshot(target, client=client)
