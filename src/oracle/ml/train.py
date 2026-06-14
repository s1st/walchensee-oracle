"""Model fitting for the ML ceiling spike (Phase C).

Two model families are fit head-to-head per the research doc §3.1 + §5:

1. **Logistic regression** — a linear baseline. Confirms the features
   carry signal at all (if LR doesn't beat the rule baseline, the
   non-linear models probably can't either).
2. **`HistGradientBoostingClassifier`** — the research doc's primary
   baseline. Native NaN handling for the ICON-era block-missing
   predictors; `class_weight='balanced'` to correct the NO_GO prior;
   `min_samples_leaf=20` (the first-pass default from research doc §3.7).

TabPFN is supported but **lazy-imported** behind a `find_spec` guard
(it's a 200 MB+ download; not in the default `[ml]` dep group). The
spike runs HGB + LR by default; pass `--include-tabpfn` to add it.

The fitted model is wrapped in a `FittedClassifier` so the train and
evaluate CLI subcommands share one interface for `predict` / `predict_proba`
and serialisation (joblib).
"""
from __future__ import annotations

import importlib.util
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from oracle.ml.dataset import ReplayDataset


RANDOM_STATE = 42     # research doc §3.6: pin everywhere for reproducibility
MIN_SAMPLES_LEAF = 20  # research doc §3.7: first-pass HGB default for ~1,900 rows


# --- fitted model wrapper -------------------------------------------------


@dataclass
class FittedClassifier:
    """Wraps a fitted sklearn-like classifier with a stable predict surface.

    The wrapper exists so the train and evaluate CLI subcommands share one
    interface (`predict`, `predict_proba`, `predict_int`) and one
    serialisation path (pickle). The internal `model` is typed as `Any`
    because sklearn + TabPFN have different concrete types and the spike
    doesn't need to distinguish them at the call site.
    """
    name: str
    model: Any
    classes_: np.ndarray       # sorted unique int labels in LABEL_ORDER order

    def predict_int(self, X) -> np.ndarray:
        """Predict int class labels (0=go, 1=maybe, 2=no_go)."""
        pred = self.model.predict(X)
        return np.asarray(pred).astype(int)

    def predict_proba(self, X) -> np.ndarray:
        """Predict class probabilities in (N, 3) LABEL_ORDER column order.

        For the project's int-encoded labels (0=go, 1=maybe, 2=no_go),
        sklearn's `model.classes_` is `[0, 1, 2]` and `predict_proba`'s
        columns are in the same order. So the model's output is already
        in LABEL_ORDER order — we just return it as a NumPy array.
        """
        proba = self.model.predict_proba(X)
        return np.asarray(proba, dtype=float)

    def save(self, path: Path | str) -> None:
        """Pickle to disk. joblib would be slightly faster for sklearn
        models but pickle is stdlib and the model is small (~MB)."""
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, path: Path | str) -> "FittedClassifier":
        with open(path, "rb") as f:
            return pickle.load(f)


# --- model factories ------------------------------------------------------


def fit_logistic(data: ReplayDataset) -> FittedClassifier:
    """Multinomial logistic regression with class-balanced weights.

    `class_weight='balanced'` corrects the NO_GO prior (research doc §3.4
    + §3.5). The int-encoded labels in `data.y_int` are 0/1/2 in
    LABEL_ORDER, which is what sklearn's LogisticRegression expects
    (no string→int mapping needed at fit time).
    """
    from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]

    model = LogisticRegression(
        max_iter=1000,
        class_weight="balanced",
        random_state=RANDOM_STATE,
    )
    model.fit(data.X, data.y_int)
    classes_ = np.array(sorted(np.unique(data.y_int)))
    return FittedClassifier(name="logistic", model=model, classes_=classes_)


def fit_hgb(data: ReplayDataset) -> FittedClassifier:
    """HistGradientBoostingClassifier — the research doc's primary baseline.

    Native NaN handling for the ICON-era block-missing features
    (research doc §3.8); `class_weight='balanced'` to correct the
    NO_GO prior; `min_samples_leaf=20` per the doc's first-pass default.
    `max_iter=200` with `n_iter_no_change=20` for early stopping on a
    held-out 10% validation slice — both are sklearn defaults the doc
    endorses (§3.7).
    """
    from sklearn.ensemble import HistGradientBoostingClassifier  # type: ignore[import-untyped]

    model = HistGradientBoostingClassifier(
        max_iter=200,
        learning_rate=0.05,
        min_samples_leaf=MIN_SAMPLES_LEAF,
        class_weight="balanced",
        random_state=RANDOM_STATE,
        early_stopping=True,
        n_iter_no_change=20,
        validation_fraction=0.1,
    )
    model.fit(data.X, data.y_int)
    classes_ = np.array(sorted(np.unique(data.y_int)))
    return FittedClassifier(name="hgb", model=model, classes_=classes_)


def fit_tabpfn(data: ReplayDataset) -> FittedClassifier:
    """TabPFN — optional, lazy-imported.

    Per the research doc §3.1, TabPFN is the "active threat" at this
    sample size: McElfresh et al. (NeurIPS 2023) showed TabPFN beats
    GBDT for n ≤ 3,000. The dependency is large (200 MB+ prior) so it's
    not in the default `[ml]` extra; this function only works if
    `tabpfn` is installed (a separate `[ml-tabpfn]` dep group, deferred
    until the spike decides whether TabPFN is worth pulling in).
    """
    if importlib.util.find_spec("tabpfn") is None:
        raise ImportError(
            "tabpfn is not installed. The 'ml' extra is HGB+LR only; "
            "add a '[ml-tabpfn]' group to pyproject.toml and reinstall "
            "to enable the TabPFN head-to-head."
        )
    from tabpfn import TabPFNClassifier  # type: ignore[import-not-found]

    model = TabPFNClassifier(device="cpu", random_state=RANDOM_STATE)
    model.fit(data.X, data.y_int)
    classes_ = np.array(sorted(np.unique(data.y_int)))
    return FittedClassifier(name="tabpfn", model=model, classes_=classes_)


# --- top-level fit dispatcher --------------------------------------------


def fit_all(data: ReplayDataset, *, include_tabpfn: bool = False) -> list[FittedClassifier]:
    """Fit every model family the spike plans to benchmark.

    Returns a list (in head-to-head order) so the caller can iterate
    predict + score. TabPFN is only included when both the caller asks
    for it *and* the dep is installed; otherwise it's silently skipped
    so the spike can run on a default install.
    """
    models: list[FittedClassifier] = [fit_logistic(data), fit_hgb(data)]
    if include_tabpfn and importlib.util.find_spec("tabpfn") is not None:
        models.append(fit_tabpfn(data))
    return models
