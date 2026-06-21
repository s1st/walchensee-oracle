"""HGB shadow classifier — HistGradientBoostingClassifier run alongside the rules.

Loaded from the same pkl bundle as the offline ML evaluation. Two ways in:
  - offline, over the whole archive, via `oracle hgb-backfill`;
  - at forecast time in the Cloud Run job, when `Dockerfile.job` ships the
    `[hgb]` extra (scikit-learn) + the pkl and sets `ENABLE_HGB_SHADOW=1` —
    `logger.forecast_to_dict` then attaches the block like `ml_classifier`.

Requires scikit-learn (the `[hgb]` or `[ml]` extra). The serve path is
numpy-only — pandas is *not* needed — to keep the image small. The model
bundle is loaded once and cached (`functools.lru_cache`).

Output schema mirrors `ml_classifier` in run records so the dashboard can
render it identically, but stored as `hgb_classifier` to keep the two
models clearly distinct.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

from oracle.knowledge.rules import SIGNAL_ORDER

_DEFAULT_PKL = Path(__file__).parent.parent.parent / "data" / "ml" / "replay_full.pkl"

# Map the model's int classes back to verdict strings. Derived from SIGNAL_ORDER
# (GO, MAYBE, NO_GO) — the SAME source oracle.ml.dataset.LABEL_ORDER uses at
# train time, so {0: go, 1: maybe, 2: no_go} can't drift from training. Defined
# locally (not imported from oracle.ml.dataset) because that module pulls in
# pandas, which the lean [hgb] prod image deliberately doesn't ship.
_INT_TO_LABEL = {i: s.value for i, s in enumerate(SIGNAL_ORDER)}

_FEATURE_LABEL_EN: dict[str, str] = {
    "munich_hpa": "Munich pressure",
    "innsbruck_hpa": "Innsbruck pressure",
    "bolzano_hpa": "Bolzano pressure",
    "thermik_delta_hpa": "pressure Δ (thermik)",
    "foehn_delta_hpa": "Föhn Δ",
    "overnight_cloud_cover_pct": "overnight cloud",
    "morning_solar_radiation_wm2": "morning solar",
    "min_dew_point_spread_c": "dew-point spread",
    "rained_yesterday": "rained yesterday",
    "yesterday_precipitation_mm": "yesterday rain",
    "max_daytime_low_cloud_pct": "daytime cloud",
}
_FEATURE_LABEL_DE: dict[str, str] = {
    "munich_hpa": "Druck München",
    "innsbruck_hpa": "Druck Innsbruck",
    "bolzano_hpa": "Druck Bozen",
    "thermik_delta_hpa": "Druck-Δ (Thermik)",
    "foehn_delta_hpa": "Föhn-Δ",
    "overnight_cloud_cover_pct": "Bewölkung nachts",
    "morning_solar_radiation_wm2": "Sonne morgens",
    "min_dew_point_spread_c": "Taupunkt-Abstand",
    "rained_yesterday": "gestern Regen",
    "yesterday_precipitation_mm": "Regen gestern",
    "max_daytime_low_cloud_pct": "Bewölkung tags",
}


@functools.lru_cache(maxsize=4)
def _load_hgb(pkl_path: str | None = None):
    """Load + cache the HGB model from the bundle pkl. Requires scikit-learn.

    Cached so the 2 MB bundle is unpickled once per process, not per forecast.
    `pkl_path` is a str (not Path) so the result is hashable/cacheable.
    """
    import pickle

    path = pkl_path or os.environ.get("ML_PKL", str(_DEFAULT_PKL))
    with open(path, "rb") as f:
        bundle = pickle.load(f)
    fitted = bundle["models"]["hgb"]
    return fitted.model, tuple(fitted.model.feature_names_in_)


def classify_hgb(
    pressure: dict | None,
    meteo: dict | None,
    pkl_path: Path | None = None,
) -> dict | None:
    """Score HGB from the serialised inputs dicts. Returns None on missing inputs.

    Returns a dict with the same shape as `ml_classifier` in run records:
      verdict, probabilities {go/maybe/no_go}, reason_en, reason_de.
    """
    if not pressure or not meteo:
        return None

    model, features = _load_hgb(str(pkl_path) if pkl_path else None)

    row: list[float] = []
    for name in features:
        raw = pressure.get(name, meteo.get(name))
        if isinstance(raw, bool):
            raw = float(raw)
        try:
            v = float(raw) if raw is not None else float("nan")
        except (TypeError, ValueError):
            v = float("nan")
        row.append(v)

    import warnings

    import numpy as np

    # numpy-only serve path (no pandas) — keeps the prod image lean. The row is
    # built in `feature_names_in_` order, so positional scoring is correct;
    # sklearn HGB handles NaN natively. The model was fit on a named DataFrame,
    # so a bare array triggers a harmless "no feature names" UserWarning we mute.
    X = np.asarray([row], dtype=float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        proba = model.predict_proba(X)[0]

    # classes_ are ints {0,1,2}. Map back through _INT_TO_LABEL (0=go, 1=maybe,
    # 2=no_go, from SIGNAL_ORDER — the training encoding). A hand-written reverse
    # map here previously swapped go/no_go, flipping the two extreme classes and
    # driving the HGB column to a worse-than-chance Peirce. See
    # docs/findings/stats-panel-season-scoping-2026-06-21.md.
    classes = [_INT_TO_LABEL[int(c)] for c in model.classes_]
    probs = {cls: float(p) for cls, p in zip(classes, proba)}
    verdict = max(probs, key=probs.__getitem__)

    # HGB doesn't expose feature_importances_ for HistGradientBoostingClassifier.
    # Report the three features whose values deviate most from the training median
    # (i.e. the inputs furthest from neutral on this particular day).
    medians = {
        "munich_hpa": 968.0, "innsbruck_hpa": 951.0, "bolzano_hpa": 960.0,
        "thermik_delta_hpa": 17.0, "foehn_delta_hpa": 9.0,
        "overnight_cloud_cover_pct": 40.0, "morning_solar_radiation_wm2": 500.0,
        "min_dew_point_spread_c": 5.0, "rained_yesterday": 0.0,
        "yesterday_precipitation_mm": 0.0, "max_daytime_low_cloud_pct": 30.0,
    }
    deviations = []
    for i, name in enumerate(features):
        v = row[i]
        if not np.isnan(v):
            med = medians.get(name, 0.0)
            deviations.append((name, abs(v - med)))
    top3 = sorted(deviations, key=lambda t: -t[1])[:3]

    top_en = ", ".join(_FEATURE_LABEL_EN.get(f, f) for f, _ in top3)
    top_de = ", ".join(_FEATURE_LABEL_DE.get(f, f) for f, _ in top3)
    return {
        "verdict": verdict,
        "probabilities": probs,
        "top_features": [[f, d] for f, d in top3],
        "reason_en": f"Most distinctive inputs: {top_en}.",
        "reason_de": f"Auffälligste Eingaben: {top_de}.",
    }
