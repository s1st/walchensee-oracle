"""Calibration log — one JSON file per forecast day under `data/runs/`.

Each file stores the raw pillar inputs, the verdict, and a `ground_truth`
block that starts empty and is filled in later — machine-observed values from
`backfill_run`, plus any subjective human notes the user hand-edits.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from oracle.engine import Forecast
from oracle.pillars.measurements import UrfeldSample, fetch_urfeld_day_curve

DEFAULT_RUNS_DIR = Path("data/runs")

# Ignition and "session-worthy" thresholds used for post-hoc duration metrics.
# Kept intentionally separate from config.IGNITION_WIND_KNOTS so changing the
# forecaster's threshold doesn't silently rewrite historical metrics.
_IGNITION_KT = 8.0
_SESSION_KT = 12.0


def forecast_to_dict(result: Forecast, target_day: date) -> dict:
    """Canonical serialisation used by both `--json` output and the log writer."""
    return {
        "day": target_day.isoformat(),
        "overall": result.overall.value,
        "verdicts": [
            {"rule": v.rule, "signal": v.signal.value, "reason": v.reason}
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
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> Path:
    """Write today's forecast to `runs_dir/<iso>.json`, preserving any
    `ground_truth` that a previous run (or backfill) already saved."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{target_day.isoformat()}.json"

    ground_truth = {"machine": None, "human": None}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            ground_truth = existing.get("ground_truth", ground_truth)
        except json.JSONDecodeError:
            pass  # corrupt — overwrite

    record = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        **forecast_to_dict(result, target_day),
        "ground_truth": ground_truth,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_run(target_day: date, runs_dir: Path = DEFAULT_RUNS_DIR) -> dict:
    path = runs_dir / f"{target_day.isoformat()}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No forecast log for {target_day.isoformat()} at {path} — "
            "run `oracle forecast` first so there's something to back-fill."
        )
    return json.loads(path.read_text(encoding="utf-8"))


async def backfill_run(
    target_day: date,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    client: httpx.AsyncClient | None = None,
) -> Path:
    """Pull the full Urfeld curve for `target_day` and merge machine ground
    truth into the existing run file. Leaves `ground_truth.human` untouched."""
    record = load_run(target_day, runs_dir)
    samples = await fetch_urfeld_day_curve(target_day, client=client)
    machine = _machine_ground_truth(samples)

    record.setdefault("ground_truth", {"machine": None, "human": None})
    record["ground_truth"]["machine"] = machine

    path = runs_dir / f"{target_day.isoformat()}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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
        "munich_hpa": p.alpenpumpe_north.hpa,
        "innsbruck_hpa": p.alpenpumpe_south.hpa,
        "bolzano_hpa": p.foehn_south.hpa,
        "alpenpumpe_delta_hpa": round(p.alpenpumpe_delta_hpa, 2),
        "foehn_delta_hpa": round(p.foehn_delta_hpa, 2),
        "measured_at": p.alpenpumpe_north.measured_at.isoformat(),
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
