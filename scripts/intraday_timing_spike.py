#!/usr/bin/env python3
"""Stage-1 spike: does *intraday* meteo predict thermal onset time?

Stage-0 (morning maxima) scored Spearman ~0.17 vs onset on 10 y of labels.
This asks whether the intraday shape — when solar crosses ignition levels, when
the low-cloud deck clears, how fast the surface heats — carries the timing
signal the morning box throws away.

For every 2021+ warm-season thermal session (onset label from stored Urfeld
ground truth), fetch the hourly archive, build two feature sets, and compare
them under leave-one-year-out CV (Ridge) at predicting the onset minute:

    gradient-only · stage0 (morning aggregates) · stage1 (intraday) · combined

Reports pooled out-of-fold Spearman + MAE (minutes). Research-only; needs the
[ml] extra and network. Caches payloads under /tmp for re-runs.

    python3 scripts/intraday_timing_spike.py
    python3 scripts/intraday_timing_spike.py --from-year 2021
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import httpx
import numpy as np
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.model_selection import LeaveOneGroupOut
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from oracle.calibration import (
    _ACTUAL_MAYBE_KT,
    _sustained_onset_minute,
    _THERMAL_ONSET_RUN,
    actual_verdict_thermal,
)
from oracle.config import OPEN_METEO_HISTORICAL_FORECAST_URL as HF
from oracle.research.ignition_timing import estimate_from_inputs
from oracle.logger import LocalRunStore
from oracle.pillars import meteo
from oracle.research.intraday_timing import intraday_features

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAGE0 = [
    "thermik_delta_hpa", "min_lifted_index", "max_daytime_low_cloud_pct",
    "morning_solar_radiation_wm2", "max_boundary_layer_height_m",
]
_CACHE = Path("/tmp/intraday_payloads")


def _labelled_days(store: LocalRunStore, from_year: int) -> list[tuple[str, dict, int]]:
    """(iso, record, onset_minute) for warm-season thermal sessions >= from_year."""
    out = []
    for iso in store.list_days():
        if not _DATE.match(iso) or int(iso[:4]) < from_year or int(iso[5:7]) not in range(4, 11):
            continue
        rec = store.read(iso)
        if rec is None:
            continue
        m = (rec.get("ground_truth") or {}).get("machine")
        if actual_verdict_thermal(m) not in ("go", "maybe"):
            continue
        samples = (m or {}).get("samples")
        onset = _sustained_onset_minute(samples, _ACTUAL_MAYBE_KT, _THERMAL_ONSET_RUN) if samples else None
        if onset is not None:
            out.append((iso, rec, onset))
    return out


async def _year_payload(year: int) -> dict:
    _CACHE.mkdir(exist_ok=True)
    cached = _CACHE / f"{year}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    from datetime import date
    end = min(date(year, 10, 31), date.today())
    async with httpx.AsyncClient(timeout=120.0) as client:
        payload = await meteo.fetch_hourly_range(
            date(year, 4, 1), end, client=client, host=HF
        )
    cached.write_text(json.dumps(payload))
    return payload


def _cv(X: np.ndarray, y: np.ndarray, groups: np.ndarray, name: str, model: str = "ridge") -> None:
    if model == "hgb":
        est = HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=300)
        pipe = make_pipeline(est)  # HGB handles NaN natively, no scaling needed
    else:
        pipe = make_pipeline(
            SimpleImputer(strategy="mean", keep_empty_features=True),
            StandardScaler(), Ridge(alpha=1.0),
        )
    logo = LeaveOneGroupOut()
    preds = np.zeros_like(y, dtype=float)
    for tr, te in logo.split(X, y, groups):
        pipe.fit(X[tr], y[tr])
        preds[te] = pipe.predict(X[te])
    rho = spearmanr(preds, y).statistic
    mae = float(np.mean(np.abs(preds - y)))
    print(f"  {name:28} Spearman {rho:+.3f}   MAE {mae:5.1f} min   ({X.shape[1]} feat)")


def _naive_mae(y: np.ndarray, groups: np.ndarray) -> float:
    """Out-of-fold MAE of predicting the train-fold mean onset — the floor any
    model must beat to be worth anything."""
    logo = LeaveOneGroupOut()
    preds = np.zeros_like(y, dtype=float)
    for tr, te in logo.split(y.reshape(-1, 1), y, groups):
        preds[te] = y[tr].mean()
    return float(np.mean(np.abs(preds - y)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-year", type=int, default=2021)
    ap.add_argument("--store", default="data/runs")
    args = ap.parse_args()

    store = LocalRunStore(Path(args.store))
    days = _labelled_days(store, args.from_year)
    print(f"thermal-session labels {args.from_year}+: {len(days)}")

    years = sorted({int(iso[:4]) for iso, _, _ in days})
    payloads = {y: asyncio.run(_year_payload(y)) for y in years}
    times = {y: meteo.parse_times(payloads[y]) for y in years}

    from datetime import date as _date
    rows_s0, rows_s1, ys, grp, heur_scores = [], [], [], [], []
    s1_keys: list[str] | None = None
    skipped = 0
    for iso, rec, onset in days:
        year = int(iso[:4])
        day = _date.fromisoformat(iso)
        feats = intraday_features(payloads[year]["hourly"], times[year], day)
        if feats is None:
            skipped += 1
            continue
        if s1_keys is None:
            s1_keys = sorted(feats)
        # Stage-0 morning aggregates from the SAME fetched payload (the stored
        # main records are ground-truth-only stubs for 2021-25 — their inputs
        # are empty; the real archived inputs live in the replay record). The
        # daily pressure gradient isn't in the meteo payload, so take it from
        # the replay inputs (a pressure-pillar output, archive-sourced).
        try:
            s0 = meteo.snapshot_from_range(payloads[year], times[year], day)
        except RuntimeError:
            skipped += 1
            continue
        rep = store.read_replay(iso) or {}
        grad = ((rep.get("inputs") or {}).get("pressure") or {}).get("thermik_delta_hpa")
        m = {
            "min_lifted_index": s0.min_lifted_index,
            "max_daytime_low_cloud_pct": s0.max_daytime_low_cloud_pct,
            "morning_solar_radiation_wm2": s0.morning_solar_radiation_wm2,
            "max_boundary_layer_height_m": s0.max_boundary_layer_height_m,
        }
        p = {"thermik_delta_hpa": grad}
        rows_s0.append([_get(p, m, k) for k in _STAGE0])
        rows_s1.append([feats.get(k, np.nan) for k in s1_keys])
        ys.append(onset)
        grp.append(year)
        heur_scores.append(estimate_from_inputs(p, m).score if grad is not None else np.nan)

    X0 = np.array(rows_s0, float)
    X1 = np.array(rows_s1, float)
    y = np.array(ys, float)
    groups = np.array(grp)
    print(f"usable rows: {len(y)}  (skipped {skipped} uncovered)   years: {years}")
    print(f"onset: mean {y.mean():.0f} min ({int(y.mean())//60:02d}:{int(y.mean())%60:02d}), "
          f"std {y.std():.0f} min")
    print(f"stage1 features: {s1_keys}\n")

    # Reference: Stage-0 heuristic (non-fitted) on the same days.
    hs = np.array(heur_scores, float)
    ok = ~np.isnan(hs)
    print(f"  {'stage0 HEURISTIC (score)':24} Spearman "
          f"{spearmanr(hs[ok], y[ok]).statistic:+.3f}   (non-fitted reference)\n")

    print(f"  {'naive (predict mean onset)':28} {'':16}   MAE {_naive_mae(y, groups):5.1f} min   "
          f"(floor to beat)\n")
    _cv(X0[:, :1], y, groups, "gradient-only")
    _cv(X0, y, groups, "stage0 aggregates")
    _cv(X1, y, groups, "stage1 intraday")
    Xc = np.hstack([X0, X1])
    _cv(Xc, y, groups, "combined")
    _cv(Xc, y, groups, "combined (HGB, nonlinear)", model="hgb")


def _get(p: dict, m: dict, key: str):
    v = p.get(key) if key == "thermik_delta_hpa" else m.get(key)
    return np.nan if v is None else float(v)


if __name__ == "__main__":
    main()
