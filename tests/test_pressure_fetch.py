"""Unit test for the Open-Meteo pressure fetcher.

Uses httpx.MockTransport so we don't hit the network. Verifies:
  - the request targets the Open-Meteo forecast endpoint
  - the three stations are batched into a single call
  - the response is parsed into a PressureSnapshot with correct deltas
  - replay mode (target_day set) swaps `current` for `hourly` and picks
    the 08:00 Europe/Berlin reading of the target day (the live job hour)
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from oracle.config import OPEN_METEO_HISTORICAL_FORECAST_URL, OPEN_METEO_URL
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


@pytest.mark.asyncio
async def test_fetch_snapshot_replay_mode_uses_hourly_and_picks_08_local():
    """Replay mode (target_day set) must use `hourly=pressure_msl` with a
    date range, target the archive host, and pick the 08:00 Europe/Berlin
    reading — the hour the live 08:00 CET job samples `current` pressure."""
    replay_day = date(2021, 6, 15)
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        # Three locations, each with an hourly timeseries for 2021-06-15.
        return httpx.Response(200, json=[
            {  # Munich: pick 1018.4 at 08:00 local
                "hourly": {
                    "time": ["2021-06-15T00:00", "2021-06-15T08:00", "2021-06-15T18:00"],
                    "pressure_msl": [1019.0, 1018.4, 1017.2],
                },
            },
            {  # Innsbruck: pick 1016.0 at 08:00 local
                "hourly": {
                    "time": ["2021-06-15T00:00", "2021-06-15T08:00", "2021-06-15T18:00"],
                    "pressure_msl": [1016.5, 1016.0, 1014.8],
                },
            },
            {  # Bolzano: pick 1020.5 at 08:00 local
                "hourly": {
                    "time": ["2021-06-15T00:00", "2021-06-15T08:00", "2021-06-15T18:00"],
                    "pressure_msl": [1021.0, 1020.5, 1019.3],
                },
            },
        ])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await fetch_snapshot(
            client=client, host=OPEN_METEO_HISTORICAL_FORECAST_URL, target_day=replay_day
        )

    req = captured["req"]
    assert str(req.url).startswith(OPEN_METEO_HISTORICAL_FORECAST_URL)
    assert "current" not in req.url.params  # replay uses hourly, not current
    assert req.url.params["hourly"] == "pressure_msl"
    assert req.url.params["timezone"] == "Europe/Berlin"
    assert req.url.params["start_date"] == "2021-06-15"
    assert req.url.params["end_date"] == "2021-06-15"

    # The 08:00 local reading of each station is what we picked.
    assert snapshot.thermik_north.hpa == pytest.approx(1018.4)
    assert snapshot.thermik_south.hpa == pytest.approx(1016.0)
    assert snapshot.foehn_south.hpa == pytest.approx(1020.5)
    assert snapshot.thermik_north.measured_at.hour == 8
    assert snapshot.thermik_delta_hpa == pytest.approx(2.4)
    assert snapshot.foehn_delta_hpa == pytest.approx(4.5)


@pytest.mark.asyncio
async def test_fetch_snapshot_replay_mode_raises_if_target_hour_missing():
    """If the archive doesn't cover the requested day, the pillar must
    raise with a clear error rather than silently returning wrong data."""
    from datetime import date
    replay_day = date(2021, 6, 15)

    def handler(request: httpx.Request) -> httpx.Response:
        # Server returns a timeseries for a different day — the 08:00
        # reading of the target day isn't in there.
        return httpx.Response(200, json=[
            {"hourly": {"time": ["2020-07-01T09:00"], "pressure_msl": [1019.0]}},
            {"hourly": {"time": ["2020-07-01T09:00"], "pressure_msl": [1016.5]}},
            {"hourly": {"time": ["2020-07-01T09:00"], "pressure_msl": [1021.0]}},
        ])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="not in hourly timeseries"):
            await fetch_snapshot(
                client=client, host=OPEN_METEO_HISTORICAL_FORECAST_URL, target_day=replay_day
            )
