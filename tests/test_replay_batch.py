"""Batch replay: year-chunked archive fetches + buoy reconstruction from
stored ground truth.

Uses httpx.MockTransport for the Open-Meteo range requests and a
LocalRunStore seeded with stub records (the shape the historical backfill
writes). Verifies:
  - buoy samples round-trip from `ground_truth.machine.samples`
  - `snapshot_from_range` slicing matches the single-day fetch
  - the batch makes two requests per year, not two per day
  - per-day archive holes are skipped, not fatal
  - replay records land in `replay/` with replay metadata
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from oracle.logger import LocalRunStore
from oracle.pillars import meteo, pressure
from oracle.replay import run_replay_batch, samples_from_record

_HOURLY_KEYS = [
    "cloud_cover", "shortwave_radiation", "wind_speed_850hPa", "temperature_2m",
    "dew_point_2m", "boundary_layer_height", "soil_moisture_0_to_1cm",
    "precipitation", "cape", "lifted_index", "cloud_cover_low",
    "wind_speed_700hPa", "wind_direction_850hPa",
]

# Benign hourly values: clear night, sunny morning, light wind aloft.
_VALUE_BY_KEY = {
    "cloud_cover": 20.0, "shortwave_radiation": 700.0, "wind_speed_850hPa": 5.0,
    "temperature_2m": 18.0, "dew_point_2m": 9.0, "boundary_layer_height": 1200.0,
    "soil_moisture_0_to_1cm": 0.2, "precipitation": 0.0, "cape": 50.0,
    "lifted_index": 3.0, "cloud_cover_low": 10.0, "wind_speed_700hPa": 8.0,
    "wind_direction_850hPa": 20.0,
}


def _hour_axis(start: date, end: date) -> list[str]:
    hours = []
    d = start
    while d <= end:
        for h in range(24):
            hours.append(f"{d.isoformat()}T{h:02d}:00")
        d += timedelta(days=1)
    return hours


def _meteo_range_payload(start: date, end: date) -> dict:
    times = _hour_axis(start, end)
    hourly: dict = {"time": times}
    for key in _HOURLY_KEYS:
        hourly[key] = [_VALUE_BY_KEY[key]] * len(times)
    return {"hourly": hourly}


def _pressure_range_payload(start: date, end: date) -> list[dict]:
    times = _hour_axis(start, end)
    return [
        {"hourly": {"time": times, "pressure_msl": [hpa] * len(times)}}
        for hpa in (1018.4, 1016.0, 1020.5)  # Munich, Innsbruck, Bolzano
    ]


def _stub_record(day: date, samples: list[dict] | None = None) -> dict:
    """Minimal historical-backfill stub: ground truth only, no verdicts."""
    return {
        "day": day.isoformat(),
        "ground_truth": {
            "machine": {
                "source": "addicted-sports-urfeld",
                "samples": samples if samples is not None else [
                    {"t": f"{day.isoformat()}T10:00:00", "avg_kt": 3.0, "gust_kt": 5.0,
                     "water_temp_c": 18.0, "air_temp_c": 12.0, "dew_point_c": None,
                     "rel_humidity_pct": None, "pressure_hpa": None, "rain_mm": None},
                    {"t": f"{day.isoformat()}T14:00:00", "avg_kt": 8.0, "gust_kt": 12.0,
                     "water_temp_c": 18.5, "air_temp_c": 15.0, "dew_point_c": 7.0,
                     "rel_humidity_pct": 60.0, "pressure_hpa": 1015.0, "rain_mm": 0.0},
                ],
            },
            "human": None,
        },
    }


def _seed_store(tmp_path: Path, days: list[date]) -> LocalRunStore:
    store = LocalRunStore(tmp_path)
    for d in days:
        store.write(d.isoformat(), _stub_record(d))
    return store


def _archive_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    """Serve pressure/meteo range payloads covering whatever span is asked."""
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = date.fromisoformat(request.url.params["start_date"])
        end = date.fromisoformat(request.url.params["end_date"])
        if request.url.params["hourly"] == "pressure_msl":
            return httpx.Response(200, json=_pressure_range_payload(start, end))
        return httpx.Response(200, json=_meteo_range_payload(start, end))
    return httpx.MockTransport(handler)


# --- samples_from_record ------------------------------------------------


def test_samples_from_record_round_trips_full_payload():
    day = date(2021, 6, 15)
    samples = samples_from_record(_stub_record(day))
    assert len(samples) == 2
    last = samples[-1]
    assert last.measured_at == datetime(2021, 6, 15, 14, 0)
    assert last.avg_knots == 8.0
    assert last.water_temp_c == 18.5
    assert last.rel_humidity_pct == 60.0


def test_samples_from_record_tolerates_legacy_and_missing():
    # Legacy sample without the full-payload keys.
    legacy = {"ground_truth": {"machine": {"samples": [
        {"t": "2020-07-01T12:00:00", "avg_kt": 6.0, "gust_kt": 9.0},
    ]}}}
    samples = samples_from_record(legacy)
    assert samples[0].water_temp_c is None
    # No machine block / no record at all.
    assert samples_from_record({"ground_truth": {"machine": None}}) == []
    assert samples_from_record(None) == []


def test_samples_from_record_sorts_chronologically():
    day = date(2021, 6, 15)
    record = _stub_record(day, samples=[
        {"t": f"{day.isoformat()}T14:00:00", "avg_kt": 8.0, "gust_kt": 12.0},
        {"t": f"{day.isoformat()}T10:00:00", "avg_kt": 3.0, "gust_kt": 5.0},
    ])
    samples = samples_from_record(record)
    assert [s.measured_at.hour for s in samples] == [10, 14]


# --- snapshot_from_range ------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_from_range_matches_single_day_fetch():
    """Slicing a day out of a range payload must produce the identical
    snapshot the per-day fetch path produces."""
    target = date(2021, 6, 15)
    range_payload = _meteo_range_payload(target - timedelta(days=3), target + timedelta(days=2))
    times = meteo.parse_times(range_payload)
    sliced_snap = meteo.snapshot_from_range(range_payload, times, target)

    def handler(request: httpx.Request) -> httpx.Response:
        start = date.fromisoformat(request.url.params["start_date"])
        end = date.fromisoformat(request.url.params["end_date"])
        return httpx.Response(200, json=_meteo_range_payload(start, end))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        single_snap = await meteo.fetch_snapshot(target, client=client)

    assert sliced_snap == single_snap


def test_snapshot_from_range_raises_outside_coverage():
    payload = _meteo_range_payload(date(2021, 6, 10), date(2021, 6, 16))
    times = meteo.parse_times(payload)
    with pytest.raises(RuntimeError, match="does not cover"):
        meteo.snapshot_from_range(payload, times, date(2022, 6, 15))


def test_pressure_snapshot_at_morning_picks_08_local():
    start, end = date(2021, 6, 14), date(2021, 6, 16)
    times = [datetime.fromisoformat(t) for t in _hour_axis(start, end)]
    series = pressure.PressureHourlyRange(
        times=times,
        values_by_station={
            "Munich": [1018.4] * len(times),
            "Innsbruck": [1016.0] * len(times),
            "Bolzano": [1020.5] * len(times),
        },
    )
    snap = pressure.snapshot_at_morning(series, date(2021, 6, 15))
    assert snap.thermik_north.measured_at == datetime(2021, 6, 15, 8, 0)
    assert snap.thermik_delta_hpa == pytest.approx(2.4)


# --- run_replay_batch ---------------------------------------------------


def _mock_client(transport: httpx.MockTransport):
    """Factory for monkeypatching httpx.AsyncClient inside run_replay_batch."""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    return factory


@pytest.mark.asyncio
async def test_batch_makes_two_requests_per_year(tmp_path: Path, monkeypatch):
    days = [date(2021, 6, 15), date(2021, 6, 16), date(2022, 7, 1)]
    store = _seed_store(tmp_path, days)
    requests: list[httpx.Request] = []
    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(_archive_transport(requests)))

    result = await run_replay_batch(date(2021, 1, 1), date(2022, 12, 31), store=store)

    assert result.skipped == []
    assert result.replayed == ["2021-06-15", "2021-06-16", "2022-07-01"]
    # Two years × (1 pressure + 1 meteo) = 4 requests, not 6.
    assert len(requests) == 4
    # Replay records exist, with replay metadata + buoy projection.
    record = json.loads((tmp_path / "replay" / "2021-06-15.json").read_text())
    assert record["replay_day"] == "2021-06-15"
    assert record["replay_source"] == "historical-forecast"
    # The original stubs are untouched (still no verdict keys beyond the stub's).
    stub = json.loads((tmp_path / "2021-06-15.json").read_text())
    assert "replay_day" not in stub


@pytest.mark.asyncio
async def test_batch_skips_days_outside_archive_coverage(tmp_path: Path, monkeypatch):
    """A day whose 08:00 pressure hour is missing gets skipped with a reason;
    the rest of the year still replays."""
    days = [date(2021, 6, 15), date(2021, 6, 16)]
    store = _seed_store(tmp_path, days)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = date.fromisoformat(request.url.params["start_date"])
        # Serve one day short at the end: 06-16 is missing from the archive.
        end = date(2021, 6, 15)
        if request.url.params["hourly"] == "pressure_msl":
            return httpx.Response(200, json=_pressure_range_payload(start, end))
        return httpx.Response(200, json=_meteo_range_payload(start, end))

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(httpx.MockTransport(handler)))

    result = await run_replay_batch(date(2021, 1, 1), date(2021, 12, 31), store=store)

    assert result.replayed == ["2021-06-15"]
    assert len(result.skipped) == 1
    iso, reason = result.skipped[0]
    assert iso == "2021-06-16"
    assert "not in hourly timeseries" in reason
    assert not (tmp_path / "replay" / "2021-06-16.json").exists()


@pytest.mark.asyncio
async def test_batch_passes_models_pin_through(tmp_path: Path, monkeypatch):
    store = _seed_store(tmp_path, [date(2021, 6, 15)])
    requests: list[httpx.Request] = []
    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(_archive_transport(requests)))

    await run_replay_batch(
        date(2021, 1, 1), date(2021, 12, 31), store=store, models="ecmwf_ifs",
    )

    assert all(r.url.params["models"] == "ecmwf_ifs" for r in requests)


@pytest.mark.asyncio
async def test_batch_with_no_days_in_range_makes_no_requests(tmp_path: Path, monkeypatch):
    store = _seed_store(tmp_path, [date(2021, 6, 15)])
    requests: list[httpx.Request] = []
    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(_archive_transport(requests)))

    result = await run_replay_batch(date(2019, 1, 1), date(2019, 12, 31), store=store)

    assert result.replayed == []
    assert result.skipped == []
    assert requests == []
