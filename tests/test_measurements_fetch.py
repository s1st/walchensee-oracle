"""Unit tests for the measurements pillar (Bright Sky / DWD only)."""
from __future__ import annotations

import httpx
import pytest

from oracle.config import BRIGHT_SKY_CURRENT_URL, StationRole
from oracle.pillars.measurements import fetch_latest


_BRIGHT_SKY_PAYLOAD = {
    "weather": {
        "source_id": 332569,
        "timestamp": "2026-04-19T18:00:00+00:00",
        "wind_speed_10": 14.4,
        "wind_gust_speed_10": 20.9,
        "wind_direction_10": 160,
        "fallback_source_ids": {"wind_speed_10": 186686},
    },
    "sources": [
        {"id": 332569, "station_name": "Mittenwald-Buckelwie"},
        {"id": 186686, "station_name": "Mittenwald/Obb."},
    ],
}


@pytest.mark.asyncio
async def test_fetch_latest_returns_bright_sky_reading():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(BRIGHT_SKY_CURRENT_URL):
            return httpx.Response(200, json=_BRIGHT_SKY_PAYLOAD)
        raise AssertionError(f"unexpected URL: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        latest = await fetch_latest(client=client)

    assert len(latest.winds) == 1
    reading = latest.winds[0]
    assert reading.role is StationRole.IGNITION_REFERENCE
    assert reading.station == "Mittenwald/Obb."
    assert reading.avg_knots == pytest.approx(14.4 * 0.5399568)
    assert reading.direction_deg == pytest.approx(160.0)
    assert latest.lake_temp is None


@pytest.mark.asyncio
async def test_fetch_latest_raises_when_bright_sky_missing_required_field():
    bad_payload = {
        "weather": {
            "source_id": 1,
            "timestamp": "2026-04-19T18:00:00+00:00",
            "wind_speed_10": None,
            "wind_gust_speed_10": None,
            "wind_direction_10": None,
        },
        "sources": [{"id": 1, "station_name": "X"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await fetch_latest(client=client)
