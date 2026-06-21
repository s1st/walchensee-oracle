"""HGB sibling of scripts/icon_coverage_experiment.py: would the **HGB** shadow
benefit from the two ICON-coverage features (max_boundary_layer_height_m,
max_cape_j_kg) the bundle currently drops — or does HGB overfit on the small
ICON-only set?

Background: the 11→13 ICON-only retrain (2026-06-15) was applied to the
*logistic* shadow only (it's the one exported to oracle.knowledge.ml_coeffs).
The bundle HGB (replay_full.pkl) is still the 11-feature, cross-era model. This
script asks the open question from the storm-handling finding
(docs/findings/storm-handling-rule-vs-learned-2026-06-21.md): can HGB be trained
on the extended feature set, and is it worth it?

Same data, splits, labels, cost matrix, and rule baseline as the logistic
experiment, so the two are directly comparable. The classifier is swapped for
HistGradientBoostingClassifier with the **production** hyperparameters from
src/oracle/ml/train.fit_hgb (max_iter=200, lr=0.05, min_samples_leaf=20,
class_weight='balanced', early_stopping=False) — not sklearn defaults — so the
numbers reflect the model that would actually ship.

Configs (mirrors the logistic script):
  (a) 11-feature, cross-era. Train IFS ≤2022 → test ICON ≥2023.
  (b) 11-feature, ICON-only. LOYO within ICON (4 folds) + year-blocked (≤2024→≥2025).
  (c) 13-feature, ICON-only. Same splits as (b) + BLH + CAPE.

Overfitting check (the reason this is a separate script, not a flag): HGB is a
high-capacity non-linear model and the ICON-only set is ~715 days. For every fit
the script also scores the model on its OWN training set and reports the
train−test Peirce gap. A large gap that grows from (b)→(c) is the signature of
the extra features buying memorisation, not generalisation.

Dev-only: requires the `[ml]` extra. No prod-image impact — research only.
Output: data/ml/hgb_coverage_experiment.json.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from oracle.calibration import (
    heidke_skill_score,
    mean_cost,
    peirce_skill_score,
)
from oracle.knowledge.rules import SIGNAL_ORDER

# --- schemas (kept in lock-step with scripts/icon_coverage_experiment.py) ---

STABLE_FEATURES: tuple[str, ...] = (
    "munich_hpa", "innsbruck_hpa", "bolzano_hpa",
    "thermik_delta_hpa", "foehn_delta_hpa",
    "overnight_cloud_cover_pct", "morning_solar_radiation_wm2",
    "min_dew_point_spread_c", "rained_yesterday",
    "yesterday_precipitation_mm", "max_daytime_low_cloud_pct",
)
ICON_COVERAGE_FEATURES: tuple[str, ...] = (
    "max_boundary_layer_height_m",
    "max_cape_j_kg",
)
EXTENDED_FEATURES: tuple[str, ...] = STABLE_FEATURES + ICON_COVERAGE_FEATURES

LABELS = ("go", "maybe", "no_go")
TARGET = "actual_verdict_thermal"

# Production HGB hyperparameters — mirror src/oracle/ml/train.fit_hgb exactly so
# the experiment reflects the shippable model, not sklearn defaults.
RANDOM_STATE = 42
MIN_SAMPLES_LEAF = 20

# A column below this training population would force HGB's histogram binner to
# learn from an (almost) all-NaN column — and a 100%-NaN column crashes the
# binner outright. Auto-drop below the bar (and report it).
MIN_POP_RATE = 0.10


@dataclass
class FitResult:
    config: str
    split: str
    train_n: int
    test_n: int
    test_years: str
    features_used: list[str]
    # rule baseline on the same test window
    rule_peirce: float
    rule_hss: float
    rule_accuracy: float
    rule_hard_error: float
    rule_mean_cost: float
    # HGB on the test window
    ml_peirce: float
    ml_hss: float
    ml_accuracy: float
    ml_hard_error: float
    ml_mean_cost: float
    # overfitting diagnostics: same model scored on its own training set
    ml_train_peirce: float
    ml_train_accuracy: float
    overfit_gap_peirce: float    # train − test (higher = more overfit)
    overfit_gap_accuracy: float
    test_nan_rate: dict[str, float]


# --- helpers (identical scoring to the logistic experiment) ----------------


def _confusion(pred: np.ndarray, true: np.ndarray) -> dict[str, dict[str, int]]:
    cm = {f.value: {a.value: 0 for a in SIGNAL_ORDER} for f in SIGNAL_ORDER}
    for p, t in zip(pred, true):
        cm[p][t] += 1
    return cm


def _hard_error_rate(pred: np.ndarray, true: np.ndarray) -> float:
    if len(pred) == 0:
        return 0.0
    hard = sum(1 for p, t in zip(pred, true) if {p, t} == {"go", "no_go"})
    return hard / len(pred)


def _accuracy(pred: np.ndarray, true: np.ndarray) -> float:
    if len(pred) == 0:
        return 0.0
    return float(np.mean([p == t for p, t in zip(pred, true)]))


def _encode(y_str: Iterable[str]) -> np.ndarray:
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


def _fit_and_score(train: pd.DataFrame, test: pd.DataFrame, features: list[str]):
    """Fit the production HGB on `train`, score on `test` AND on `train`.
    Returns a dict of test metrics + train metrics (for the overfit gap) +
    per-feature test NaN rate."""
    X_train = train[list(features)].copy()
    X_test = test[list(features)].copy()
    if "rained_yesterday" in features:
        for X in (X_train, X_test):
            if X["rained_yesterday"].dtype == bool:
                X["rained_yesterday"] = X["rained_yesterday"].astype(int)

    keep: list[str] = []
    nan_rates_test: dict[str, float] = {}
    for c in features:
        nan_rates_test[c] = float(X_test[c].isna().mean())
        if X_train[c].notna().mean() < MIN_POP_RATE:
            continue  # mostly-NaN in train — HGB can't learn it, binner may crash
        keep.append(c)
    if not keep:
        raise RuntimeError("All candidate features below MIN_POP_RATE — nothing to fit.")

    model = HistGradientBoostingClassifier(
        max_iter=200,
        learning_rate=0.05,
        min_samples_leaf=MIN_SAMPLES_LEAF,
        class_weight="balanced",
        random_state=RANDOM_STATE,
        early_stopping=False,
    )
    y_train = _encode(train[TARGET])
    model.fit(X_train[keep], y_train)

    pred_test = _decode(model.predict(X_test[keep]))
    true_test = _decode(_encode(test[TARGET]))
    pred_train = _decode(model.predict(X_train[keep]))
    true_train = _decode(y_train)

    cm_test = _confusion(pred_test, true_test)
    test_peirce = peirce_skill_score(cm_test)
    test_acc = _accuracy(pred_test, true_test)
    cm_train = _confusion(pred_train, true_train)
    train_peirce = peirce_skill_score(cm_train)
    train_acc = _accuracy(pred_train, true_train)

    return {
        "ml_peirce": test_peirce,
        "ml_hss": heidke_skill_score(cm_test),
        "ml_accuracy": test_acc,
        "ml_hard_error": _hard_error_rate(pred_test, true_test),
        "ml_mean_cost": mean_cost(cm_test),
        "ml_train_peirce": train_peirce,
        "ml_train_accuracy": train_acc,
        "overfit_gap_peirce": train_peirce - test_peirce,
        "overfit_gap_accuracy": train_acc - test_acc,
        "test_nan_rate": nan_rates_test,
    }


def _result(config: str, split: str, train: pd.DataFrame, test: pd.DataFrame,
            features: tuple[str, ...], test_years: str,
            rule: tuple[float, float, float, float, float]) -> FitResult:
    ml = _fit_and_score(train, test, list(features))
    return FitResult(
        config=config, split=split, train_n=len(train), test_n=len(test),
        test_years=test_years, features_used=list(features),
        rule_peirce=rule[0], rule_hss=rule[1], rule_accuracy=rule[2],
        rule_hard_error=rule[3], rule_mean_cost=rule[4],
        ml_peirce=ml["ml_peirce"], ml_hss=ml["ml_hss"], ml_accuracy=ml["ml_accuracy"],
        ml_hard_error=ml["ml_hard_error"], ml_mean_cost=ml["ml_mean_cost"],
        ml_train_peirce=ml["ml_train_peirce"], ml_train_accuracy=ml["ml_train_accuracy"],
        overfit_gap_peirce=ml["overfit_gap_peirce"],
        overfit_gap_accuracy=ml["overfit_gap_accuracy"],
        test_nan_rate=ml["test_nan_rate"],
    )


def run_experiment(csv_path: Path) -> list[FitResult]:
    df = pd.read_csv(csv_path)
    df = df[df[TARGET].isin(LABELS)].copy()
    if "storm_suspected" in df.columns:
        df = df[~df["storm_suspected"].fillna(False).astype(bool)].reset_index(drop=True)
    print(f"loaded {len(df)} in-season rows from {csv_path}")
    print(f"  era split: ifs={(df['era']=='ifs').sum()}, icon={(df['era']=='icon').sum()}")
    icon = df[df["era"] == "icon"]
    for c in ICON_COVERAGE_FEATURES:
        if c in df.columns:
            print(f"  {c}: {icon[c].notna().mean():.1%} populated in ICON")

    results: list[FitResult] = []

    # (a) 11-feature cross-era (current bundle HGB regime).
    train_a = _filter(df, era="ifs", year_max=2022)
    test_a = _filter(df, era="icon", year_min=2023)
    results.append(_result(
        "(a) 11-feature cross-era (current bundle HGB)",
        "cross-era holdout (train IFS ≤2022 → test ICON ≥2023)",
        train_a, test_a, STABLE_FEATURES, "2023–2026", _baseline_metrics(test_a),
    ))

    # (b, c) ICON-only LOYO.
    icon_df = _filter(df, era="icon")
    icon_years = sorted(icon_df["year"].unique())
    print(f"  ICON years: {icon_years}")
    for held_out in icon_years:
        train = icon_df[icon_df["year"] != held_out].reset_index(drop=True)
        test = icon_df[icon_df["year"] == held_out].reset_index(drop=True)
        rule = _baseline_metrics(test)
        results.append(_result(
            "(b) 11-feature ICON-only", f"LOYO within ICON, held out {held_out}",
            train, test, STABLE_FEATURES, str(held_out), rule,
        ))
        results.append(_result(
            "(c) 13-feature ICON-only (11 stable + BLH + CAPE)",
            f"LOYO within ICON, held out {held_out}",
            train, test, EXTENDED_FEATURES, str(held_out), rule,
        ))

    # (b, c) year-blocked within ICON: train ≤2024, test ≥2025.
    train_yb = _filter(icon_df, year_max=2024)
    test_yb = _filter(icon_df, year_min=2025)
    rule_yb = _baseline_metrics(test_yb)
    results.append(_result(
        "(b) 11-feature ICON-only", "year-blocked within ICON (train ≤2024, test ≥2025)",
        train_yb, test_yb, STABLE_FEATURES, "2025–2026", rule_yb,
    ))
    results.append(_result(
        "(c) 13-feature ICON-only (11 stable + BLH + CAPE)",
        "year-blocked within ICON (train ≤2024, test ≥2025)",
        train_yb, test_yb, EXTENDED_FEATURES, "2025–2026", rule_yb,
    ))

    return results


def _summary(results: list[FitResult]) -> list[dict]:
    grouped: dict[str, list[FitResult]] = {}
    for r in results:
        if "LOYO" not in r.split:
            continue
        grouped.setdefault(r.config, []).append(r)
    out = []
    for config, fits in grouped.items():
        out.append({
            "config": config,
            "n_folds": len(fits),
            "rule_mean_peirce": float(np.mean([f.rule_peirce for f in fits])),
            "ml_mean_peirce": float(np.mean([f.ml_peirce for f in fits])),
            "ml_minus_rule_peirce": float(np.mean([f.ml_peirce - f.rule_peirce for f in fits])),
            "ml_mean_train_peirce": float(np.mean([f.ml_train_peirce for f in fits])),
            "mean_overfit_gap_peirce": float(np.mean([f.overfit_gap_peirce for f in fits])),
            "ml_mean_cost": float(np.mean([f.ml_mean_cost for f in fits])),
            "rule_mean_cost": float(np.mean([f.rule_mean_cost for f in fits])),
            "ml_minus_rule_cost": float(np.mean([f.ml_mean_cost - f.rule_mean_cost for f in fits])),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/replay_full.csv")
    ap.add_argument("--out", default="data/ml/hgb_coverage_experiment.json")
    args = ap.parse_args()

    results = run_experiment(Path(args.csv))
    payload = {
        "model": "HistGradientBoostingClassifier (production params: max_iter=200, lr=0.05, min_samples_leaf=20, class_weight=balanced, early_stopping=False)",
        "per_fit": [asdict(r) for r in results],
        "loyo_summary": _summary(results),
        "read": {
            "interpretation": [
                "ml_minus_rule_peirce > 0: HGB beats the rule on this split.",
                "(c) − (b) on ml_mean_peirce: does adding BLH+CAPE help HGB out-of-sample?",
                "mean_overfit_gap_peirce (train − test): HGB memorisation. A gap that GROWS from (b) to (c) with no test gain = the extra features overfit.",
                "All metrics share oracle.calibration's cost matrix + skill scores with the logistic experiment, so the two are directly comparable.",
            ],
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=float))
    print(f"wrote {out}")

    print("\n=== LOYO mean (across ICON folds) — test skill + overfit gap ===")
    print(f"{'config':52s}  {'rule PSS':>8s}  {'ML PSS':>8s}  {'Δrule':>7s}  {'trainPSS':>8s}  {'gap':>6s}")
    for s in payload["loyo_summary"]:
        print(
            f"{s['config']:52s}  {s['rule_mean_peirce']:+8.4f}  {s['ml_mean_peirce']:+8.4f}"
            f"  {s['ml_minus_rule_peirce']:+7.4f}  {s['ml_mean_train_peirce']:+8.4f}"
            f"  {s['mean_overfit_gap_peirce']:6.3f}"
        )

    print("\n=== Non-LOYO splits ===")
    for r in results:
        if "LOYO" in r.split:
            continue
        print(
            f"\n{r.config}  ({r.split})"
            f"\n  train={r.train_n} test={r.test_n}  rule PSS={r.rule_peirce:+.4f}  ML PSS={r.ml_peirce:+.4f}  Δ={r.ml_peirce - r.rule_peirce:+.4f}"
            f"\n  overfit: train PSS={r.ml_train_peirce:+.4f}  test PSS={r.ml_peirce:+.4f}  gap={r.overfit_gap_peirce:.3f}"
        )


if __name__ == "__main__":
    main()
