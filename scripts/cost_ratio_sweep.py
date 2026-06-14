"""Sweep the missed-session / wasted-drive cost ratio and plot ML vs rule baseline.

The project's `_COST` matrix in `oracle.calibration` bakes in a 2:1 ratio
(MISSED_SESSION_COST = 2 × WASTED_DRIVE_COST) as a guess. That guess is
itself a tunable parameter — a rider who lives 2h from Walchensee might
weight a Schneiderfahrt (windless drive) much closer to 1:1, while a
rider with a season pass whose marginal session is "free" might keep
the 2:1 (or higher). This script makes the trade-off explicit so each
rider can pick their own operating point.

For each ratio r, the script:
  1. Builds a 3x3 cost matrix with MISSED_SESSION_COST = r × WASTED_DRIVE_COST
     (and the off-diagonal half-credit entries from `_COST`).
  2. Computes the optimal Bayes decision rule for each ML model from its
     predicted probabilities — argmin of expected cost per sample.
  3. Computes the rule baseline's mean cost (it can't be re-thresholded;
     its verdicts are what they are). The rule baseline was tuned at r=2,
     so its cost grows as r moves away from 2 in either direction.
  4. Reports the crossover ratio where the rule baseline and each ML
     model tie on mean cost. Below the crossover, ML is cheaper; above
     it, the rule baseline is cheaper.

Usage (assumes the user has already run `oracle ml train` and
`oracle ml evaluate` to produce data/ml/replay_full.pkl):

    uv run python scripts/cost_ratio_sweep.py
    # → writes data/ml/cost_sweep.json + data/ml/cost_sweep.png

The script is intentionally read-only against the run store — it loads
the existing fitted models and re-scores them under different cost
matrices, no retraining. Re-run with new models after re-training.
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display needed
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from oracle.ml.dataset import LABEL_TO_INT, load_replay_csv, split_by_year


# Sweep range. Lower bound < 1.0 to capture the "wasted drive hurts more
# than a missed session" perspective the user raised (Schneiderfahrt
# frustration for a far-drive rider); upper bound > 2.0 to test the
# "rare good day is sacred" perspective (serious local chases every
# thermal). The current default (2.0) sits inside this range.
DEFAULT_RATIOS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0)


def make_cost_matrix(r: float) -> dict[str, dict[str, float]]:
    """3x3 cost matrix with MISSED_SESSION_COST = r × WASTED_DRIVE_COST.

    The off-diagonal half-credit entries (e.g. forecast=GO, actual=MAYBE
    costs 0.5 because the rider still drove but the day was marginal)
    are kept from `calibration._COST` so the sweep stays comparable to
    the spike's main scoring. WASTED_DRIVE_COST is normalised to 1; r
    is the only knob.
    """
    return {
        "go":    {"go": 0.0,            "maybe": 0.5,        "no_go": 1.0},
        "maybe": {"go": 0.5 * r,        "maybe": 0.0,        "no_go": 0.5},
        "no_go": {"go": r,              "maybe": 0.5 * r,    "no_go": 0.0},
    }


def cost_array(cost: dict[str, dict[str, float]]) -> np.ndarray:
    """3x3 dict → 3x3 ndarray in (forecast, actual) row/col order.
    Matches LABEL_ORDER = (go, maybe, no_go) so `cost_arr[pred, true]`
    indexes correctly with int-encoded labels 0/1/2.
    """
    return np.array([
        [cost["go"]["go"],     cost["go"]["maybe"],     cost["go"]["no_go"]],
        [cost["maybe"]["go"],  cost["maybe"]["maybe"],  cost["maybe"]["no_go"]],
        [cost["no_go"]["go"],  cost["no_go"]["maybe"],  cost["no_go"]["no_go"]],
    ])


def expected_cost_per_class(proba: np.ndarray, cost: dict) -> np.ndarray:
    """For each sample, the expected cost of predicting each of the 3 classes.

    E[cost | predict k] = Σ_j P(y=j) × cost(k, j)
    `proba` is (N, 3) in LABEL_ORDER column order; returns (N, 3) where
    entry [i, k] is the expected cost of class k for sample i.
    """
    return proba @ cost_array(cost).T


def optimal_bayes_predictions(proba: np.ndarray, cost: dict) -> np.ndarray:
    """Argmin expected cost per sample. The standard cost-sensitive
    decision rule (Elkan 2001, research doc §3.4)."""
    return np.argmin(expected_cost_per_class(proba, cost), axis=1)


def mean_cost(y_true: np.ndarray, y_pred: np.ndarray, cost: dict) -> float:
    """Average per-sample cost under the given cost matrix. Mirrors
    `oracle.calibration.mean_cost` but parameterised by the cost dict
    so the sweep can pass an alternative ratio without mutating the
    module-level `_COST`."""
    arr = cost_array(cost)
    if len(y_true) == 0:
        return 0.0
    return float(arr[y_pred, y_true].mean())


def find_crossover(ratios: list[float], ml_costs: np.ndarray, rule_costs: np.ndarray) -> float | None:
    """Linear-interpolated ratio where the two cost curves cross. None
    if the curves don't cross in the swept range (one dominates)."""
    diff = ml_costs - rule_costs
    for i in range(len(ratios) - 1):
        if diff[i] * diff[i + 1] < 0:
            t = diff[i] / (diff[i] - diff[i + 1])
            return ratios[i] + t * (ratios[i + 1] - ratios[i])
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--csv", type=Path, default=Path("data/replay_full.csv"),
                   help="Replay CSV (must match the one used to train the models)")
    p.add_argument("--model", type=Path, default=Path("data/ml/replay_full.pkl"),
                   help="Fitted-models pickle written by `oracle ml train --out`")
    p.add_argument("--ratios", type=float, nargs="+", default=DEFAULT_RATIOS,
                   help="Cost ratios to sweep (default: 0.25 to 7.0 in 11 steps)")
    p.add_argument("--table", type=Path, default=Path("data/ml/cost_sweep.json"),
                   help="Output JSON table")
    p.add_argument("--plot", type=Path, default=Path("data/ml/cost_sweep.png"),
                   help="Output PNG plot")
    args = p.parse_args(argv)

    if not args.csv.exists():
        print(f"ERROR: {args.csv} not found. Run `oracle calibrate --csv {args.csv} --replayed` first.", file=sys.stderr)
        return 1
    if not args.model.exists():
        print(f"ERROR: {args.model} not found. Run `oracle ml train --csv {args.csv} --out {args.model}` first.", file=sys.stderr)
        return 1

    # Load the fitted models and recover the split parameters.
    with open(args.model, "rb") as f:
        bundle = pickle.load(f)
    models = bundle["models"]
    label = bundle["label"]
    train_until_year = bundle.get("train_until_year", 2022)
    test_from_year = bundle.get("test_from_year", 2023)

    # Rebuild the same train/test split so the test set is identical
    # to the one the main `oracle ml evaluate` scored.
    label_col = f"actual_verdict_{label}" if label != "peak" else "actual_verdict"
    data = load_replay_csv(args.csv, label_col=label_col)
    split = split_by_year(
        data, train_until_year=train_until_year, test_from_year=test_from_year, calibration_year=None,
    )

    # Rule baseline = `forecast_overall_resimulated` on the test days,
    # falling back to `forecast_overall` for legacy CSVs that predate the
    # rescore column (mirrors `oracle ml evaluate`).
    raw_df = pd.read_csv(args.csv)
    if "forecast_overall_resimulated" in raw_df.columns:
        baseline_col = "forecast_overall_resimulated"
    elif "forecast_overall" in raw_df.columns:
        baseline_col = "forecast_overall"
    else:
        raise SystemExit(
            "replay CSV is missing both 'forecast_overall_resimulated' and "
            "'forecast_overall' — no rule baseline to score against"
        )
    test_days = set(split.test.day.tolist())
    baseline_rows = raw_df[raw_df["day"].isin(test_days)].set_index("day")
    baseline_pred_str = [baseline_rows.loc[d, baseline_col] for d in split.test.day]
    baseline_pred_int = np.array([LABEL_TO_INT[s] for s in baseline_pred_str])

    # Predicted probabilities from each fitted model.
    test_proba = {name: fitted.predict_proba(split.test.X) for name, fitted in models.items()}

    # Sweep.
    ratios = sorted(args.ratios)
    rows = []
    for r in ratios:
        cost = make_cost_matrix(r)
        rule_mc = mean_cost(split.test.y_int, baseline_pred_int, cost)
        rows.append({"ratio": r, "model": "rule", "mean_cost": rule_mc})
        for name, proba in test_proba.items():
            ml_pred_int = optimal_bayes_predictions(proba, cost)
            ml_mc = mean_cost(split.test.y_int, ml_pred_int, cost)
            rows.append({"ratio": r, "model": name, "mean_cost": ml_mc})

    df = pd.DataFrame(rows)
    pivot = df.pivot(index="ratio", columns="model", values="mean_cost")

    # Report.
    n_test = len(baseline_pred_int)
    print(f"Cost sensitivity sweep over {len(ratios)} ratios "
          f"(MISSED_SESSION_COST = r × WASTED_DRIVE_COST, default r=2.0):")
    print(f"  {n_test} test days, year-blocked holdout (train ≤ {train_until_year}, "
          f"test ≥ {test_from_year}).")
    print("  ML models use the optimal Bayes decision rule (argmin expected cost);")
    print("  the rule baseline is a fixed categorical forecast and can't be re-thresholded.")
    print()
    print(pivot.to_string(float_format=lambda x: f"{x:.4f}"))
    print()
    for ml_name in models:
        crossover = find_crossover(ratios, pivot[ml_name].to_numpy(), pivot["rule"].to_numpy())
        ml_low, rule_low = pivot[ml_name].iloc[0], pivot["rule"].iloc[0]
        if crossover is None:
            winner = "ML" if ml_low < rule_low else "rule baseline"
            print(f"  {ml_name:>8s}: no crossover in the swept range — {winner} dominates across the board")
        else:
            below = "ML wins (cheaper than rule)" if ml_low < rule_low else "rule wins"
            above = "rule wins" if ml_low < rule_low else "ML wins"
            print(f"  {ml_name:>8s}: crossover at r ≈ {crossover:.2f}  "
                  f"(r < {crossover:.2f}: {below}; r > {crossover:.2f}: {above})")

    # Persist + plot.
    args.table.parent.mkdir(parents=True, exist_ok=True)
    pivot.to_json(args.table, indent=2)
    print(f"\nTable: {args.table}")

    fig, ax = plt.subplots(figsize=(9, 5.5))
    for col in pivot.columns:
        ax.plot(pivot.index, pivot[col], marker="o", linewidth=1.8, label=col)
    ax.axvline(2.0, color="gray", linestyle="--", alpha=0.5,
               label="current project default (r=2.0)")
    ax.set_xlabel("Cost ratio  r = MISSED_SESSION_COST / WASTED_DRIVE_COST")
    ax.set_ylabel("Mean cost per day (lower is better)")
    ax.set_title(f"Cost sensitivity: rule baseline vs ML  ({n_test} test days)")
    ax.set_xscale("log")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(args.plot, dpi=120)
    print(f"Plot:  {args.plot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
