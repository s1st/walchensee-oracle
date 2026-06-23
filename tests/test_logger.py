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
        verdicts=[Verdict(
            "thermik", Signal.NO_GO,
            reason_en="Δ=1.0 hPa below threshold",
            reason_de="Δ=1.0 hPa unter Schwellwert",
        )],
        pressure=PressureSnapshot(
            thermik_north=PressureReading("Munich", 1020.0, now),
            thermik_south=PressureReading("Innsbruck", 1019.0, now),
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
            max_lifted_index=3.0,
            min_lifted_index=1.0,
            max_cape_j_kg=0.0,
            max_daytime_low_cloud_pct=20.0,
            wind_850_direction_at_peak_deg=30.0,
            max_wind_700_knots=10.0,
        ),
        winds=[WindReading("Urfeld", StationRole.SHORE, 2.1, 4.5, None, now)],
        lake_temp=None,
    )


def test_forecast_to_dict_round_trips_raw_inputs():
    d = forecast_to_dict(_forecast(), date(2026, 4, 22))
    assert d["overall"] == "no_go"
    assert d["inputs"]["pressure"]["thermik_delta_hpa"] == pytest.approx(1.0)
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
                "wtemp": "12.4", "wsavg": "8.5", "wsmax": "12.1",
                "temp": "10.2", "dp": "3.5", "rh": "52", "rp": "911.8", "rain": "0.0",
                "tsdatetime": "2026-04-22 11:05:00", "utctstamp": "1",
            },
            "417 2026-04-22 13:40:00": {
                "wtemp": "13.1", "wsavg": "13.2", "wsmax": "18.9",
                "temp": "14.0", "dp": "5.1", "rh": "48", "rp": "911.4", "rain": "0.0",
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
    # Water-temp ground truth: mean of 12.4 and 13.1, captured per-sample too.
    assert machine["mean_water_temp_c"] == pytest.approx(12.75)
    assert all(s["water_temp_c"] is not None for s in machine["samples"])
    # All buoy-side fields are captured per-sample (raw inputs preserved
    # for replay — see docs/future-buoy-signals.md).
    for s in machine["samples"]:
        assert s["air_temp_c"] is not None
        assert s["dew_point_c"] is not None
        assert s["rel_humidity_pct"] is not None
        assert s["pressure_hpa"] is not None
        assert s["rain_mm"] == 0.0  # 0.0 is a real reading, not a miss


@pytest.mark.asyncio
async def test_backfill_raises_when_no_forecast_logged(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="run `oracle forecast` first"):
        await backfill_run(date(2026, 4, 22), store=LocalRunStore(tmp_path))


# --- replay record routing --------------------------------------------


def _replay_forecast() -> Forecast:
    """Build a minimal replay Forecast — pressure/meteo are filled in just
    enough for `forecast_to_dict` to serialise without errors. The replay
    path doesn't re-fetch; this is for the logger test only."""
    now = datetime(2021, 6, 15, 9, 0)
    from oracle.pillars.pressure import PressureReading, PressureSnapshot
    from oracle.pillars.meteo import MeteoSnapshot
    from oracle.pillars.measurements import LakeTempSnapshot
    return Forecast(
        overall=Signal.GO,
        verdicts=[Verdict("thermik", Signal.GO,
                          reason_en="Δ favourable", reason_de="Δ günstig")],
        pressure=PressureSnapshot(
            thermik_north=PressureReading("Munich", 1018.4, now),
            thermik_south=PressureReading("Innsbruck", 1016.0, now),
            foehn_south=PressureReading("Bolzano", 1020.5, now),
        ),
        meteo=MeteoSnapshot(
            day=date(2021, 6, 15),
            overnight_cloud_cover_pct=20.0,
            morning_solar_radiation_wm2=750.0,
            synoptic_wind_knots=5.0,
            min_dew_point_spread_c=9.0,
            max_boundary_layer_height_m=1200.0,
            soil_moisture_m3m3=0.20,
            rained_yesterday=False,
            yesterday_precipitation_mm=0.0,
            max_lifted_index=2.0,
            min_lifted_index=2.0,
            max_cape_j_kg=0.0,
            max_daytime_low_cloud_pct=25.0,
            wind_850_direction_at_peak_deg=20.0,
            max_wind_700_knots=15.0,
            morning_air_temp_c=14.0,
        ),
        winds=[WindReading("Urfeld", StationRole.SHORE, 8.0, 12.0, None, now,
                           water_temp_c=18.5)],
        lake_temp=LakeTempSnapshot(18.5, now, "Urfeld"),
        replay_day=date(2021, 6, 15),
        replay_source="historical-forecast",
    )


def test_write_run_replay_routes_to_replay_subdir(tmp_path: Path):
    """A replay Forecast must land in `runs/replay/<date>.json`, not the
    project root, and must NOT pollute the calibrate loop's view of days."""
    target = date(2021, 6, 15)
    location = write_run(_replay_forecast(), target, store=LocalRunStore(tmp_path))

    # Path is under the replay/ subdir.
    assert location.endswith("replay/2021-06-15.json")
    assert (tmp_path / "replay" / "2021-06-15.json").exists()
    # The project root has no file for this day.
    assert not (tmp_path / "2021-06-15.json").exists()

    # list_days() must skip the replay/ subdir.
    assert LocalRunStore(tmp_path).list_days() == []

    # The replay record itself carries replay metadata.
    data = json.loads((tmp_path / "replay" / "2021-06-15.json").read_text())
    assert data["replay_day"] == "2021-06-15"
    assert data["replay_source"] == "historical-forecast"
    # And lacks the live-forecast-only fields.
    assert "verdicts_resimulated" not in data
    assert data["ground_truth"] == {"machine": None, "human": None}


def test_forecast_to_dict_carries_replay_metadata():
    """`replay_day` + `replay_source` round-trip through the canonical
    serialiser so the JSON dump (--json stdout, written record) is
    self-describing."""
    d = forecast_to_dict(_replay_forecast(), date(2021, 6, 15))
    assert d["replay_day"] == "2021-06-15"
    assert d["replay_source"] == "historical-forecast"
    # Live forecasts omit these keys entirely.
    live = _forecast()
    live_d = forecast_to_dict(live, date(2026, 4, 22))
    assert "replay_day" not in live_d
    assert "replay_source" not in live_d


def test_list_days_skips_replay_subdir_in_gcs(monkeypatch):
    """GCSRunStore.list_days must skip the replay/ sub-prefix; replay
    records live under runs/replay/ and the project loop only sees the
    top-level files. We can't hit GCS from unit tests, so we mock
    list_blobs to return a mix of forecast + replay paths and check the
    filter logic in isolation."""
    from oracle.logger import GCSRunStore
    all_blobs = [
        type("B", (), {"name": "runs/2026-04-22.json"}),
        type("B", (), {"name": "runs/2026-04-23.json"}),
        type("B", (), {"name": "runs/replay/2021-06-15.json"}),
        type("B", (), {"name": "runs/replay/2020-07-01.json"}),
        type("B", (), {"name": "runs/something/2025-01-01.json"}),  # any other sub-prefix
    ]
    # Real GCS list_blobs filters by prefix server-side; mirror that.
    def fake_list_blobs(self, bucket, prefix):
        return iter(b for b in all_blobs if b.name.startswith(prefix))
    store = GCSRunStore.__new__(GCSRunStore)  # bypass __post_init__ (no GCS creds in unit test)
    store._client = type("C", (), {"list_blobs": fake_list_blobs})()
    store._bucket = None
    assert store.list_days() == ["2026-04-22", "2026-04-23"]
    assert store.list_replays() == ["2020-07-01", "2021-06-15"]


def test_local_store_replay_namespace_round_trip(tmp_path: Path):
    """read_replay/write_replay/list_replays operate on the replay/ subdir
    and never leak into the main namespace."""
    store = LocalRunStore(tmp_path)
    assert store.list_replays() == []
    assert store.read_replay("2021-06-15") is None

    location = store.write_replay("2021-06-15", {"overall": "go"})
    assert location.endswith("replay/2021-06-15.json")
    assert store.read_replay("2021-06-15") == {"overall": "go"}
    assert store.list_replays() == ["2021-06-15"]
    # Main namespace unaffected.
    assert store.list_days() == []
    assert store.read("2021-06-15") is None
