"""Batch replay: re-run the rules over a date range against the Open-Meteo
archives, pairing each day with the buoy ground truth already stored in the
runs bucket.

This exists so a calibration pass over the ~3,300 archived ground-truth days
doesn't make one request per day: archive ranges are fetched once per year
(two requests — pressure + meteo) and sliced locally, and the Urfeld buoy
curve is reconstructed from the stub records' `ground_truth.machine.samples`
instead of re-scraping addicted-sports thousands of times.

Per-day failures (day outside archive coverage, null pressure hour, missing
required meteo windows) are collected and reported, not fatal — a batch over
a decade will always have holes.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time

import httpx

from oracle.engine import (
    Forecast,
    ReplaySource,
    _REPLAY_HOSTS,
    _project_buoy_day_curve,
    aggregate,
    apply_rules,
)
from oracle.logger import RunStore, default_store, write_run
from oracle.pillars import meteo, pressure
from oracle.pillars.measurements import UrfeldSample


@dataclass
class ReplayBatchResult:
    replayed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (iso, reason)


def samples_from_record(record: dict | None) -> list[UrfeldSample]:
    """Reconstruct the buoy day-curve from a stored record's ground truth.

    The backfill serialises every `UrfeldSample` field into
    `ground_truth.machine.samples` (see `logger._machine_ground_truth`), so
    the round trip is lossless for records written after the full-payload
    capture (2026-06-12); older records yield samples with the extra fields
    None — same tolerance as a live scrape with sensors offline. Records
    without samples (or no ground truth at all) yield [], which the engine
    treats as a buoy outage.
    """
    machine = ((record or {}).get("ground_truth") or {}).get("machine") or {}
    samples = []
    for s in machine.get("samples") or []:
        samples.append(UrfeldSample(
            measured_at=datetime.fromisoformat(s["t"]),
            avg_knots=float(s["avg_kt"]),
            gust_knots=float(s["gust_kt"]),
            water_temp_c=s.get("water_temp_c"),
            air_temp_c=s.get("air_temp_c"),
            dew_point_c=s.get("dew_point_c"),
            rel_humidity_pct=s.get("rel_humidity_pct"),
            pressure_hpa=s.get("pressure_hpa"),
            rain_mm=s.get("rain_mm"),
        ))
    samples.sort(key=lambda s: s.measured_at)
    return samples


async def run_replay_batch(
    start: date,
    end: date,
    *,
    source: ReplaySource = "historical-forecast",
    models: str | None = None,
    store: RunStore | None = None,
    progress: Callable[[str], None] = lambda _msg: None,
) -> ReplayBatchResult:
    """Replay every stored ground-truth day in [start, end] (inclusive).

    Day selection comes from `store.list_days()` — i.e. the days the
    historical buoy backfill (or the live pipeline) has a record for; days
    with no record have no ground truth to calibrate against, so replaying
    them is pointless. Results land in `runs/replay/<date>.json` via the
    normal `write_run` routing, overwriting any previous replay of the day.

    `models` pins an Open-Meteo model (e.g. "ecmwf_ifs") for both pillars —
    recommended for scoring runs that span model-coverage eras, per
    docs/historical_forecasts.md.
    """
    store = store or default_store()
    host = _REPLAY_HOSTS[source]
    result = ReplayBatchResult()

    days = sorted(
        d for iso in store.list_days()
        if start <= (d := date.fromisoformat(iso)) <= end
    )
    if not days:
        return result

    by_year: dict[int, list[date]] = {}
    for d in days:
        by_year.setdefault(d.year, []).append(d)

    async with httpx.AsyncClient(timeout=60.0) as client:
        for year, year_days in sorted(by_year.items()):
            progress(
                f"{year}: fetching archive ranges "
                f"({year_days[0].isoformat()} → {year_days[-1].isoformat()}, "
                f"{len(year_days)} days)"
            )
            try:
                series, payload = await asyncio.gather(
                    pressure.fetch_hourly_range(
                        year_days[0], year_days[-1],
                        client=client, host=host, models=models,
                    ),
                    meteo.fetch_hourly_range(
                        year_days[0], year_days[-1],
                        client=client, host=host, models=models,
                    ),
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                reason = f"archive fetch failed for {year}: {exc}"
                result.skipped.extend((d.isoformat(), reason) for d in year_days)
                continue
            times = meteo.parse_times(payload)

            replayed_before = len(result.replayed)
            for d in year_days:
                iso = d.isoformat()
                try:
                    snapshot = pressure.snapshot_at_morning(series, d)
                    meteo_snap = meteo.snapshot_from_range(payload, times, d)
                except RuntimeError as exc:
                    result.skipped.append((iso, str(exc)))
                    continue
                samples = samples_from_record(store.read(iso))
                winds, lake_temp = _project_buoy_day_curve(samples)
                # Anchor staleness checks to the replay day, not the wall
                # clock — same convention as engine.run_replay.
                now = datetime.combine(d, time(12, 0))
                verdicts = apply_rules(snapshot, meteo_snap, winds, lake_temp, now=now)
                forecast = Forecast(
                    overall=aggregate(verdicts),
                    verdicts=verdicts,
                    pressure=snapshot,
                    meteo=meteo_snap,
                    winds=winds,
                    lake_temp=lake_temp,
                    replay_day=d,
                    replay_source=source,
                )
                write_run(forecast, d, store=store)
                result.replayed.append(iso)
            progress(f"{year}: {len(result.replayed) - replayed_before}/{len(year_days)} replayed")

    return result
