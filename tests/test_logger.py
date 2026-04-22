"""Unit tests for the calibration logger."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from oracle.config import ADDICTED_SPORTS_BASE_URL, StationRole
from oracle.engine import Forecast
from oracle.knowledge.rules import Signal, Verdict
from oracle.logger import LocalRunStore, backfill_run, forecast_to_dict, write_run
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureReading, PressureSnapshot


def _forecast() -> Forecast:
    now = datetime(2026, 4, 22, 9, 0)
    return Forecast(
        overall=Signal.NO_GO,
        verdicts=[Verdict("alpenpumpe_threshold", Signal.NO_GO, "Δ=1.0 hPa below threshold")],
        pressure=PressureSnapshot(
            alpenpumpe_north=PressureReading("Munich", 1020.0, now),
            alpenpumpe_south=PressureReading("Innsbruck", 1019.0, now),
            foehn_south=PressureReading("Bolzano", 1017.0, now),
        ),
        meteo=MeteoSnapshot(
            day=date(2026, 4, 22),
            overnight_cloud_cover_pct=35.0,
            morning_solar_radiation_wm2=832.0,
            synoptic_wind_knots=8.0,
            min_dew_point_spread_c=9.0,
            max_boundary_layer_height_m=1200.0,
            soil_moisture_m3m3=0.22,
            rained_yesterday=False,
            yesterday_precipitation_mm=0.0,
        ),
        winds=[WindReading("Urfeld", StationRole.SHORE, 2.1, 4.5, None, now)],
    )


def test_forecast_to_dict_round_trips_raw_inputs():
    d = forecast_to_dict(_forecast(), date(2026, 4, 22))
    assert d["overall"] == "no_go"
    assert d["inputs"]["pressure"]["alpenpumpe_delta_hpa"] == pytest.approx(1.0)
    assert d["inputs"]["pressure"]["foehn_delta_hpa"] == pytest.approx(-2.0)
    assert d["inputs"]["meteo"]["overnight_cloud_cover_pct"] == 35.0
    assert d["inputs"]["measurements"][0]["station"] == "Urfeld"


def test_write_run_preserves_existing_ground_truth(tmp_path: Path):
    target = date(2026, 4, 22)
    path = tmp_path / f"{target.isoformat()}.json"
    path.write_text(json.dumps({
        "day": target.isoformat(), "overall": "go", "verdicts": [], "inputs": {},
        "chat_messages": [],
        "ground_truth": {"machine": {"peak_avg_knots": 14.0}, "human": "great day"},
    }))

    written = write_run(_forecast(), target, store=LocalRunStore(tmp_path))
    assert written == str(path)
    data = json.loads(path.read_text())
    # New verdict written, but ground truth must survive the overwrite.
    assert data["overall"] == "no_go"
    assert data["ground_truth"]["machine"]["peak_avg_knots"] == 14.0
    assert data["ground_truth"]["human"] == "great day"


@pytest.mark.asyncio
async def test_backfill_merges_machine_ground_truth(tmp_path: Path):
    target = date(2026, 4, 22)
    write_run(_forecast(), target, store=LocalRunStore(tmp_path))

    _URFELD_HTML = (
        '<html><head><meta name="csrf-token" content="T"></head></html>'
    )
    # Two samples on 2026-04-22, one on 2026-04-23 that must be filtered out.
    _URFELD_JSON = {
        "measurment": {
            "417 2026-04-22 11:05:00": {
                "wsavg": "8.5", "wsmax": "12.1",
                "tsdatetime": "2026-04-22 11:05:00", "utctstamp": "1",
            },
            "417 2026-04-22 13:40:00": {
                "wsavg": "13.2", "wsmax": "18.9",
                "tsdatetime": "2026-04-22 13:40:00", "utctstamp": "2",
            },
            "417 2026-04-23 00:05:00": {
                "wsavg": "3.1", "wsmax": "4.0",
                "tsdatetime": "2026-04-23 00:05:00", "utctstamp": "3",
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{ADDICTED_SPORTS_BASE_URL}/webcam/walchensee/urfeld/"):
            return httpx.Response(200, text=_URFELD_HTML)
        if url.startswith(f"{ADDICTED_SPORTS_BASE_URL}/fileadmin/webcam/src/getWeatherData.php"):
            return httpx.Response(200, json=_URFELD_JSON)
        raise AssertionError(f"unexpected URL: {url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await backfill_run(target, store=LocalRunStore(tmp_path), client=client)

    data = json.loads((tmp_path / f"{target.isoformat()}.json").read_text())
    machine = data["ground_truth"]["machine"]
    assert machine["sample_count"] == 2  # the 2026-04-23 sample filtered out
    assert machine["peak_avg_knots"] == pytest.approx(13.2)
    assert machine["peak_gust_knots"] == pytest.approx(18.9)
    assert machine["samples_above_8kt"] == 2
    assert machine["samples_above_12kt"] == 1
    assert machine["first_ignition_at"] == "2026-04-22T11:05:00"


@pytest.mark.asyncio
async def test_backfill_raises_when_no_forecast_logged(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="run `oracle forecast` first"):
        await backfill_run(date(2026, 4, 22), store=LocalRunStore(tmp_path))
