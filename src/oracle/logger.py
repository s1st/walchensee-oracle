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
from typing import Any, Protocol

import httpx

from oracle.engine import Forecast
from oracle.knowledge.rules import Verdict
from oracle.ml_classifier import classify
from oracle.pillars.measurements import UrfeldSample, fetch_urfeld_day_curve

DEFAULT_RUNS_DIR = Path("data/runs")

# Thresholds used for post-hoc duration metrics only — intentionally separate
# from config.IGNITION_WIND_KNOTS so tuning the forecaster's threshold doesn't
# silently rewrite historical metrics.
_IGNITION_KT = 8.0
_SESSION_KT = 12.0


# --- store protocol --------------------------------------------------------


class RunStore(Protocol):
    """Day-keyed JSON storage with two namespaces: the main records
    (forecasts + ground truth) and the replay records (verdicts re-run
    against the historical archives, kept separate so the calibrate loop
    only sees replays when explicitly asked)."""

    def read(self, iso_day: str) -> dict | None: ...
    def write(self, iso_day: str, data: dict) -> str: ...
    def list_days(self) -> list[str]: ...
    def read_replay(self, iso_day: str) -> dict | None: ...
    def write_replay(self, iso_day: str, data: dict) -> str: ...
    def list_replays(self) -> list[str]: ...


@dataclass
class LocalRunStore:
    directory: Path

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def read(self, iso_day: str) -> dict | None:
        return self._read_path(self.directory / f"{iso_day}.json")

    def write(self, iso_day: str, data: dict) -> str:
        path = self.directory / f"{iso_day}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def list_days(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.json"))

    def read_replay(self, iso_day: str) -> dict | None:
        return self._read_path(self.directory / "replay" / f"{iso_day}.json")

    def write_replay(self, iso_day: str, data: dict) -> str:
        replay_dir = self.directory / "replay"
        replay_dir.mkdir(parents=True, exist_ok=True)
        path = replay_dir / f"{iso_day}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def list_replays(self) -> list[str]:
        return sorted(p.stem for p in (self.directory / "replay").glob("*.json"))

    @staticmethod
    def _read_path(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            return None


@dataclass
class GCSRunStore:
    bucket_name: str

    def __post_init__(self) -> None:
        # Import lazily so CI / local runs don't need the package loaded.
        from google.cloud import storage  # type: ignore[attr-defined]  # stubs not installed

        self._client = storage.Client()
        self._bucket = self._client.bucket(self.bucket_name)

    def _blob(self, iso_day: str):
        return self._bucket.blob(f"runs/{iso_day}.json")

    def _replay_blob(self, iso_day: str):
        return self._bucket.blob(f"runs/replay/{iso_day}.json")

    def read(self, iso_day: str) -> dict | None:
        return self._read_blob(self._blob(iso_day))

    def write(self, iso_day: str, data: dict) -> str:
        self._blob(iso_day).upload_from_string(
            json.dumps(data, ensure_ascii=False, indent=2),
            content_type="application/json",
        )
        return f"gs://{self.bucket_name}/runs/{iso_day}.json"

    def read_replay(self, iso_day: str) -> dict | None:
        return self._read_blob(self._replay_blob(iso_day))

    def write_replay(self, iso_day: str, data: dict) -> str:
        self._replay_blob(iso_day).upload_from_string(
            json.dumps(data, ensure_ascii=False, indent=2),
            content_type="application/json",
        )
        return f"gs://{self.bucket_name}/runs/replay/{iso_day}.json"

    @staticmethod
    def _read_blob(blob) -> dict | None:
        from google.cloud.exceptions import NotFound

        try:
            return json.loads(blob.download_as_text())
        except NotFound:
            return None
        except json.JSONDecodeError:
            return None

    def list_days(self) -> list[str]:
        # Blob names look like "runs/2026-04-22.json"; strip prefix + suffix.
        # Skip any nested sub-prefixes (e.g. `runs/replay/...`) — the
        # forecast list is the project files, replays live separately.
        days: list[str] = []
        for blob in self._client.list_blobs(self._bucket, prefix="runs/"):
            name = blob.name
            if not name.endswith(".json") or not name.startswith("runs/"):
                continue
            stem = name[len("runs/"):-len(".json")]
            if "/" in stem:  # sub-prefix, e.g. "replay/2021-06-15"
                continue
            days.append(stem)
        return sorted(days)

    def list_replays(self) -> list[str]:
        """List the days that have a replay record. Replays live under
        `runs/replay/<date>.json` so the calibrate loop only sees them
        when explicitly asked (`compile_report(replayed=True)`)."""
        days: list[str] = []
        for blob in self._client.list_blobs(self._bucket, prefix="runs/replay/"):
            name = blob.name
            if not name.endswith(".json"):
                continue
            stem = name[len("runs/replay/"):-len(".json")]
            days.append(stem)
        return sorted(days)


def default_store() -> RunStore:
    """Pick a store based on environment — GCS if $RUNS_BUCKET, else local."""
    bucket = os.environ.get("RUNS_BUCKET")
    if bucket:
        return GCSRunStore(bucket)
    return LocalRunStore(DEFAULT_RUNS_DIR)


# --- public API ------------------------------------------------------------


def verdict_to_dict(v: Verdict, *, legacy_reason: bool = False) -> dict:
    """Serialise one Verdict to its stored JSON shape.

    `legacy_reason` adds the English-only `reason` field that pre-i18n readers
    (CLI, old dashboards) still expect. The resimulated verdicts written by the
    calibration tooling omit it — they're internal and post-date bilingual reasons.
    """
    d = {
        "rule": v.rule,
        "signal": v.signal.value,
        "severity": v.severity.value,
        "reason_en": v.reason_en,
        "reason_de": v.reason_de,
    }
    if legacy_reason:
        d["reason"] = v.reason_en
    return d


def forecast_to_dict(result: Forecast, target_day: date) -> dict[str, Any]:
    """Canonical serialisation used by both `--json` output and the log writer.

    Replay records carry `replay_day` + `replay_source` so the calibrate
    loop and the dashboard can distinguish them from live forecasts.
    """
    pressure_d = result.pressure.to_dict()
    meteo_d = result.meteo.to_dict()
    d: dict[str, Any] = {
        "day": target_day.isoformat(),
        "overall": result.overall.value,
        "verdicts": [verdict_to_dict(v, legacy_reason=True) for v in result.verdicts],
        "inputs": {
            "pressure": pressure_d,
            "meteo": meteo_d,
            "measurements": [w.to_dict() for w in result.winds],
            "lake_temp": (
                result.lake_temp.to_dict() if result.lake_temp is not None else None
            ),
        },
    }
    # Shadow ML classifier: experimental, logged + shown alongside the rules,
    # NEVER fed into `overall`. Attached here (not in the engine) so it is
    # structurally incapable of influencing the aggregated verdict. Scores the
    # exact serialised feature values the training CSV was built from.
    ml = classify(pressure_d, meteo_d)
    if ml is not None:
        d["ml_classifier"] = ml.to_dict()
    if result.replay_day is not None:
        d["replay_day"] = result.replay_day.isoformat()
        d["replay_source"] = result.replay_source
    return d


def write_run(
    result: Forecast,
    target_day: date,
    store: RunStore | None = None,
) -> str:
    """Write forecast as <iso>.json, preserving any pre-existing ground_truth.

    Replay records (when `result.replay_day` is set) are written to
    `runs/replay/<iso>.json` instead of the project root, so the
    calibrate loop walks only live forecasts by default. The replay
    record is keyed by `target_day` (the day being replayed) and carries
    a `replay_day` / `replay_source` pair in its body.
    """
    store = store or default_store()
    iso = target_day.isoformat()

    if result.replay_day is not None:
        # Replay path: write to the replay/ namespace; no ground-truth
        # merge because the target day is in the past and ground truth
        # for it is already in the project root (separate concern).
        record = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            **forecast_to_dict(result, target_day),
            "ground_truth": {"machine": None, "human": None},
        }
        return store.write_replay(iso, record)

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


def _round_or_none(value: float | None) -> float | None:
    """Round to 2 dp for the per-sample ground-truth record; preserve None
    so a missing buoy field stays distinguishable from a literal 0.0."""
    return round(value, 2) if value is not None else None


def _machine_ground_truth(samples: list[UrfeldSample]) -> dict:
    if not samples:
        return {"source": "addicted-sports-urfeld", "samples": [], "notes": "no samples"}

    peak_avg = max(samples, key=lambda s: s.avg_knots)
    peak_gust = max(samples, key=lambda s: s.gust_knots)
    above_ignition = [s for s in samples if s.avg_knots >= _IGNITION_KT]
    above_session = [s for s in samples if s.avg_knots >= _SESSION_KT]
    water_temps = [s.water_temp_c for s in samples if s.water_temp_c is not None]
    mean_water_temp = sum(water_temps) / len(water_temps) if water_temps else None

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
        "mean_water_temp_c": (
            round(mean_water_temp, 2) if mean_water_temp is not None else None
        ),
        "samples": [
            {
                "t": s.measured_at.isoformat(),
                "avg_kt": round(s.avg_knots, 2),
                "gust_kt": round(s.gust_knots, 2),
                "water_temp_c": _round_or_none(s.water_temp_c),
                "air_temp_c": _round_or_none(s.air_temp_c),
                "dew_point_c": _round_or_none(s.dew_point_c),
                "rel_humidity_pct": _round_or_none(s.rel_humidity_pct),
                "pressure_hpa": _round_or_none(s.pressure_hpa),
                "rain_mm": _round_or_none(s.rain_mm),
            }
            for s in samples
        ],
    }


