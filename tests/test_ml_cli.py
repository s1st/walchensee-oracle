"""CLI surface for the `oracle ml` subcommand.

The test cases here pin the CLI contract: --help works, flags are
present, the deps guard fires cleanly, --label validation runs before
the deps check, and the Phase C body (train + evaluate) works
end-to-end on a synthetic CSV.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from oracle.cli import app


runner = CliRunner()


# --- shared helper --------------------------------------------------------


def _write_synthetic_replay_csv(path: Path, n_per_year_month: int = 8) -> None:
    """Build a small replay CSV the loader can parse. n_per_year_month=8
    gives 192 rows across 4 years × 6 months — enough for HGB to fit
    with stable per-class counts (early-stopping CV needs ≥2 per class
    per fold). The label is a noisy linear function of `thermik_delta_hpa`
    and `morning_solar_radiation_wm2` so the model has something to learn."""
    rng = np.random.default_rng(42)
    days = []
    for y in (2020, 2021, 2022, 2023):
        for m in (4, 5, 6, 7, 8, 9):
            for d_idx in range(n_per_year_month):
                day_num = min(28, 1 + d_idx * 3)
                days.append(f"{y}-{m:02d}-{day_num:02d}")
    n = len(days)
    df = pd.DataFrame({
        "day": days,
        "munich_hpa": rng.normal(1018, 5, n),
        "innsbruck_hpa": rng.normal(1018, 5, n),
        "bolzano_hpa": rng.normal(1018, 5, n),
        "thermik_delta_hpa": rng.normal(2, 3, n),
        "foehn_delta_hpa": rng.normal(0, 2, n),
        "overnight_cloud_cover_pct": rng.uniform(0, 100, n),
        "morning_solar_radiation_wm2": rng.uniform(200, 900, n),
        "synoptic_wind_knots": rng.uniform(0, 30, n),
        "min_dew_point_spread_c": rng.uniform(0, 12, n),
        "max_boundary_layer_height_m": rng.uniform(200, 2000, n),
        "soil_moisture_m3m3": rng.uniform(0.1, 0.4, n),
        "rained_yesterday": rng.integers(0, 2, n).astype(bool),
        "yesterday_precipitation_mm": rng.uniform(0, 5, n),
        "max_lifted_index": rng.uniform(-2, 10, n),
        "min_lifted_index": rng.uniform(-4, 5, n),
        "max_cape_j_kg": rng.uniform(0, 500, n),
        "max_daytime_low_cloud_pct": rng.uniform(0, 100, n),
        "wind_850_direction_at_peak_deg": rng.uniform(0, 360, n),
        "max_wind_700_knots": rng.uniform(0, 40, n),
    })
    score = df["thermik_delta_hpa"] + df["morning_solar_radiation_wm2"] / 200 + rng.normal(0, 0.5, n)
    df["actual_verdict_thermal"] = np.where(score > 4, "go", np.where(score > 2, "maybe", "no_go"))
    df["storm_suspected"] = False
    # Rule baseline that gets ~70% right (a reasonable synthetic baseline).
    noise = rng.normal(0, 1, n)
    score_with_noise = score + noise
    df["forecast_overall_resimulated"] = np.where(
        score_with_noise > 4, "go", np.where(score_with_noise > 2, "maybe", "no_go")
    )
    df["forecast_overall"] = df["forecast_overall_resimulated"]
    for c in ("peak_avg_knots", "peak_gust_knots", "first_ignition_minute",
              "samples_above_8kt", "samples_above_12kt", "actual_verdict", "actual_verdict_duration"):
        df[c] = None
    df["samples_above_8kt"] = 6
    df["samples_above_12kt"] = 4
    parsed = pd.to_datetime(df["day"])
    df["month"] = parsed.dt.month
    df["year"] = parsed.dt.year
    df["era"] = np.where(df["year"] < 2023, "ifs", "icon")
    df.to_csv(path, index=False)


# --- --help / flag surface ------------------------------------------------


def test_root_help_lists_ml_subcommand():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ml" in result.output


def test_ml_help_lists_train_and_evaluate():
    """Phase C added the `evaluate` subcommand next to `train` — both
    must show in `oracle ml --help`."""
    result = runner.invoke(app, ["ml", "--help"])
    assert result.exit_code == 0
    assert "train" in result.output
    assert "evaluate" in result.output


def test_ml_train_help_shows_all_flags():
    """The Phase C contract: --csv, --label, --horizon, --out,
    --train-until-year, --test-from-year, --include-tabpfn are all wired.
    Pinning the flag surface here means a future refactor can't silently
    drop a flag without a test failure."""
    result = runner.invoke(app, ["ml", "train", "--help"])
    assert result.exit_code == 0
    for flag in ("--csv", "--label", "--horizon", "--out",
                 "--train-until-year", "--test-from-year", "--include-tabpfn"):
        assert flag in result.output, f"missing flag {flag} in train --help"


def test_ml_evaluate_help_shows_all_flags():
    result = runner.invoke(app, ["ml", "evaluate", "--help"])
    assert result.exit_code == 0
    for flag in ("--csv", "--model", "--label", "--report", "--no-mcnemar"):
        assert flag in result.output, f"missing flag {flag} in evaluate --help"


# --- input validation -----------------------------------------------------


def test_ml_train_rejects_invalid_label_with_helpful_list():
    """Label validation runs *before* the deps check."""
    result = runner.invoke(app, ["ml", "train", "--csv", "/tmp/x.csv", "--label", "nope"])
    assert result.exit_code != 0
    assert "peak" in result.output and "duration" in result.output and "thermal" in result.output


def test_ml_train_rejects_zero_horizon():
    result = runner.invoke(app, ["ml", "train", "--csv", "/tmp/x.csv", "--horizon", "0"])
    assert result.exit_code != 0
    assert "horizon" in result.output.lower()


def test_ml_evaluate_rejects_invalid_label():
    result = runner.invoke(app, ["ml", "evaluate", "--csv", "/tmp/x.csv", "--model", "/tmp/m.pkl", "--label", "nope"])
    assert result.exit_code != 0
    assert "peak" in result.output


# --- deps guard -----------------------------------------------------------


def test_ml_train_deps_guard_fires_when_sklearn_missing(monkeypatch):
    """On the prod images sklearn is not installed; the CLI must fail
    with a clear, installable message rather than a traceback."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "sklearn":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(app, ["ml", "train", "--csv", "/tmp/x.csv"])
    assert result.exit_code != 0
    assert "ml" in result.output.lower() and ("extra" in result.output.lower() or "install" in result.output.lower())


def test_ml_evaluate_deps_guard_fires_when_sklearn_missing(monkeypatch):
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "sklearn":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    # Need a real model file path for the arg parser; the deps guard
    # fires before file IO, so an empty string is fine.
    result = runner.invoke(app, ["ml", "evaluate", "--csv", "/tmp/x.csv", "--model", "/tmp/m.pkl"])
    assert result.exit_code != 0
    assert "ml" in result.output.lower() and ("extra" in result.output.lower() or "install" in result.output.lower())


# --- end-to-end on synthetic data (sklearn is installed in the test env) -


@pytest.fixture
def synthetic_csv(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic.csv"
    _write_synthetic_replay_csv(path)
    return path


def test_ml_train_end_to_end_writes_model_file(synthetic_csv: Path, tmp_path: Path):
    """The Phase C body should fit the models and write a pickle
    containing both `logistic` and `hgb`."""
    out = tmp_path / "models.pkl"
    result = runner.invoke(app, ["ml", "train", "--csv", str(synthetic_csv), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    import pickle
    with open(out, "rb") as f:
        bundle = pickle.load(f)
    assert set(bundle["models"].keys()) == {"logistic", "hgb"}
    assert bundle["label"] == "thermal"
    assert bundle["train_until_year"] == 2022
    assert bundle["test_from_year"] == 2023


def test_ml_evaluate_end_to_end_writes_report(synthetic_csv: Path, tmp_path: Path):
    """`train` then `evaluate` on the same CSV should produce a JSON
    report with all the metrics for both models."""
    model_path = tmp_path / "models.pkl"
    report_path = tmp_path / "report.json"
    train_result = runner.invoke(app, ["ml", "train", "--csv", str(synthetic_csv), "--out", str(model_path)])
    assert train_result.exit_code == 0, train_result.output
    eval_result = runner.invoke(app, [
        "ml", "evaluate",
        "--csv", str(synthetic_csv),
        "--model", str(model_path),
        "--report", str(report_path),
    ])
    assert eval_result.exit_code == 0, eval_result.output
    assert report_path.exists()
    import json
    rep = json.loads(report_path.read_text())
    assert rep["label"] == "thermal"
    assert rep["n_test"] > 0
    assert set(rep["models"].keys()) == {"logistic", "hgb"}
    # Each model scored on the same metrics
    for name, mdl in rep["models"].items():
        assert "peirce" in mdl["ml"]
        assert "hss" in mdl["ml"]
        assert "accuracy" in mdl["ml"]
        assert "rps" in mdl["ml"]
        assert "brier" in mdl["ml"]
        assert "value_auc" in mdl["ml"]
        assert "p_value" in mdl["mcnemar"]


def test_ml_evaluate_no_mcnemar_skips_p_value(synthetic_csv: Path, tmp_path: Path):
    model_path = tmp_path / "models.pkl"
    runner.invoke(app, ["ml", "train", "--csv", str(synthetic_csv), "--out", str(model_path)])
    report_path = tmp_path / "report_no_mc.json"
    result = runner.invoke(app, [
        "ml", "evaluate",
        "--csv", str(synthetic_csv),
        "--model", str(model_path),
        "--report", str(report_path),
        "--no-mcnemar",
    ])
    assert result.exit_code == 0, result.output
    import json
    rep = json.loads(report_path.read_text())
    for mdl in rep["models"].values():
        assert mdl["mcnemar"] is None
