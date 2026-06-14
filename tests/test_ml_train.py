"""Model fitting and serialisation for the ML ceiling spike (Phase C)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from oracle.ml.dataset import FEATURE_COLS, ReplayDataset
from oracle.ml.train import (
    FittedClassifier,
    MIN_SAMPLES_LEAF,
    RANDOM_STATE,
    fit_all,
    fit_hgb,
    fit_logistic,
    fit_tabpfn,
)


def _synth_dataset(n: int = 240, seed: int = 42) -> ReplayDataset:
    """Build a ReplayDataset for fit/serialise unit tests.

    Features: random floats. Target: round a synthetic linear score to
    one of three classes so the model has something to learn. The size
    is set so each class has ≥30 samples — the early-stopping CV inside
    HGB needs at least 2 per class to fit, and we want a comfortable
    margin so a class imbalance in the synthetic data doesn't trip it."""
    rng = np.random.default_rng(seed)
    days = [f"2022-{m:02d}-{d:02d}" for m in (4, 5, 6, 7) for d in range(1, 28)][:n]
    n = len(days)
    X = pd.DataFrame(
        rng.normal(0, 1, (n, len(FEATURE_COLS))),
        columns=list(FEATURE_COLS),
    )
    # Make each class clearly represented (the early_stopping splitter
    # inside HGB fails when any class has < 2 members in a fold).
    score = X["thermik_delta_hpa"] + 0.5 * X["morning_solar_radiation_wm2"] / 100 + rng.normal(0, 0.3, n)
    y_str = np.where(score > 0.4, "go", np.where(score > -0.1, "maybe", "no_go"))
    from oracle.ml.dataset import encode_labels
    y_int = encode_labels(y_str)
    return ReplayDataset(
        X=X,
        y_str=y_str,
        y_int=y_int,
        day=pd.Series(days),
        month=pd.Series([4, 5, 6, 7] * 100)[:n],
        year=pd.Series([2022] * n),
        era=pd.Series(["ifs"] * n),
        feature_names=FEATURE_COLS,
    )


def test_fit_logistic_returns_fitted_classifier():
    ds = _synth_dataset()
    fitted = fit_logistic(ds)
    assert isinstance(fitted, FittedClassifier)
    assert fitted.name == "logistic"
    assert fitted.classes_.tolist() == [0, 1, 2]
    pred = fitted.predict_int(ds.X)
    assert pred.shape == (ds.n_rows,)
    assert set(pred.tolist()).issubset({0, 1, 2})


def test_fit_hgb_returns_fitted_classifier():
    ds = _synth_dataset()
    fitted = fit_hgb(ds)
    assert isinstance(fitted, FittedClassifier)
    assert fitted.name == "hgb"
    assert fitted.classes_.tolist() == [0, 1, 2]
    pred = fitted.predict_int(ds.X)
    assert pred.shape == (ds.n_rows,)


def test_hgb_uses_random_state_and_min_samples_leaf_defaults():
    """The research doc §3.6 pins random_state=42 everywhere; §3.7
    recommends min_samples_leaf as the key tuning knob. Verify the
    defaults are baked into the factory (and a future tuning pass
    changes them in one place)."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    ds = _synth_dataset()
    fitted = fit_hgb(ds)
    assert isinstance(fitted.model, HistGradientBoostingClassifier)
    assert fitted.model.random_state == RANDOM_STATE
    assert fitted.model.min_samples_leaf == MIN_SAMPLES_LEAF


def test_predict_proba_columns_sum_to_one():
    """Each row's probability vector must sum to 1 (the LABEL_ORDER
    column-order contract that downstream scoring relies on)."""
    ds = _synth_dataset()
    fitted = fit_hgb(ds)
    proba = fitted.predict_proba(ds.X)
    assert proba.shape == (ds.n_rows, 3)
    np.testing.assert_allclose(proba.sum(axis=1), 1.0, atol=1e-6)


def test_save_load_round_trip(tmp_path: Path):
    ds = _synth_dataset()
    fitted = fit_hgb(ds)
    path = tmp_path / "model.pkl"
    fitted.save(path)
    reloaded = FittedClassifier.load(path)
    np.testing.assert_array_equal(reloaded.predict_int(ds.X), fitted.predict_int(ds.X))
    np.testing.assert_allclose(reloaded.predict_proba(ds.X), fitted.predict_proba(ds.X), atol=1e-9)


def test_fit_tabpfn_raises_when_not_installed(monkeypatch):
    """If tabpfn isn't installed, the factory must raise ImportError
    with a clear install hint — not a generic ModuleNotFoundError."""
    import importlib.util
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "tabpfn":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    ds = _synth_dataset()
    with pytest.raises(ImportError, match="tabpfn is not installed"):
        fit_tabpfn(ds)


def test_fit_all_returns_logistic_and_hgb_by_default():
    """The default head-to-head is logistic + hgb. TabPFN is opt-in via
    `include_tabpfn=True` (and silently skipped if the dep isn't there)."""
    ds = _synth_dataset()
    models = fit_all(ds)
    names = {m.name for m in models}
    assert names == {"logistic", "hgb"}


def test_fit_all_with_tabpfn_when_installed(monkeypatch):
    """When tabpfn IS installed and the caller asks for it, fit_all
    adds it. The fake module satisfies the `find_spec` guard; the
    `TabPFNClassifier` import is mocked so we don't need the real
    ~200 MB dep just to test the wiring."""
    import importlib.util
    import sys
    import types

    # Make find_spec believe tabpfn is installed.
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "tabpfn":
            return importlib.util.spec_from_loader("tabpfn", loader=None)
        return real_find_spec(name, *args, **kwargs)

    # Stub the TabPFNClassifier class so the import succeeds + `fit` works.
    class _FakeTabPFN:
        def __init__(self, device="cpu", random_state=42):
            self.device = device
            self.random_state = random_state
            self.classes_ = None
        def fit(self, X, y):
            self.classes_ = np.array(sorted(np.unique(y)))
            return self
        def predict(self, X):
            # Predict the modal class — close enough for the wiring test.
            return np.full(len(X), self.classes_[0])
        def predict_proba(self, X):
            n = len(X)
            k = len(self.classes_)
            out = np.full((n, k), 1.0 / k)
            return out

    fake_pkg = types.ModuleType("tabpfn")
    fake_pkg.TabPFNClassifier = _FakeTabPFN
    monkeypatch.setitem(sys.modules, "tabpfn", fake_pkg)
    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    ds = _synth_dataset()
    models = fit_all(ds, include_tabpfn=True)
    names = {m.name for m in models}
    assert names == {"logistic", "hgb", "tabpfn"}
