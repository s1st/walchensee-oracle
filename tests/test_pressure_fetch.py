"""Unit test for the Open-Meteo pressure fetcher.

Uses httpx.MockTransport so we don't hit the network. Verifies:
  - the request targets the Open-Meteo forecast endpoint
  - the three stations are batched into a single call
  - the response is parsed into a PressureSnapshot with correct deltas
"""
from __future__ import annotations

import httpx
import pytest

from oracle.config import OPEN_METEO_URL
from oracle.pillars.pressure import fetch_snapshot


_FAKE_PAYLOAD = [
    {"current": {"time": "2026-04-19T10:00", "pressure_msl": 1022.5}},  # Munich
    {"current": {"time": "2026-04-19T10:00", "pressure_msl": 1019.0}},  # Innsbruck
    {"current": {"time": "2026-04-19T10:00", "pressure_msl": 1020.0}},  # Bolzano
]


@pytest.mark.asyncio
async def test_fetch_snapshot_parses_open_meteo_response():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, json=_FAKE_PAYLOAD)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await fetch_snapshot(client=client)

    req = captured["req"]
    assert str(req.url).startswith(OPEN_METEO_URL)
    assert req.url.params["current"] == "pressure_msl"
    assert req.url.params["latitude"].count(",") == 2  # three stations
    assert req.url.params["longitude"].count(",") == 2

    assert snapshot.thermik_north.station == "Munich"
    assert snapshot.thermik_south.station == "Innsbruck"
    assert snapshot.foehn_south.station == "Bolzano"
    assert snapshot.thermik_delta_hpa == pytest.approx(3.5)
    assert snapshot.foehn_delta_hpa == pytest.approx(1.0)
