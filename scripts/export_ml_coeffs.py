"""Re-export the frozen coefficients for the shadow ML classifier.

This is the RETRAIN step for `src/oracle/knowledge/ml_coeffs.py`. It fits the
multinomial logistic regression on the replay calibration CSV and dumps the
floats the pure-Python scorer (`oracle.ml_classifier`) consumes — so the
production images need no sklearn.

Dev-only: requires the `[ml]` extra (`uv pip install -e ".[ml]"`). It does NOT
import `oracle.ml` (that lives on the research branch); feature/label order
are pinned here as literals and asserted against the scorer's expectations.

Usage:
    # default — 11 ICON-stable features, all replay rows (cross-era)
    python scripts/export_ml_coeffs.py --csv data/replay_full.csv

    # 13-feature ICON-only retrain (recommended for the 2026+ production
    # regime; see docs/findings/ml-icon-coverage-shadow-2026-06-15.md)
    python scripts/export_ml_coeffs.py --csv data/replay_full.csv \\
        --feature-set extended --train-filter icon

After running, re-run `pytest tests/test_ml_classifier.py` — the golden test
will flag if the coefficients moved (expected after a genuine retrain; update
the golden if so).
"""
from __future__ import annotations

import argparse
import datetime as _dt


# Feature order MUST match oracle.ml_classifier / the training schema.
STABLE_FEATURES = [
    "munich_hpa", "innsbruck_hpa", "bolzano_hpa", "thermik_delta_hpa", "foehn_delta_hpa",
    "overnight_cloud_cover_pct", "morning_solar_radiation_wm2", "min_dew_point_spread_c",
    "rained_yesterday", "yesterday_precipitation_mm", "max_daytime_low_cloud_pct",
]
# The two ICON-coverage features the 11→13 retrain adds. Both map to
# rules the production layer uses (boundary_layer_height, atmospheric_stability)
# and are 100% populated at the 2026+ production regime.
ICON_COVERAGE_FEATURES = [
    "max_boundary_layer_height_m",
    "max_cape_j_kg",
]
EXTENDED_FEATURES = STABLE_FEATURES + ICON_COVERAGE_FEATURES

LABELS = ["go", "maybe", "no_go"]
TARGET = "actual_verdict_thermal"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/replay_full.csv")
    ap.add_argument("--out", default="src/oracle/knowledge/ml_coeffs.py")
    ap.add_argument(
        "--feature-set", choices=("stable", "extended"), default="stable",
        help="stable=11 ICON-stable features (default); extended=stable + BLH + CAPE "
        "(use this for the 2026+ production regime; see ml-icon-coverage-shadow-2026-06-15.md).",
    )
    ap.add_argument(
        "--train-filter", choices=("all", "icon"), default="all",
        help="all=full replay CSV (default, cross-era); icon=ICON rows only (2022-11-24+; "
        "matches the production regime; eliminates the IFS/ICON distribution shift).",
    )
    args = ap.parse_args()

    import pandas as pd
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    features = STABLE_FEATURES if args.feature_set == "stable" else EXTENDED_FEATURES
    df = pd.read_csv(args.csv)
    if args.train_filter == "icon" and "era" in df.columns:
        df = df[df["era"] == "icon"].copy()
    df = df[df[TARGET].isin(LABELS)].copy()
    if "storm_suspected" in df.columns:
        df = df[~df["storm_suspected"].fillna(False).astype(bool)]

    # Cast booleans to int so the imputer doesn't see mixed dtypes.
    X = df[features].copy()
    if "rained_yesterday" in X.columns and X["rained_yesterday"].dtype == bool:
        X["rained_yesterday"] = X["rained_yesterday"].astype(int)
    X = X.apply(pd.to_numeric, errors="coerce")
    y = df[TARGET].map({lab: i for i, lab in enumerate(LABELS)}).to_numpy()

    pipe = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("sc", StandardScaler()),
        ("clf", LogisticRegression(max_iter=5000, class_weight="balanced", random_state=42)),
    ]).fit(X, y)
    imp, sc, clf = pipe.named_steps["imp"], pipe.named_steps["sc"], pipe.named_steps["clf"]
    assert list(clf.classes_) == [0, 1, 2], "class order drifted from LABELS"

    # Population rate in the training subset, for the export header. The
    # scorer uses median-imputation per-feature; surfacing the train-time
    # population rate makes it visible when a coefficient is mostly
    # learning the median.
    train_pops = {f: float(X[f].notna().mean()) for f in features}

    bundle = {
        "trained_on": f"{args.csv} ({args.train_filter} filter, feature_set={args.feature_set}, n={len(y)})",
        "trained_at": _dt.date.today().isoformat(),
        "n": int(len(y)),
        "feature_set": args.feature_set,
        "train_filter": args.train_filter,
        "train_population": train_pops,
        "median": [float(x) for x in imp.statistics_],
        "mean": [float(x) for x in sc.mean_],
        "scale": [float(x) for x in sc.scale_],
        "coef": [[float(c) for c in row] for row in clf.coef_],
        "intercept": [float(x) for x in clf.intercept_],
    }

    doc_link = (
        "ml-shadow-classifier-design-2026-06-14.md"
        if args.feature_set == "stable"
        else "ml-icon-coverage-shadow-2026-06-15.md"
    )
    lines = [
        '"""Frozen coefficients for the shadow ML classifier.',
        "",
        "AUTO-GENERATED by scripts/export_ml_coeffs.py — do not hand-edit.",
        "Multinomial logistic regression (impute-median -> standardize -> linear),",
        "distilled to pure data so the scorer needs no sklearn/numpy in prod.",
        f"Trained on {bundle['trained_on']}; exported {bundle['trained_at']}.",
        f"See docs/findings/{doc_link}.",
        '"""',
        "from __future__ import annotations",
        "",
        "ML_MODEL: dict = {",
        f'    "trained_on": {bundle["trained_on"]!r},',
        f'    "trained_at": {bundle["trained_at"]!r},',
        f'    "n": {bundle["n"]},',
        f'    "feature_set": {bundle["feature_set"]!r},',
        f'    "train_filter": {bundle["train_filter"]!r},',
        f'    "train_population": {bundle["train_population"]!r},',
        f'    "features": {features!r},',
        f'    "labels": {LABELS!r},',
        f'    "median": {bundle["median"]!r},',
        f'    "mean": {bundle["mean"]!r},',
        f'    "scale": {bundle["scale"]!r},',
        '    "coef": [',
        *(f"        {row!r}," for row in bundle["coef"]),
        "    ],",
        f'    "intercept": {bundle["intercept"]!r},',
        "}",
    ]
    with open(args.out, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {args.out} (n={bundle['n']}, exported {bundle['trained_at']})")
    if args.feature_set == "extended":
        for f, p in train_pops.items():
            tag = " ⚠ sparse" if p < 0.5 else ""
            print(f"  {f}: {p:.1%} populated in train{tag}")


if __name__ == "__main__":
    main()
