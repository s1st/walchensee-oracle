"""Dataset loader + year-blocked splitter."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from oracle.ml.dataset import (
    EXCLUDED_COLS,
    FEATURE_COLS,
    LABEL_ORDER,
    LABEL_TO_INT,
    ReplayDataset,
    YearSplit,
    binarise_thermal,
    encode_labels,
    load_replay_csv,
    split_by_year,
)


def _minimal_csv(path: Path, *, with_thermal_label: bool = True,
                 with_storm: bool = False, drop_target_rows: int = 0) -> None:
    """Write a 12-row minimal CSV that the loader can parse."""
    days = [f"2023-{m:02d}-{d:02d}" for m in (4, 5, 6) for d in (1, 15)]
    n = len(days)
    df = pd.DataFrame({
        "day": days,
        "munich_hpa": np.full(n, 1018.0),
        "innsbruck_hpa": np.full(n, 1018.0),
        "bolzano_hpa": np.full(n, 1018.0),
        "thermik_delta_hpa": np.linspace(-1, 5, n),
        "foehn_delta_hpa": np.zeros(n),
        "overnight_cloud_cover_pct": np.full(n, 20.0),
        "morning_solar_radiation_wm2": np.full(n, 700.0),
        "synoptic_wind_knots": np.full(n, 5.0),
        "min_dew_point_spread_c": np.full(n, 6.0),
        "max_boundary_layer_height_m": np.full(n, 1500.0),
        "soil_moisture_m3m3": np.full(n, 0.2),
        "rained_yesterday": np.zeros(n, dtype=bool),
        "yesterday_precipitation_mm": np.zeros(n),
        "max_lifted_index": np.full(n, 3.0),
        "min_lifted_index": np.full(n, 1.0),
        "max_cape_j_kg": np.full(n, 0.0),
        "max_daytime_low_cloud_pct": np.full(n, 20.0),
        "wind_850_direction_at_peak_deg": np.full(n, 30.0),
        "max_wind_700_knots": np.full(n, 10.0),
    })
    if with_thermal_label:
        labels = ["go", "maybe", "no_go"] * (n // 3)
        df["actual_verdict_thermal"] = labels[:n]
        df["actual_verdict"] = labels[:n]
        df["actual_verdict_duration"] = labels[:n]
    else:
        for c in ("actual_verdict_thermal", "actual_verdict", "actual_verdict_duration"):
            df[c] = None
    if with_storm:
        df["storm_suspected"] = [True, False, True] * (n // 3)
    else:
        df["storm_suspected"] = False
    df["forecast_overall_resimulated"] = ["maybe"] * n
    df["forecast_overall"] = ["maybe"] * n
    for c in ("peak_avg_knots", "peak_gust_knots", "first_ignition_minute",
              "samples_above_8kt", "samples_above_12kt"):
        df[c] = None
    df["samples_above_8kt"] = 6
    df["samples_above_12kt"] = 4
    parsed = pd.to_datetime(df["day"])
    df["month"] = parsed.dt.month
    df["year"] = parsed.dt.year
    df["era"] = np.where(df["year"] < 2023, "ifs", "icon")
    if drop_target_rows:
        # Mark the first N rows with a missing target so the loader drops them.
        df.loc[:drop_target_rows - 1, "actual_verdict_thermal"] = None
    df.to_csv(path, index=False)


def test_load_replay_csv_returns_aligned_dataset(tmp_path: Path):
    csv = tmp_path / "r.csv"
    _minimal_csv(csv)
    ds = load_replay_csv(csv)
    assert isinstance(ds, ReplayDataset)
    assert ds.n_rows == 6   # 3 months × 2 days
    assert ds.n_features == 19
    # day / month / year / era aligned to X
    assert len(ds.day) == ds.n_rows
    assert len(ds.month) == ds.n_rows
    assert ds.year.tolist() == [2023] * 6
    assert ds.era.tolist() == ["icon"] * 6


def test_load_replay_csv_drops_rows_with_missing_target(tmp_path: Path):
    csv = tmp_path / "r.csv"
    _minimal_csv(csv, drop_target_rows=4)
    ds = load_replay_csv(csv)
    assert ds.n_rows == 6 - 4


def test_load_replay_csv_quarantines_storm_days(tmp_path: Path):
    csv = tmp_path / "r.csv"
    _minimal_csv(csv, with_storm=True)
    ds = load_replay_csv(csv)
    # [True, False, True] * 2 = 4 storms in 6 rows → 2 survivors.
    assert ds.n_rows == 6 - 4


def test_load_replay_csv_rejects_unknown_label_column(tmp_path: Path):
    csv = tmp_path / "r.csv"
    _minimal_csv(csv)
    with pytest.raises(ValueError, match="label_col must be one of"):
        load_replay_csv(csv, label_col="bogus")


def test_load_replay_csv_rejects_csv_missing_day(tmp_path: Path):
    csv = tmp_path / "r.csv"
    _minimal_csv(csv)
    pd.read_csv(csv).drop(columns=["day"]).to_csv(csv, index=False)
    with pytest.raises(ValueError, match="missing the 'day' column"):
        load_replay_csv(csv)


def test_features_exclude_buoy_forecast_target_metadata():
    """The contract: ML sees only pressure + meteo, not the buoy/forecast
    columns. Pin this so a future refactor can't silently leak a label
    into the feature matrix."""
    for forbidden in (
        "peak_avg_knots", "peak_gust_knots", "first_ignition_minute",
        "samples_above_8kt", "samples_above_12kt",
        "forecast_overall", "forecast_overall_resimulated",
        "actual_verdict", "actual_verdict_duration", "actual_verdict_thermal",
        "storm_suspected", "month", "year", "era", "day",
    ):
        assert forbidden not in FEATURE_COLS, f"{forbidden} leaked into FEATURE_COLS"
    for forbidden in (
        "peak_avg_knots", "forecast_overall", "actual_verdict_thermal", "storm_suspected",
    ):
        assert forbidden in EXCLUDED_COLS


def test_split_by_year_partitions_correctly(tmp_path: Path):
    """Train on ≤ 2022, test on ≥ 2023, calibration = 2022."""
    csv = tmp_path / "r.csv"
    # Build a CSV with 2020, 2021, 2022, 2023 days.
    days = (
        [f"2020-{m:02d}-15" for m in (4, 5, 6)]
        + [f"2021-{m:02d}-15" for m in (4, 5, 6)]
        + [f"2022-{m:02d}-15" for m in (4, 5, 6)]
        + [f"2023-{m:02d}-15" for m in (4, 5, 6)]
    )
    df = pd.DataFrame({"day": days})
    for c in ("munich_hpa", "innsbruck_hpa", "bolzano_hpa", "thermik_delta_hpa", "foehn_delta_hpa",
              "overnight_cloud_cover_pct", "morning_solar_radiation_wm2", "synoptic_wind_knots",
              "min_dew_point_spread_c", "max_boundary_layer_height_m", "soil_moisture_m3m3",
              "rained_yesterday", "yesterday_precipitation_mm", "max_lifted_index", "min_lifted_index",
              "max_cape_j_kg", "max_daytime_low_cloud_pct", "wind_850_direction_at_peak_deg",
              "max_wind_700_knots"):
        df[c] = 0.0
    df["actual_verdict_thermal"] = ["go", "maybe", "no_go"] * 4
    df["actual_verdict"] = df["actual_verdict_thermal"]
    df["actual_verdict_duration"] = df["actual_verdict_thermal"]
    df["storm_suspected"] = False
    df["forecast_overall"] = "maybe"
    df["forecast_overall_resimulated"] = "maybe"
    for c in ("peak_avg_knots", "peak_gust_knots", "first_ignition_minute",
              "samples_above_8kt", "samples_above_12kt"):
        df[c] = None
    df["samples_above_8kt"] = 6
    df["samples_above_12kt"] = 4
    parsed = pd.to_datetime(df["day"])
    df["month"] = parsed.dt.month
    df["year"] = parsed.dt.year
    df["era"] = np.where(df["year"] < 2023, "ifs", "icon")
    df.to_csv(csv, index=False)

    data = load_replay_csv(csv)
    split = split_by_year(data, train_until_year=2022, test_from_year=2023, calibration_year=2022)
    assert isinstance(split, YearSplit)
    assert split.train.year.tolist() == [2020, 2020, 2020, 2021, 2021, 2021]  # 2022 carved out
    assert split.calibration is not None
    assert split.calibration.year.tolist() == [2022, 2022, 2022]
    assert split.test.year.tolist() == [2023, 2023, 2023]


def test_split_by_year_raises_on_empty_train():
    """An empty train set (e.g. all data in 2023, train-until=2022) must
    surface a clear ValueError, not silently produce a 0-row fit."""
    days = pd.to_datetime(["2023-06-01", "2023-07-01", "2023-08-01"])
    df = pd.DataFrame({
        "day": days.strftime("%Y-%m-%d").tolist(),
        "year": [2023, 2023, 2023],
        "month": [6, 7, 8],
        "era": ["icon"] * 3,
        "actual_verdict_thermal": ["go", "maybe", "no_go"],
        "storm_suspected": [False, False, False],
    })
    data = ReplayDataset(
        X=pd.DataFrame(np.zeros((3, 19)), columns=FEATURE_COLS),
        y_str=np.array(["go", "maybe", "no_go"], dtype=object),
        y_int=encode_labels(["go", "maybe", "no_go"]),
        day=df["day"], month=df["month"], year=df["year"], era=df["era"],
        feature_names=FEATURE_COLS,
    )
    with pytest.raises(ValueError, match="empty train set"):
        split_by_year(data, train_until_year=2022, test_from_year=2023, calibration_year=None)


def test_split_by_year_raises_on_empty_test():
    """Mirror of the train test: empty test set (all data in 2022) must raise."""
    days = pd.to_datetime(["2022-06-01", "2022-07-01"])
    df = pd.DataFrame({
        "day": days.strftime("%Y-%m-%d").tolist(),
        "year": [2022, 2022],
        "month": [6, 7],
        "era": ["ifs"] * 2,
        "actual_verdict_thermal": ["go", "maybe"],
        "storm_suspected": [False, False],
    })
    data = ReplayDataset(
        X=pd.DataFrame(np.zeros((2, 19)), columns=FEATURE_COLS),
        y_str=np.array(["go", "maybe"], dtype=object),
        y_int=encode_labels(["go", "maybe"]),
        day=df["day"], month=df["month"], year=df["year"], era=df["era"],
        feature_names=FEATURE_COLS,
    )
    with pytest.raises(ValueError, match="empty test set"):
        split_by_year(data, train_until_year=2022, test_from_year=2023, calibration_year=None)


def test_binarise_thermal_maybe_maps_to_one():
    """GO and MAYBE → 1 (thermal fired), NO_GO → 0."""
    y = np.array([LABEL_TO_INT["go"], LABEL_TO_INT["maybe"], LABEL_TO_INT["no_go"]])
    assert binarise_thermal(y).tolist() == [1, 1, 0]


def test_label_order_matches_signal_order():
    """The label order must match SIGNAL_ORDER so the cost matrix from
    `calibration._COST` (keyed by Signal) applies unchanged to the
    ML pipeline's confusion matrix."""
    from oracle.knowledge.rules import SIGNAL_ORDER
    assert LABEL_ORDER == tuple(s.value for s in SIGNAL_ORDER)
