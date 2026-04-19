"""Unit test for the Bright Sky measurements fetcher."""
from __future__ import annotations

import httpx
import pytest

from oracle.config import BRIGHT_SKY_CURRENT_URL, StationRole
from oracle.pillars.measurements import fetch_latest


_FAKE_PAYLOAD = {
    "weather": {
        "source_id": 332569,
        "timestamp": "2026-04-19T18:00:00+00:00",
        "wind_speed_10": 14.4,          # km/h → ~7.78 kt
        "wind_gust_speed_10": 20.9,     # km/h → ~11.28 kt
        "wind_direction_10": 160,
        "fallback_source_ids": {"wind_speed_10": 186686},
    },
    "sources": [
        {"id": 332569, "station_name": "Mittenwald-Buckelwie"},
        {"id": 186686, "station_name": "Mittenwald/Obb."},
    ],
}


@pytest.mark.asyncio
async def test_fetch_latest_converts_kmh_to_knots_and_picks_fallback_station():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, json=_FAKE_PAYLOAD)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        readings = await fetch_latest(client=client)

    req = captured["req"]
    assert str(req.url).startswith(BRIGHT_SKY_CURRENT_URL)
    assert "lat" in req.url.params and "lon" in req.url.params

    assert len(readings) == 1
    reading = readings[0]
    # Station name should come from the fallback source that actually has wind.
    assert reading.station == "Mittenwald/Obb."
    assert reading.role is StationRole.IGNITION_REFERENCE
    assert reading.avg_knots == pytest.approx(14.4 * 0.5399568)
    assert reading.gust_knots == pytest.approx(20.9 * 0.5399568)
    assert reading.direction_deg == pytest.approx(160.0)


@pytest.mark.asyncio
async def test_fetch_latest_raises_when_wind_missing():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "weather": {
                "source_id": 1,
                "timestamp": "2026-04-19T18:00:00+00:00",
                "wind_speed_10": None,
                "wind_gust_speed_10": None,
                "wind_direction_10": None,
            },
            "sources": [{"id": 1, "station_name": "X"}],
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="missing required field"):
            await fetch_latest(client=client)
