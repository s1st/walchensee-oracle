"""Replay-CSV loader and year-blocked splitter for the ML ceiling spike.

The replay CSV is produced by `oracle calibrate --csv <path> --replayed` and
has 34 columns (see `_CSV_COLUMNS` in `oracle.calibration`). This module
defines which columns are *features*, which are *target*, and how to split
the rows into train / calibration / test blocks by year for the
year-blocked holdout protocol (research doc §3.6 + §5: train ≤ 2022, test
≥ 2023, calibration = 2022 alone).

The era indicator (`era` column, "ifs" or "icon") is carried through as
metadata but **not** fed into the model. Per the research doc §3.8: the
model should generalise across the era boundary, not depend on it.
`storm_suspected` is treated as a *quarantine* (rows excluded from
training) rather than a feature, mirroring the rule baseline's behavior
in `compile_report` (gust-front wind isn't a thermal session).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from oracle.knowledge.rules import SIGNAL_ORDER, Signal

if TYPE_CHECKING:  # pandas is only needed to *load* CSVs / train — see load_replay_csv.
    import pandas as pd  # type: ignore[import-untyped]
# NOTE: pandas is imported lazily (inside load_replay_csv), NOT at module level,
# so that unpickling the model bundle — which imports oracle.ml.train →
# oracle.ml.dataset — works in the lean [hgb] prod image that ships no pandas.
# Module-level annotations stay valid as strings via `from __future__ import
# annotations`. See docs/findings/stats-panel-season-scoping-2026-06-21.md.


# --- feature / target / metadata column maps -----------------------------
# Single source of truth for "which columns does the model see?" Update
# here when extending the schema, and the train/evaluate pipelines pick
# it up automatically.

# Pressure pillar — 5 features. The deltas are the model's actual signal;
# the absolute pressures are kept for completeness but are collinear with
# the deltas (Munich - Innsbruck = thermik_delta, etc.). HGB won't split
# on the redundant ones, but keeping them makes the schema inspectable.
# All 5 are ICON-stable: IFS HRES exposes MSL pressure at the same
# Munich / Innsbruck / Bolzano stations across both eras.
PRESSURE_COLS: tuple[str, ...] = (
    "munich_hpa", "innsbruck_hpa", "bolzano_hpa",
    "thermik_delta_hpa", "foehn_delta_hpa",
)

# Meteo pillar — 6 ICON-stable features. Deliberately excludes the
# 8 ICON-era-only signals (`synoptic_wind_knots`, `max_boundary_layer_height_m`,
# `soil_moisture_m3m3`, `max_lifted_index`, `min_lifted_index`, `max_cape_j_kg`,
# `wind_850_direction_at_peak_deg`, `max_wind_700_knots`) that the
# Open-Meteo archive only exposes from 2022-11-24 onward. Including them
# in the model would mean training on IFS-era rows where they're 70-100%
# NaN and testing on ICON-era rows where they have real values — a
# distribution shift that conflates "the model learned the right
# patterns" with "the model learned to use new ICON features." The
# research doc §3.8 calls this out as a known confound; the user
# flagged it during review ("are we sure this is a good split?") and
# chose the cleaner alternative: restrict the model to features
# measured in BOTH eras. Cost: 8 potentially-useful features are
# dropped. Benefit: train and test have the same feature distribution;
# the test Peirce is a clean measure of discrimination on a stable
# feature set, not a mixture of discrimination + era generalisation.
# Schema: 6 ICON-stable meteo features used by the model.
METEO_COLS: tuple[str, ...] = (
    "overnight_cloud_cover_pct",
    "morning_solar_radiation_wm2",
    "min_dew_point_spread_c",
    "rained_yesterday",          # bool → cast to int
    "yesterday_precipitation_mm",
    "max_daytime_low_cloud_pct",
)

# Valid target columns on the replay CSV. All three are ground-truth
# scales: the ML spike defaults to `actual_verdict_thermal` (the
# decontaminated label) per the research doc + handoff.
TARGET_COLS: tuple[str, ...] = (
    "actual_verdict",
    "actual_verdict_duration",
    "actual_verdict_thermal",
)

# Columns the dataset loader never feeds into the model. Listed here so
# the rule "what's a feature?" is explicit, not implicit.
EXCLUDED_COLS: frozenset[str] = frozenset({
    "day",                              # ID, not a signal
    "month", "year",                    # used for splitting, not features
    "era",                              # carried as metadata, not a feature
    "storm_suspected",                  # quarantine, not a feature
    "forecast_overall",                 # rule baseline's verdict — for evaluation, not features
    "forecast_overall_resimulated",     # same
    "peak_avg_knots", "peak_gust_knots",     # ground truth — would be label leakage
    "first_ignition_minute",
    "samples_above_8kt", "samples_above_12kt",
} | set(TARGET_COLS))

FEATURE_COLS: tuple[str, ...] = tuple(c for c in PRESSURE_COLS + METEO_COLS if c not in EXCLUDED_COLS)


# --- label encoding -------------------------------------------------------
# Match the rule baseline's SIGNAL_ORDER (GO, MAYBE, NO_GO) so the ML
# model's confusion matrix aligns with `compile_report`'s and the
# cost-matrix from `calibration._COST` applies unchanged.
LABEL_ORDER: tuple[str, ...] = tuple(s.value for s in SIGNAL_ORDER)
LABEL_TO_INT: dict[str, int] = {label: i for i, label in enumerate(LABEL_ORDER)}
INT_TO_LABEL: dict[int, str] = {i: label for label, i in LABEL_TO_INT.items()}


def encode_labels(y_str: pd.Series | list[str]) -> np.ndarray:
    """Map string labels (go/maybe/no_go) to int in LABEL_ORDER order."""
    return np.asarray([LABEL_TO_INT[str(v)] for v in y_str], dtype=np.int64)


def binarise_thermal(y: np.ndarray) -> np.ndarray:
    """3-class → binary: GO/MAYBE vs NO_GO. Feeds the binary probabilistic
    metrics (Brier + relative-value curve); the reported Peirce/HSS are
    3-class (see evaluate.py). The research doc §4.1 reports a binary Peirce
    anchor of +0.107 on its own dataset — not directly comparable to the
    3-class numbers this spike reports."""
    return (y != int(LABEL_TO_INT[Signal.NO_GO.value])).astype(np.int64)


# --- dataset containers ---------------------------------------------------


@dataclass
class ReplayDataset:
    """The shape every train/evaluate pipeline operates on.

    X, y_str, and y_int are aligned by row. day / month / year / era are
    Series with the same length and index, useful for the year-blocked
    split and per-era reporting.
    """
    X: pd.DataFrame
    y_str: np.ndarray           # string labels (e.g. "go", "maybe", "no_go")
    y_int: np.ndarray           # int labels in LABEL_ORDER
    day: pd.Series
    month: pd.Series
    year: pd.Series
    era: pd.Series
    feature_names: tuple[str, ...]

    @property
    def n_rows(self) -> int:
        return len(self.X)

    @property
    def n_features(self) -> int:
        return len(self.feature_names)

    def binarise(self) -> np.ndarray:
        """Return binary thermal/no-thermal target (1 = GO or MAYBE)."""
        return binarise_thermal(self.y_int)


@dataclass
class YearSplit:
    """Train / calibration / test partition for the year-blocked holdout.

    `calibration` is the most recent year of the training era — used to
    fit the temperature-scaling calibrator (research doc §3.2). It can
    be None if the training set is too small to carve out a year, in
    which case the calibrator falls back to cross-fitted calibration.
    """
    train: ReplayDataset
    calibration: ReplayDataset | None
    test: ReplayDataset
    train_until_year: int
    test_from_year: int
    calibration_year: int | None


# --- loader + splitter ----------------------------------------------------


def load_replay_csv(
    path: Path | str,
    label_col: str = "actual_verdict_thermal",
) -> ReplayDataset:
    """Read a replay CSV, drop rows with no usable target, return a
    ReplayDataset aligned by row.

    Quarantines storm-suspected days (research doc + `compile_report`
    share this convention: gust-front wind isn't a thermal session).
    Skips rows with a missing `day` or a missing target — those are
    legacy records that have a peak reading but no buoy day-curve and
    therefore can't be labelled on the duration/thermal scale.
    """
    import pandas as pd  # lazy — keeps module import pandas-free (see header note)

    if label_col not in TARGET_COLS:
        raise ValueError(
            f"label_col must be one of {TARGET_COLS} (got {label_col!r}). "
            "The replay CSV is the contract — pick from its target columns."
        )
    df = pd.read_csv(path)
    if "day" not in df.columns:
        raise ValueError(f"replay CSV {path} is missing the 'day' column; not a replay-CSV export?")
    if label_col not in df.columns:
        raise ValueError(
            f"replay CSV {path} is missing target column {label_col!r}. "
            f"Regenerate with the Phase A schema (commit a77df22)."
        )

    # Drop rows with no usable target (legacy records, no buoy day-curve).
    df = df.dropna(subset=[label_col]).reset_index(drop=True)
    # Quarantine storm-suspected days — they teach the wrong lesson
    # (gust-front wind isn't a thermal session).
    if "storm_suspected" in df.columns:
        df = df[~df["storm_suspected"].astype(bool)].reset_index(drop=True)

    y_str = df[label_col].astype(str).to_numpy()
    y_int = encode_labels(y_str)
    X = df[list(FEATURE_COLS)].copy()
    # Cast booleans to int so sklearn classifiers don't choke.
    if "rained_yesterday" in X.columns:
        X["rained_yesterday"] = X["rained_yesterday"].astype("Int64").fillna(0).astype(int)

    return ReplayDataset(
        X=X,
        y_str=y_str,
        y_int=y_int,
        day=df["day"],
        month=df["month"],
        year=df["year"],
        era=df["era"],
        feature_names=FEATURE_COLS,
    )


def split_by_year(
    data: ReplayDataset,
    train_until_year: int = 2022,
    test_from_year: int = 2023,
    calibration_year: int | None = 2022,
) -> YearSplit:
    """Year-blocked holdout. Train on years ≤ train_until_year; test on
    years ≥ test_from_year. Carve out `calibration_year` from the train
    set for the temperature-scaling calibrator.

    Research doc §5: "train on years 2017–2022, test on 2023+; calibration
    = 2022 alone". The defaults match; pass overrides only for ablations.
    Raises if the resulting train/cal/test sets are empty — silent
    zero-row fits are debugging traps.

    Also drops any feature column that is 100% NaN in the *training*
    rows, as a defensive guard: a column with zero non-NaN values at fit
    time can't be learned from, and HGB's histogram binner crashes on it
    ("window shape cannot be larger than input array shape"). Since
    c1337e0 the 8 ICON-era-only features (BLH, soil moisture, 850/700 hPa
    wind, LI ×2, CAPE, synoptic wind) are no longer in FEATURE_COLS, so
    the 11 ICON-stable features that remain have no NaNs and this drop is
    a no-op on the real 1,912-row replay. It still fires (and is tested)
    for any future feature that is block-missing in the train era.
    """
    years = data.year.to_numpy()
    train_mask = years <= train_until_year
    test_mask = years >= test_from_year
    if calibration_year is not None and train_until_year >= calibration_year >= years.min():
        cal_mask = years == calibration_year
        train_mask = train_mask & ~cal_mask
    else:
        cal_mask = np.zeros(len(years), dtype=bool)
        calibration_year = None

    # Drop columns that are 100% NaN in the train rows. The test rows
    # are masked OUT of the all-NaN check — we only care about what the
    # model can learn from at fit time.
    train_for_drop = data.X.iloc[train_mask]
    all_nan_cols = [c for c in train_for_drop.columns if train_for_drop[c].isna().all()]
    if all_nan_cols:
        data = _drop_columns(data, all_nan_cols)

    train = _slice(data, train_mask)
    test = _slice(data, test_mask)
    calibration = _slice(data, cal_mask) if calibration_year is not None else None
    if train.n_rows == 0:
        raise ValueError(
            f"year-blocked split produced an empty train set "
            f"(train_until_year={train_until_year}, calibration_year={calibration_year})"
        )
    if test.n_rows == 0:
        raise ValueError(
            f"year-blocked split produced an empty test set (test_from_year={test_from_year})"
        )
    return YearSplit(
        train=train,
        calibration=calibration,
        test=test,
        train_until_year=train_until_year,
        test_from_year=test_from_year,
        calibration_year=calibration_year,
    )


def _slice(data: ReplayDataset, mask: np.ndarray) -> ReplayDataset:
    """Row-slice every aligned field of a ReplayDataset."""
    if not mask.any():
        # Return a zero-row dataset so downstream code can do `len(...)`
        # without a special case. Mirrors pandas slice semantics.
        return ReplayDataset(
            X=data.X.iloc[0:0].copy(),
            y_str=np.empty(0, dtype=object),
            y_int=np.empty(0, dtype=np.int64),
            day=data.day.iloc[0:0].copy(),
            month=data.month.iloc[0:0].copy(),
            year=data.year.iloc[0:0].copy(),
            era=data.era.iloc[0:0].copy(),
            feature_names=data.feature_names,
        )
    return ReplayDataset(
        X=data.X.iloc[mask].reset_index(drop=True),
        y_str=data.y_str[mask],
        y_int=data.y_int[mask],
        day=data.day.iloc[mask].reset_index(drop=True),
        month=data.month.iloc[mask].reset_index(drop=True),
        year=data.year.iloc[mask].reset_index(drop=True),
        era=data.era.iloc[mask].reset_index(drop=True),
        feature_names=data.feature_names,
    )


def _drop_columns(data: ReplayDataset, columns: list[str]) -> ReplayDataset:
    """Return a new ReplayDataset with `columns` removed from X.

    Used by `split_by_year` to strip the ICON-era block-missing features
    that are 100% NaN in the training subset. `feature_names` is updated
    to match; the new dataclass is otherwise identical.
    """
    if not columns:
        return data
    new_feature_names = tuple(c for c in data.feature_names if c not in columns)
    return ReplayDataset(
        X=data.X.drop(columns=list(columns)),
        y_str=data.y_str,
        y_int=data.y_int,
        day=data.day,
        month=data.month,
        year=data.year,
        era=data.era,
        feature_names=new_feature_names,
    )
