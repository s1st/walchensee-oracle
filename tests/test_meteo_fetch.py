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
    temps: list[float] = []
    dew_points: list[float] = []
    blh: list[float] = []
    soil: list[float] = []
    precip: list[float] = []

    for d in (yesterday, target):
        for h in range(24):
            t = datetime.combine(d, datetime.min.time()).replace(hour=h)
            times.append(t.isoformat(timespec="minutes"))

            in_overnight = (d == yesterday and h >= 22) or (d == target and h < 6)
            in_morning = d == target and 9 <= h <= 13

            clouds.append(20.0 if in_overnight else 90.0)
            radiation.append(750.0 if h == 12 and in_morning else (400.0 if in_morning else 0.0))
            wind_850.append(12.5 if h == 11 and in_morning else (5.0 if in_morning else 3.0))

            # Morning window: T sweeps 15→19°C, dew point steady at 6°C → spreads 9→13°C.
            temps.append((15.0 + (h - 9)) if in_morning else 8.0)
            dew_points.append(6.0 if in_morning else 4.0)
            # BLH peaks at 1200 m in morning; otherwise 300 m.
            blh.append(1200.0 if h == 12 and in_morning else (800.0 if in_morning else 300.0))
            # Soil moisture constant 0.20 (dry).
            soil.append(0.20)
            # Yesterday gets 0.5 mm total (below 2 mm threshold → rained_yesterday=False).
            precip.append(0.5 if d == yesterday and h == 10 else 0.0)

    return {
        "hourly": {
            "time": times,
            "cloud_cover": clouds,
            "shortwave_radiation": radiation,
            "wind_speed_850hPa": wind_850,
            "temperature_2m": temps,
            "dew_point_2m": dew_points,
            "boundary_layer_height": blh,
            "soil_moisture_0_to_1cm": soil,
            "precipitation": precip,
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
    assert "dew_point_2m" in req.url.params["hourly"]
    assert "boundary_layer_height" in req.url.params["hourly"]
    assert "soil_moisture_0_to_1cm" in req.url.params["hourly"]
    assert "precipitation" in req.url.params["hourly"]
    assert req.url.params["start_date"] == "2026-05-14"
    assert req.url.params["end_date"] == "2026-05-15"

    assert snap.day == target
    assert snap.overnight_cloud_cover_pct == pytest.approx(20.0)
    assert snap.morning_solar_radiation_wm2 == pytest.approx(750.0)
    assert snap.synoptic_wind_knots == pytest.approx(12.5)
    # At h=9 T=15, Td=6 → spread=9. Spread grows to 13 by h=13. min = 9.
    assert snap.min_dew_point_spread_c == pytest.approx(9.0)
    assert snap.max_boundary_layer_height_m == pytest.approx(1200.0)
    assert snap.soil_moisture_m3m3 == pytest.approx(0.20)
    assert snap.rained_yesterday is False
    assert snap.yesterday_precipitation_mm == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_fetch_snapshot_raises_when_window_empty():
    target = date(2026, 5, 15)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hourly": {
            "time": [], "cloud_cover": [], "shortwave_radiation": [],
            "wind_speed_850hPa": [], "temperature_2m": [], "dew_point_2m": [],
            "boundary_layer_height": [], "soil_moisture_0_to_1cm": [],
            "precipitation": [],
        }})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="did not return expected hourly windows"):
            await fetch_snapshot(target, client=client)
