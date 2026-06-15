"""Three-way head-to-head: should the shadow ML classifier use the ICON-era
coverage features (max_boundary_layer_height_m, max_cape_j_kg) it currently
drops?

The 11-feature schema in src/oracle/ml/dataset.py drops 8 features to avoid a
train/test distribution shift — the IFS-HRES archive (used 2017-2022) is
70-100% NaN on those columns, while the ICON archive (used 2023+) has more
coverage. The spike's empirical finding was that on the cross-era holdout the
dropped features were not adding signal.

But "cross-era" is a different experiment from "ICON-only". The ICON era now
spans 4 years (2023, 2024, 2025, 2026 in-season), which is enough for a clean
year-blocked holdout *within* ICON. The two features that have any ICON signal
at all — `max_boundary_layer_height_m` (46% populated in ICON) and
`max_cape_j_kg` (15% populated) — map directly to rules the production layer
uses (`boundary_layer_height`, `atmospheric_stability`). Throwing them away
is a structural handicap the 11-feature model is asked to compete under.

This script runs three configs head-to-head on the same replay CSV:

  (a) 11-feature, cross-era (current shadow). Train on IFS (years <= 2022),
      test on ICON (years >= 2023). Reproduces the existing 715-day holdout.
  (b) 11-feature, ICON-only. LOYO within ICON (4 folds) and a year-blocked
      within-ICON holdout (train <= 2024, test >= 2025).
  (c) 13-feature, ICON-only. Same splits as (b) but with BLH and CAPE added.
      The pure-Python scorer in oracle.ml_classifier reads both features from
      the existing `inputs.meteo` dict, so retraining the shadow is a
      feature-list change, not a contract change.

For each fit, the script reports the rule baseline (read straight from the
CSV's `forecast_overall_resimulated` column) on the same test window, so the
ML numbers are head-to-head apples-to-apples.

Per-fit metrics: Peirce (3-class), Heidke, accuracy, hard-error rate, mean
cost (r=2). All scoring reuses oracle.calibration primitives — no parallel
implementations of the cost matrix or skill scores.

Output: data/ml/icon_coverage_experiment.json with per-config per-split
metrics + a head-to-head summary table.

Dev-only: requires the `[ml]` extra (`uv pip install -e ".[ml]"`). The
script does NOT import oracle.ml (the spike module); it uses sklearn
directly. No prod-image impact — this is research infrastructure only.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from oracle.calibration import (
    heidke_skill_score,
    mean_cost,
    peirce_skill_score,
)
from oracle.knowledge.rules import SIGNAL_ORDER


# --- schemas ---------------------------------------------------------------

# The 11 ICON-stable features. Must stay in lock-step with the shadow
# scorer's FEATURES list in scripts/export_ml_coeffs.py — see the
# contract there.
STABLE_FEATURES: tuple[str, ...] = (
    "munich_hpa", "innsbruck_hpa", "bolzano_hpa",
    "thermik_delta_hpa", "foehn_delta_hpa",
    "overnight_cloud_cover_pct", "morning_solar_radiation_wm2",
    "min_dew_point_spread_c", "rained_yesterday",
    "yesterday_precipitation_mm", "max_daytime_low_cloud_pct",
)

# The two ICON-coverage features worth re-testing. Both correspond to rules
# the production layer uses; both have partial ICON population but are 100%
# NaN in the IFS-era train (so cross-era training would be imputation-only
# and unreliable). See ml-icon-coverage-shadow-2026-06-15.md (this commit)
# for the NaN-rate table.
ICON_COVERAGE_FEATURES: tuple[str, ...] = (
    "max_boundary_layer_height_m",
    "max_cape_j_kg",
)
EXTENDED_FEATURES: tuple[str, ...] = STABLE_FEATURES + ICON_COVERAGE_FEATURES

LABELS = ("go", "maybe", "no_go")
TARGET = "actual_verdict_thermal"

# Below this population rate in the training subset, the model is mostly
# learning from median-imputed values — too noisy to trust. CAPE is 15%
# in ICON so it squeaks under the bar; the script reports its pop rate
# per fit for visibility, and would auto-drop it if it fell under the
# threshold.
MIN_POP_RATE = 0.10

RANDOM_STATE = 42


# --- data shapes -----------------------------------------------------------


@dataclass
class FitResult:
    config: str           # "(a) 11-feature cross-era", "(b) 11-feature ICON-only", "(c) 13-feature ICON-only"
    split: str            # "cross-era holdout" | "LOYO fold 2023" | "year-blocked ICON"
    train_n: int
    test_n: int
    test_years: str       # human-readable
    features_used: list[str]
    rule_peirce: float
    rule_hss: float
    rule_accuracy: float
    rule_hard_error: float
    rule_mean_cost: float
    ml_peirce: float
    ml_hss: float
    ml_accuracy: float
    ml_hard_error: float
    ml_mean_cost: float
    # per-feature NaN rate in the test window — flags any test feature
    # that is mostly guesswork, which would inflate the apparent ML
    # benefit (model just learned to ignore it).
    test_nan_rate: dict[str, float]


# --- helpers ---------------------------------------------------------------


def _confusion(pred: np.ndarray, true: np.ndarray) -> dict[str, dict[str, int]]:
    """3x3 integer matrix in (forecast, actual) → count. Mirrors
    calibration._empty_confusion's shape so peirce_skill_score etc. work
    unmodified."""
    cm = {f.value: {a.value: 0 for a in SIGNAL_ORDER} for f in SIGNAL_ORDER}
    for p, t in zip(pred, true):
        cm[p][t] += 1
    return cm


def _hard_error_rate(pred: np.ndarray, true: np.ndarray) -> float:
    """Fraction of predictions where forecast and actual are at opposite
    extremes (GO ↔ NO_GO). MAYBE↔anything is a soft error; GO↔NO_GO is the
    hard one that the cost matrix penalises at full weight."""
    if len(pred) == 0:
        return 0.0
    hard = sum(1 for p, t in zip(pred, true) if {p, t} == {"go", "no_go"})
    return hard / len(pred)


def _accuracy(pred: np.ndarray, true: np.ndarray) -> float:
    if len(pred) == 0:
        return 0.0
    return float(np.mean([p == t for p, t in zip(pred, true)]))


def _encode(y_str: Iterable[str]) -> np.ndarray:
    """String labels → int in LABEL_ORDER. Mirrors dataset.encode_labels
    without the import (this script doesn't depend on oracle.ml to keep
    the [ml] extra lean)."""
    table = {lab: i for i, lab in enumerate(LABELS)}
    return np.asarray([table[str(v)] for v in y_str], dtype=np.int64)


def _decode(y_int: np.ndarray) -> list[str]:
    return [LABELS[int(v)] for v in y_int]


def _filter(df: pd.DataFrame, era: str | None = None,
            year_min: int | None = None, year_max: int | None = None) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if era is not None and "era" in df.columns:
        mask &= df["era"] == era
    if year_min is not None:
        mask &= df["year"] >= year_min
    if year_max is not None:
        mask &= df["year"] <= year_max
    return df[mask].reset_index(drop=True)


def _baseline_metrics(df_test: pd.DataFrame) -> tuple[float, float, float, float, float]:
    """Score the rule baseline's resimulated verdicts on the test window.
    Returns (peirce, hss, accuracy, hard_error, mean_cost).
    """
    pred = df_test["forecast_overall_resimulated"].to_numpy()
    true = df_test[TARGET].to_numpy()
    cm = _confusion(pred, true)
    return (
        peirce_skill_score(cm),
        heidke_skill_score(cm),
        _accuracy(pred, true),
        _hard_error_rate(pred, true),
        mean_cost(cm),
    )


def _fit_and_score(train: pd.DataFrame, test: pd.DataFrame, features: list[str]
                   ) -> tuple[float, float, float, float, float, dict[str, float]]:
    """Fit the standard shadow pipeline on `train`, score on `test`.
    Returns (peirce, hss, accuracy, hard_error, mean_cost, test_nan_rate).
    """
    # Cast booleans to int so the imputer doesn't see mixed dtypes.
    X_train = train[list(features)].copy()
    X_test = test[list(features)].copy()
    if "rained_yesterday" in features:
        for X in (X_train, X_test):
            if X["rained_yesterday"].dtype == bool:
                X["rained_yesterday"] = X["rained_yesterday"].astype(int)

    # Auto-drop any column whose training-subset population is below
    # MIN_POP_RATE — the model can't learn from a column that's mostly
    # the median, and HGB's hist binner crashes on 100% NaN columns.
    keep = []
    nan_rates_test: dict[str, float] = {}
    for c in features:
        train_pop = X_train[c].notna().mean()
        if train_pop < MIN_POP_RATE:
            # Surface what we dropped so the report is honest.
            nan_rates_test[c] = float(X_test[c].isna().mean())
            continue
        keep.append(c)
        nan_rates_test[c] = float(X_test[c].isna().mean())
    if not keep:
        raise RuntimeError("All candidate features are below MIN_POP_RATE — nothing to fit.")

    pipe = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("sc", StandardScaler()),
        ("clf", LogisticRegression(max_iter=5000, class_weight="balanced", random_state=RANDOM_STATE)),
    ])
    y_train = _encode(train[TARGET])
    pipe.fit(X_train[keep], y_train)
    y_test = _encode(test[TARGET])
    y_pred = pipe.predict(X_test[keep])

    cm = _confusion(_decode(y_pred), _decode(y_test))
    return (
        peirce_skill_score(cm),
        heidke_skill_score(cm),
        _accuracy(_decode(y_pred), _decode(y_test)),
        _hard_error_rate(_decode(y_pred), _decode(y_test)),
        mean_cost(cm),
        nan_rates_test,
    )


# --- experiment driver -----------------------------------------------------


def run_experiment(csv_path: Path) -> list[FitResult]:
    df = pd.read_csv(csv_path)
    df = df[df[TARGET].isin(LABELS)].copy()
    if "storm_suspected" in df.columns:
        df = df[~df["storm_suspected"].fillna(False).astype(bool)].reset_index(drop=True)
    print(f"loaded {len(df)} in-season rows from {csv_path}")
    print(f"  era split: ifs={(df['era']=='ifs').sum()}, icon={(df['era']=='icon').sum()}")
    print(f"  year range: {df['year'].min()}–{df['year'].max()}")
    print("  ICON coverage features in ICON era:")
    icon = df[df["era"] == "icon"]
    for c in ICON_COVERAGE_FEATURES:
        if c in df.columns:
            pop = icon[c].notna().mean()
            print(f"    {c}: {pop:.1%} populated in ICON")

    results: list[FitResult] = []

    # ---- (a) 11-feature cross-era -----------------------------------------
    # Train on IFS (years <= 2022), test on ICON (years >= 2023). This
    # reproduces the existing 715-day holdout from the spike.
    train_a = _filter(df, era="ifs", year_max=2022)
    test_a = _filter(df, era="icon", year_min=2023)
    rule_a = _baseline_metrics(test_a)
    ml_a = _fit_and_score(train_a, test_a, list(STABLE_FEATURES))
    results.append(FitResult(
        config="(a) 11-feature cross-era (current shadow)",
        split="cross-era holdout (train IFS ≤2022 → test ICON ≥2023)",
        train_n=len(train_a),
        test_n=len(test_a),
        test_years="2023–2026",
        features_used=list(STABLE_FEATURES),
        rule_peirce=rule_a[0], rule_hss=rule_a[1], rule_accuracy=rule_a[2],
        rule_hard_error=rule_a[3], rule_mean_cost=rule_a[4],
        ml_peirce=ml_a[0], ml_hss=ml_a[1], ml_accuracy=ml_a[2],
        ml_hard_error=ml_a[3], ml_mean_cost=ml_a[4],
        test_nan_rate=ml_a[5],
    ))

    # ---- (b, c) ICON-only LOYO + year-blocked ----------------------------
    icon_df = _filter(df, era="icon")
    icon_years = sorted(icon_df["year"].unique())
    print(f"  ICON years: {icon_years}")

    for held_out in icon_years:
        train = icon_df[icon_df["year"] != held_out]
        test = icon_df[icon_df["year"] == held_out]
        # 11-feature ICON-only LOYO fold.
        rule_b = _baseline_metrics(test)
        ml_b = _fit_and_score(train, test, list(STABLE_FEATURES))
        results.append(FitResult(
            config="(b) 11-feature ICON-only",
            split=f"LOYO within ICON, held out {held_out}",
            train_n=len(train), test_n=len(test),
            test_years=str(held_out),
            features_used=list(STABLE_FEATURES),
            rule_peirce=rule_b[0], rule_hss=rule_b[1], rule_accuracy=rule_b[2],
            rule_hard_error=rule_b[3], rule_mean_cost=rule_b[4],
            ml_peirce=ml_b[0], ml_hss=ml_b[1], ml_accuracy=ml_b[2],
            ml_hard_error=ml_b[3], ml_mean_cost=ml_b[4],
            test_nan_rate=ml_b[5],
        ))
        # 13-feature ICON-only LOYO fold.
        ml_c = _fit_and_score(train, test, list(EXTENDED_FEATURES))
        results.append(FitResult(
            config="(c) 13-feature ICON-only (11 stable + BLH + CAPE)",
            split=f"LOYO within ICON, held out {held_out}",
            train_n=len(train), test_n=len(test),
            test_years=str(held_out),
            features_used=list(EXTENDED_FEATURES),
            rule_peirce=rule_b[0], rule_hss=rule_b[1], rule_accuracy=rule_b[2],
            rule_hard_error=rule_b[3], rule_mean_cost=rule_b[4],
            ml_peirce=ml_c[0], ml_hss=ml_c[1], ml_accuracy=ml_c[2],
            ml_hard_error=ml_c[3], ml_mean_cost=ml_c[4],
            test_nan_rate=ml_c[5],
        ))

    # Year-blocked within ICON: train <= 2024, test >= 2025 (the closest
    # analogue to the current cross-era 715-day holdout, but with the
    # distribution shift removed).
    train_yb = _filter(icon_df, year_max=2024)
    test_yb = _filter(icon_df, year_min=2025)
    rule_yb = _baseline_metrics(test_yb)
    ml_b_yb = _fit_and_score(train_yb, test_yb, list(STABLE_FEATURES))
    results.append(FitResult(
        config="(b) 11-feature ICON-only",
        split="year-blocked within ICON (train ≤2024, test ≥2025)",
        train_n=len(train_yb), test_n=len(test_yb),
        test_years="2025–2026",
        features_used=list(STABLE_FEATURES),
        rule_peirce=rule_yb[0], rule_hss=rule_yb[1], rule_accuracy=rule_yb[2],
        rule_hard_error=rule_yb[3], rule_mean_cost=rule_yb[4],
        ml_peirce=ml_b_yb[0], ml_hss=ml_b_yb[1], ml_accuracy=ml_b_yb[2],
        ml_hard_error=ml_b_yb[3], ml_mean_cost=ml_b_yb[4],
        test_nan_rate=ml_b_yb[5],
    ))
    ml_c_yb = _fit_and_score(train_yb, test_yb, list(EXTENDED_FEATURES))
    results.append(FitResult(
        config="(c) 13-feature ICON-only (11 stable + BLH + CAPE)",
        split="year-blocked within ICON (train ≤2024, test ≥2025)",
        train_n=len(train_yb), test_n=len(test_yb),
        test_years="2025–2026",
        features_used=list(EXTENDED_FEATURES),
        rule_peirce=rule_yb[0], rule_hss=rule_yb[1], rule_accuracy=rule_yb[2],
        rule_hard_error=rule_yb[3], rule_mean_cost=rule_yb[4],
        ml_peirce=ml_c_yb[0], ml_hss=ml_c_yb[1], ml_accuracy=ml_c_yb[2],
        ml_hard_error=ml_c_yb[3], ml_mean_cost=ml_c_yb[4],
        test_nan_rate=ml_c_yb[5],
    ))

    # Year-blocked (d) — 11-feature cross-era on the SAME 2025+2026 test
    # window. Trains on the IFS era (years <= 2022) the production shadow
    # was originally fit on, evaluated on the ICON era's 2025-2026 days so
    # the head-to-head vs (b) and (c) is on the same test set with the
    # same labels and cost matrix. This is the apples-to-apples answer to
    # "is ICON-only training better than the cross-era baseline, all else
    # equal?"
    train_d = _filter(df, era="ifs", year_max=2022)
    test_d = _filter(df, era="icon", year_min=2025)
    rule_d = _baseline_metrics(test_d)
    ml_d = _fit_and_score(train_d, test_d, list(STABLE_FEATURES))
    results.append(FitResult(
        config="(d) 11-feature cross-era (test on 2025–2026)",
        split="year-blocked (train IFS ≤2022, test ICON 2025–2026)",
        train_n=len(train_d), test_n=len(test_d),
        test_years="2025–2026",
        features_used=list(STABLE_FEATURES),
        rule_peirce=rule_d[0], rule_hss=rule_d[1], rule_accuracy=rule_d[2],
        rule_hard_error=rule_d[3], rule_mean_cost=rule_d[4],
        ml_peirce=ml_d[0], ml_hss=ml_d[1], ml_accuracy=ml_d[2],
        ml_hard_error=ml_d[3], ml_mean_cost=ml_d[4],
        test_nan_rate=ml_d[5],
    ))

    return results


def _summary_table(results: list[FitResult]) -> list[dict]:
    """Group LOYO folds by config and report mean Peirce / HSS / cost
    across the 4 folds, so the user can read 'on average across ICON
    years' without scanning the per-fold rows."""
    grouped: dict[str, list[FitResult]] = {}
    for r in results:
        if "LOYO" not in r.split:
            continue
        grouped.setdefault(r.config, []).append(r)

    summary = []
    for config, fits in grouped.items():
        n = len(fits)
        summary.append({
            "config": config,
            "n_folds": n,
            "rule_mean_peirce": float(np.mean([f.rule_peirce for f in fits])),
            "ml_mean_peirce": float(np.mean([f.ml_peirce for f in fits])),
            "ml_minus_rule_peirce": float(np.mean([f.ml_peirce - f.rule_peirce for f in fits])),
            "ml_mean_hss": float(np.mean([f.ml_hss for f in fits])),
            "ml_mean_accuracy": float(np.mean([f.ml_accuracy for f in fits])),
            "ml_mean_cost": float(np.mean([f.ml_mean_cost for f in fits])),
            "rule_mean_cost": float(np.mean([f.rule_mean_cost for f in fits])),
            "ml_minus_rule_cost": float(np.mean([f.ml_mean_cost - f.rule_mean_cost for f in fits])),
        })
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/replay_full.csv")
    ap.add_argument("--out", default="data/ml/icon_coverage_experiment.json")
    args = ap.parse_args()

    results = run_experiment(Path(args.csv))
    payload = {
        "config": {
            "a": "11-feature, cross-era train (IFS ≤2022) + cross-era test (ICON ≥2023). The current production shadow schema.",
            "b": "11-feature, ICON-only. Year-blocked within ICON (train ≤2024, test ≥2025) and LOYO within ICON (4 folds: 2023/2024/2025/2026).",
            "c": "13-feature, ICON-only. Same splits as (b) but with the two ICON-coverage features (max_boundary_layer_height_m, max_cape_j_kg) added.",
            "rule_baseline": "forecast_overall_resimulated column from the replay CSV; same cost matrix (MISSED_SESSION_COST=2, WASTED_DRIVE_COST=1) and Peirce/HSS implementations as oracle.calibration.",
        },
        "per_fit": [asdict(r) for r in results],
        "loyo_summary": _summary_table(results),
        "read": {
            "interpretation": [
                "Peirce (PSS) is the headline skill score (3-class, base-rate-unbiased; 0 for any constant).",
                "Heidke (HSS) is the same family, alternative denominator (chance-corrected accuracy).",
                "Hard-error rate: fraction of predictions where forecast and actual are at opposite extremes (GO ↔ NO_GO). MAYBE↔anything is a soft error.",
                "Mean cost: per-day cost under the missed-session (×2) / wasted-drive (×1) matrix; lower is better.",
                "test_nan_rate: per-feature NaN rate in the test window. A high rate means the model's prediction for that column is mostly the median, so apparent signal may be imputation noise.",
            ],
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=float))
    print(f"wrote {out}")

    # Print a readable summary.
    print("\n=== LOYO mean (across 4 ICON folds) ===")
    print(f"{'config':40s}  {'rule PSS':>9s}  {'ML PSS':>9s}  {'Δ PSS':>7s}  {'ML cost':>9s}  {'rule cost':>9s}  {'Δ cost':>7s}")
    for s in payload["loyo_summary"]:
        print(
            f"{s['config']:40s}  {s['rule_mean_peirce']:+9.4f}  {s['ml_mean_peirce']:+9.4f}"
            f"  {s['ml_minus_rule_peirce']:+7.4f}  {s['ml_mean_cost']:9.4f}  {s['rule_mean_cost']:9.4f}"
            f"  {s['ml_minus_rule_cost']:+7.4f}"
        )

    print("\n=== Cross-era holdout (a) and within-ICON year-blocked (b, c) ===")
    for r in results:
        if "LOYO" in r.split:
            continue
        print(
            f"\n{r.config}  ({r.split})"
            f"\n  train={r.train_n} test={r.test_n}  rule PSS={r.rule_peirce:+.4f}  ML PSS={r.ml_peirce:+.4f}  Δ={r.ml_peirce - r.rule_peirce:+.4f}"
            f"\n  rule cost={r.rule_mean_cost:.4f}  ML cost={r.ml_mean_cost:.4f}  Δ={r.ml_mean_cost - r.rule_mean_cost:+.4f}"
        )


if __name__ == "__main__":
    main()
