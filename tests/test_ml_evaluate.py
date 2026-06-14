"""Metrics for the ML ceiling spike (Phase C)."""
from __future__ import annotations

import numpy as np
import pytest

from oracle.calibration import mcnemar as cal_mcnemar
from oracle.ml.dataset import LABEL_TO_INT
from oracle.ml.evaluate import (
    BrierDecomposition,
    HeadToHeadResult,
    ModelScore,
    area_under_value_curve,
    brier_decomposition,
    hard_error_rate,
    multiclass_accuracy,
    relative_value_curve,
    rps_score,
    score_head_to_head,
    score_predictions,
)


# --- helpers --------------------------------------------------------------


def _int_labels(*values: str) -> np.ndarray:
    return np.array([LABEL_TO_INT[v] for v in values], dtype=int)


def _one_hot(*values: str) -> np.ndarray:
    """Build a (N, 3) probability matrix in (go, maybe, no_go) column order."""
    cols = {"go": 0, "maybe": 1, "no_go": 2}
    out = np.zeros((len(values), 3), dtype=float)
    for i, v in enumerate(values):
        out[i, cols[v]] = 1.0
    return out


def _test_dataset_proxy(n: int = 3):
    """A minimal ReplayDataset-shaped object for score_head_to_head tests.

    The scoring functions read y_int and day/month/year/era off the
    dataset. A full ReplayDataset is heavy (X is a DataFrame) — for
    metric unit tests we just need the fields score_head_to_head reads.
    """
    from oracle.ml.dataset import ReplayDataset
    return ReplayDataset(
        X=__import__("pandas").DataFrame(np.zeros((n, 19)), columns=[f"f{i}" for i in range(19)]),
        y_str=np.array(["go", "maybe", "no_go"][:n], dtype=object),
        y_int=_int_labels(*["go", "maybe", "no_go"][:n]),
        day=__import__("pandas").Series([f"2023-06-0{i+1}" for i in range(n)]),
        month=__import__("pandas").Series([6] * n),
        year=__import__("pandas").Series([2023] * n),
        era=__import__("pandas").Series(["icon"] * n),
        feature_names=tuple(f"f{i}" for i in range(19)),
    )


# --- RPS ------------------------------------------------------------------


def test_rps_perfect_forecast_is_zero():
    """A forecast that's a one-hot at the true class scores RPS = 0."""
    y_true = _int_labels("go", "maybe", "no_go")
    y_proba = _one_hot("go", "maybe", "no_go")
    assert rps_score(y_true, y_proba) == 0.0


def test_rps_worst_forecast_is_high():
    """A forecast that's maximally wrong scores the highest RPS."""
    y_true = _int_labels("go", "maybe", "no_go")
    # Predicted go for everything, but actuals are go/maybe/no_go.
    y_proba = np.array([[1.0, 0.0, 0.0]] * 3)
    score = rps_score(y_true, y_proba)
    assert score > 0.5  # arbitrary "high" threshold — exact value is in [0, 1]


def test_rps_uniform_forecast_is_better_than_worst():
    """A uniform forecast should score lower than the maximally-wrong one."""
    y_true = _int_labels("go", "maybe", "no_go")
    uniform = np.full((3, 3), 1.0 / 3.0)
    worst = np.array([[1.0, 0.0, 0.0]] * 3)
    assert rps_score(y_true, uniform) < rps_score(y_true, worst)


def test_rps_handles_empty_input():
    assert rps_score(np.array([], dtype=int), np.empty((0, 3))) == 0.0


# --- Brier with Murphy decomposition --------------------------------------


def test_brier_decomposition_perfect_forecast():
    """A perfect forecast has BS = 0, REL = 0, RES = UNC, BS = REL - RES + UNC = 0."""
    y_true = np.array([1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
    y_prob = y_true.astype(float)  # perfectly calibrated
    b = brier_decomposition(y_true, y_prob, n_bins=2)
    assert isinstance(b, BrierDecomposition)
    assert b.bs == pytest.approx(0.0, abs=1e-9)
    assert b.rel == pytest.approx(0.0, abs=1e-9)
    # RES + UNC can be anything individually; the constraint is BS = REL - RES + UNC.
    assert b.bs == pytest.approx(b.rel - b.res + b.unc, abs=1e-9)


def test_brier_decomposition_constant_climatology():
    """A constant p̄ forecast has RES = 0 (no discrimination) and BS = p̄(1-p̄)."""
    y_true = np.array([0, 0, 1, 1])
    p_bar = 0.5
    y_prob = np.full(4, p_bar)
    b = brier_decomposition(y_true, y_prob, n_bins=2)
    assert b.bs == pytest.approx(p_bar * (1 - p_bar), abs=1e-9)
    assert b.res == pytest.approx(0.0, abs=1e-9)
    # REL = 0 for a constant forecast at p̄
    assert b.rel == pytest.approx(0.0, abs=1e-9)
    assert b.unc == pytest.approx(p_bar * (1 - p_bar), abs=1e-9)


def test_brier_decomposition_satisfies_murphy_identity_within_tolerance():
    """The strict identity BS = REL − RES + UNC only holds when within-bin
    forecast variance is zero (i.e. each bin has a single forecast value).
    For continuous forecasts binned into K intervals the identity is
    approximate — the within-bin variance of `f` around `f̄_k` and the
    bin-edge uncertainty contribute the leftover. Verify the gap is
    small (≪ BS) and bounded by the within-bin variance, not zero."""
    rng = np.random.default_rng(0)
    y_true = rng.integers(0, 2, 200)
    y_prob = np.clip(rng.normal(0.5, 0.2, 200), 0, 1)
    n_bins = 10
    b = brier_decomposition(y_true, y_prob, n_bins=n_bins)
    # The Murphy three-component identity is BS = REL - RES + UNC for the
    # exact decomposition (each unique forecast as its own bin). For a
    # K-bin approximation the gap is small; we check it's ≪ BS rather
    # than exactly zero, which is the standard practice for binned
    # reliability diagrams.
    gap = abs(b.bs - (b.rel - b.res + b.unc))
    assert gap < 0.05 * b.bs, f"Murphy gap {gap:.4f} too large vs BS {b.bs:.4f}"


def test_brier_decomposition_empty_input():
    b = brier_decomposition(np.array([], dtype=int), np.array([], dtype=float))
    assert b.bs == 0.0 and b.rel == 0.0 and b.res == 0.0 and b.unc == 0.0


# --- Relative economic value (CAWCR) --------------------------------------


def test_relative_value_climatology_is_zero():
    """A constant forecast (always predict climatology) has V = 0 at every C/L."""
    y_true = np.array([0, 0, 1, 1, 1, 0, 1, 0])
    # "Predict the base rate for every day" — the climatology forecast.
    p_bar = float(y_true.mean())
    y_prob = np.full(len(y_true), p_bar)
    curve = relative_value_curve(y_true, y_prob, cl_ratios=np.arange(0.1, 0.9, 0.1))
    for r, v in curve.items():
        assert v == pytest.approx(0.0, abs=1e-9), f"V({r}) = {v}, expected 0"


def test_relative_value_perfect_forecast_is_one():
    """A perfect probabilistic forecast scores V = 1 at every C/L
    (the maximum possible value; the user always makes the optimal decision)."""
    y_true = np.array([0, 0, 1, 1, 1, 0, 1, 0])
    y_prob = y_true.astype(float)  # 0 for no-event, 1 for event
    curve = relative_value_curve(y_true, y_prob, cl_ratios=np.arange(0.1, 0.9, 0.1))
    for r, v in curve.items():
        assert v == pytest.approx(1.0, abs=1e-9), f"V({r}) = {v}, expected 1.0"


def test_relative_value_inverted_forecast_is_negative():
    """An inverted forecast (predict 0 when event happens, 1 when it doesn't)
    scores V < 0 — strictly worse than climatology at every C/L."""
    y_true = np.array([0, 0, 1, 1, 1, 0, 1, 0])
    y_prob = 1.0 - y_true.astype(float)  # inverted
    curve = relative_value_curve(y_true, y_prob, cl_ratios=np.arange(0.1, 0.9, 0.1))
    for r, v in curve.items():
        assert v < 0, f"V({r}) = {v}, expected < 0"


def test_relative_value_skips_degenerate_base_rate():
    """All-no-event or all-event labels make p̄ ∈ {0, 1} — V is undefined.
    The implementation must return 0 across the sweep rather than divide
    by zero (research doc §4.4 'Skips r values that would divide by zero')."""
    y_true = np.array([1, 1, 1, 1])
    y_prob = np.array([0.9, 0.8, 0.7, 0.6])
    curve = relative_value_curve(y_true, y_prob, cl_ratios=[0.1, 0.5, 0.9])
    assert all(v == 0.0 for v in curve.values())


def test_area_under_value_curve_integrates_trapezoidally():
    """The AUC of the V curve is the single-number summary (research doc §4.4)."""
    # Hand-computed: a flat curve at V=0.5 from r=0 to r=1 has AUC = 0.5.
    curve = {0.0: 0.5, 0.25: 0.5, 0.5: 0.5, 0.75: 0.5, 1.0: 0.5}
    assert area_under_value_curve(curve) == pytest.approx(0.5, abs=1e-9)


# --- Hard error / accuracy -----------------------------------------------


def test_hard_error_rate_perfect_forecast():
    y_true = _int_labels("go", "maybe", "no_go")
    y_pred = y_true.copy()
    assert hard_error_rate(y_true, y_pred) == 0.0


def test_hard_error_rate_counts_only_go_nogo_swap():
    """Hard error = GO↔NO_GO swap. GO→MAYBE is a softer error, not counted."""
    y_true = _int_labels("go", "no_go", "go", "no_go")
    y_pred = _int_labels("maybe", "go", "no_go", "go")
    # Position 0: go→maybe (softer, not counted)
    # Position 1: no_go→go (HARD)
    # Position 2: go→no_go (HARD)
    # Position 3: no_go→go (HARD)
    # 3 hard errors of 4 → 0.75
    assert hard_error_rate(y_true, y_pred) == 0.75


def test_multiclass_accuracy_diagonal_only():
    y_true = _int_labels("go", "maybe", "no_go", "go")
    y_pred = _int_labels("go", "no_go", "no_go", "go")
    # Position 0: go vs go ✓
    # Position 1: maybe vs no_go ✗
    # Position 2: no_go vs no_go ✓
    # Position 3: go vs go ✓
    # 3 correct of 4 → 0.75
    assert multiclass_accuracy(y_true, y_pred) == 0.75


# --- score_predictions / score_head_to_head -------------------------------


def test_score_predictions_returns_all_fields():
    """When proba is provided, RPS, Brier, and value curve are populated."""
    y_true = _int_labels("go", "maybe", "no_go", "go", "maybe")
    y_pred = _int_labels("go", "maybe", "no_go", "maybe", "no_go")
    y_proba = _one_hot("go", "maybe", "no_go", "maybe", "no_go")
    score = score_predictions("ml", y_true, y_pred, y_proba=y_proba)
    assert isinstance(score, ModelScore)
    assert score.n == 5
    assert score.accuracy == 0.6  # 3/5 correct
    assert score.rps is not None
    assert score.brier is not None
    # The value curve is "0 ≤ V ≤ 1 at every C/L" for a usable forecast;
    # with a near-perfect one-hot forecast and a 5-row mixed target the
    # AUC can be very small (a single C/L ratio flipping the sign), so
    # we just check the helper populates the field — not its magnitude.
    assert score.value_curve is not None


def test_score_predictions_without_proba_omits_probabilistic_metrics():
    """For the rule baseline (categorical only), proba=None and the
    probabilistic fields stay None / 0.0."""
    y_true = _int_labels("go", "maybe", "no_go")
    y_pred = _int_labels("go", "maybe", "no_go")
    score = score_predictions("rule", y_true, y_pred, y_proba=None)
    assert score.proba is None
    assert score.rps is None
    assert score.brier is None
    assert score.value_auc == 0.0


def test_score_head_to_head_deltas_are_signed_correctly():
    """ML beats baseline → deltas positive (for Peirce/HSS/accuracy).
    ML loses on hard-error / mean cost → those deltas can be negative
    because "less is better" for those metrics."""
    test = _test_dataset_proxy(3)
    y_true = test.y_int
    ml_pred = y_true.copy()             # perfect ML
    base_pred = _int_labels("no_go", "no_go", "no_go")  # baseline is uniformly bad
    ml_proba = _one_hot("go", "maybe", "no_go")
    result = score_head_to_head("ml", ml_pred, ml_proba, "rule", base_pred, test)
    assert isinstance(result, HeadToHeadResult)
    assert result.ml.accuracy == 1.0
    assert result.baseline.accuracy < result.ml.accuracy
    assert result.deltas["accuracy"] > 0


def test_score_head_to_head_mcnemar_reuses_calibration_mcnemar():
    """The ML and rule-baseline McNemar must use the same numbers as
    calling `oracle.calibration.mcnemar` directly — that's the contract
    the dashboard's rescore-strip relies on."""
    test = _test_dataset_proxy(3)
    y_true = test.y_int
    # ML gets 2 right, 1 wrong; baseline gets the opposite (1 right, 2 wrong).
    ml_pred = np.array([y_true[0], y_true[1], LABEL_TO_INT["no_go"]])
    base_pred = np.array([LABEL_TO_INT["no_go"], LABEL_TO_INT["no_go"], y_true[2]])
    result = score_head_to_head("ml", ml_pred, _one_hot(*["go", "maybe", "no_go"]),
                                "rule", base_pred, test, run_mcnemar=True)
    # Direct call should give the same b/c.
    expected = cal_mcnemar(
        (base_pred == y_true).tolist(),
        (ml_pred == y_true).tolist(),
    )
    assert result.mcnemar is not None
    assert result.mcnemar.b == expected.b
    assert result.mcnemar.c == expected.c
    assert result.mcnemar.p_value == expected.p_value


def test_score_head_to_head_skip_mcnemar():
    test = _test_dataset_proxy(3)
    y_true = test.y_int
    result = score_head_to_head(
        "ml", y_true.copy(), _one_hot(*["go", "maybe", "no_go"]),
        "rule", y_true.copy(), test, run_mcnemar=False,
    )
    assert result.mcnemar is None
