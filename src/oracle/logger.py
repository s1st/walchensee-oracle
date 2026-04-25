"""Calibration log — per-day JSON records of forecast + ground truth.

Backends are pluggable via the `RunStore` protocol. Two implementations:

- **Local**: files under `data/runs/<iso>.json` (default, used for development).
- **GCS**: objects under `gs://<bucket>/runs/<iso>.json` (used in Cloud Run
  deployments). Activated when `RUNS_BUCKET` is set in the environment.

Each record keeps the raw pillar inputs, the verdict, and a `ground_truth`
block. `ground_truth.machine` is filled automatically by `backfill_run` from
the Urfeld anemometer; `ground_truth.human` stays for hand-edited notes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

from oracle.engine import Forecast
from oracle.pillars.measurements import UrfeldSample, fetch_urfeld_day_curve

DEFAULT_RUNS_DIR = Path("data/runs")

# Thresholds used for post-hoc duration metrics only — intentionally separate
# from config.IGNITION_WIND_KNOTS so tuning the forecaster's threshold doesn't
# silently rewrite historical metrics.
_IGNITION_KT = 8.0
_SESSION_KT = 12.0


# --- store protocol --------------------------------------------------------


class RunStore(Protocol):
    def read(self, iso_day: str) -> dict | None: ...
    def write(self, iso_day: str, data: dict) -> str: ...
    def list_days(self) -> list[str]: ...


@dataclass
class LocalRunStore:
    directory: Path

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def read(self, iso_day: str) -> dict | None:
        path = self.directory / f"{iso_day}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def write(self, iso_day: str, data: dict) -> str:
        path = self.directory / f"{iso_day}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def list_days(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.json"))


@dataclass
class GCSRunStore:
    bucket_name: str

    def __post_init__(self) -> None:
        # Import lazily so CI / local runs don't need the package loaded.
        from google.cloud import storage

        self._client = storage.Client()
        self._bucket = self._client.bucket(self.bucket_name)

    def _blob(self, iso_day: str):
        return self._bucket.blob(f"runs/{iso_day}.json")

    def read(self, iso_day: str) -> dict | None:
        blob = self._blob(iso_day)
        if not blob.exists():
            return None
        try:
            return json.loads(blob.download_as_text())
        except json.JSONDecodeError:
            return None

    def write(self, iso_day: str, data: dict) -> str:
        blob = self._blob(iso_day)
        blob.upload_from_string(
            json.dumps(data, ensure_ascii=False, indent=2),
            content_type="application/json",
        )
        return f"gs://{self.bucket_name}/runs/{iso_day}.json"

    def list_days(self) -> list[str]:
        # Blob names look like "runs/2026-04-22.json"; strip prefix + suffix.
        days: list[str] = []
        for blob in self._client.list_blobs(self._bucket, prefix="runs/"):
            name = blob.name
            if name.endswith(".json") and name.startswith("runs/"):
                days.append(name[len("runs/"):-len(".json")])
        return sorted(days)


def default_store() -> RunStore:
    """Pick a store based on environment — GCS if $RUNS_BUCKET, else local."""
    bucket = os.environ.get("RUNS_BUCKET")
    if bucket:
        return GCSRunStore(bucket)
    return LocalRunStore(DEFAULT_RUNS_DIR)


# --- public API ------------------------------------------------------------


def forecast_to_dict(result: Forecast, target_day: date) -> dict:
    """Canonical serialisation used by both `--json` output and the log writer."""
    return {
        "day": target_day.isoformat(),
        "overall": result.overall.value,
        "verdicts": [
            {
                "rule": v.rule,
                "signal": v.signal.value,
                "severity": v.severity.value,
                "reason": v.reason_en,        # legacy field — English, what pre-i18n readers expect
                "reason_en": v.reason_en,
                "reason_de": v.reason_de,
            }
            for v in result.verdicts
        ],
        "inputs": {
            "pressure": _pressure_dict(result),
            "meteo": _meteo_dict(result),
            "measurements": _measurements_list(result),
        },
        "chat_messages": [
            {
                "posted_at": m.posted_at.isoformat(),
                "author": m.author,
                "channel": m.channel,
                "text": m.text,
            }
            for m in result.chat_messages
        ],
    }


def write_run(
    result: Forecast,
    target_day: date,
    store: RunStore | None = None,
) -> str:
    """Write forecast as <iso>.json, preserving any pre-existing ground_truth."""
    store = store or default_store()
    iso = target_day.isoformat()

    ground_truth = {"machine": None, "human": None}
    existing = store.read(iso)
    if existing is not None:
        ground_truth = existing.get("ground_truth", ground_truth)

    record = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        **forecast_to_dict(result, target_day),
        "ground_truth": ground_truth,
    }
    return store.write(iso, record)


def load_run(target_day: date, store: RunStore | None = None) -> dict:
    store = store or default_store()
    iso = target_day.isoformat()
    data = store.read(iso)
    if data is None:
        raise FileNotFoundError(
            f"No forecast log for {iso} — run `oracle forecast` first so there's "
            "something to back-fill."
        )
    return data


async def backfill_run(
    target_day: date,
    store: RunStore | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Pull Urfeld's full-day curve and merge machine ground truth into the
    existing run record. Leaves ground_truth.human untouched."""
    store = store or default_store()
    iso = target_day.isoformat()
    record = load_run(target_day, store=store)
    samples = await fetch_urfeld_day_curve(target_day, client=client)

    record.setdefault("ground_truth", {"machine": None, "human": None})
    record["ground_truth"]["machine"] = _machine_ground_truth(samples)
    return store.write(iso, record)


# --- internals -------------------------------------------------------------


def _machine_ground_truth(samples: list[UrfeldSample]) -> dict:
    if not samples:
        return {"source": "addicted-sports-urfeld", "samples": [], "notes": "no samples"}

    peak_avg = max(samples, key=lambda s: s.avg_knots)
    peak_gust = max(samples, key=lambda s: s.gust_knots)
    above_ignition = [s for s in samples if s.avg_knots >= _IGNITION_KT]
    above_session = [s for s in samples if s.avg_knots >= _SESSION_KT]

    return {
        "source": "addicted-sports-urfeld",
        "sample_count": len(samples),
        "peak_avg_knots": round(peak_avg.avg_knots, 2),
        "peak_avg_at": peak_avg.measured_at.isoformat(),
        "peak_gust_knots": round(peak_gust.gust_knots, 2),
        "peak_gust_at": peak_gust.measured_at.isoformat(),
        "first_ignition_at": (
            above_ignition[0].measured_at.isoformat() if above_ignition else None
        ),
        "samples_above_8kt": len(above_ignition),
        "samples_above_12kt": len(above_session),
        "samples": [
            {
                "t": s.measured_at.isoformat(),
                "avg_kt": round(s.avg_knots, 2),
                "gust_kt": round(s.gust_knots, 2),
            }
            for s in samples
        ],
    }


def _pressure_dict(result: Forecast) -> dict | None:
    if result.pressure is None:
        return None
    p = result.pressure
    return {
        "munich_hpa": p.thermik_north.hpa,
        "innsbruck_hpa": p.thermik_south.hpa,
        "bolzano_hpa": p.foehn_south.hpa,
        "thermik_delta_hpa": round(p.thermik_delta_hpa, 2),
        "foehn_delta_hpa": round(p.foehn_delta_hpa, 2),
        "measured_at": p.thermik_north.measured_at.isoformat(),
    }


def _meteo_dict(result: Forecast) -> dict | None:
    if result.meteo is None:
        return None
    m = result.meteo
    return {
        "day": m.day.isoformat(),
        "overnight_cloud_cover_pct": m.overnight_cloud_cover_pct,
        "morning_solar_radiation_wm2": m.morning_solar_radiation_wm2,
        "synoptic_wind_knots": m.synoptic_wind_knots,
        "min_dew_point_spread_c": m.min_dew_point_spread_c,
        "max_boundary_layer_height_m": m.max_boundary_layer_height_m,
        "soil_moisture_m3m3": m.soil_moisture_m3m3,
        "rained_yesterday": m.rained_yesterday,
        "yesterday_precipitation_mm": m.yesterday_precipitation_mm,
        "max_lifted_index": m.max_lifted_index,
        "min_lifted_index": m.min_lifted_index,
        "max_cape_j_kg": m.max_cape_j_kg,
        "max_daytime_low_cloud_pct": m.max_daytime_low_cloud_pct,
        "wind_850_direction_at_peak_deg": m.wind_850_direction_at_peak_deg,
        "max_wind_700_knots": m.max_wind_700_knots,
    }


def _measurements_list(result: Forecast) -> list[dict]:
    return [
        {
            "station": r.station,
            "role": r.role.value,
            "avg_knots": round(r.avg_knots, 2),
            "gust_knots": round(r.gust_knots, 2),
            "direction_deg": r.direction_deg,
            "measured_at": r.measured_at.isoformat(),
        }
        for r in result.winds
    ]
