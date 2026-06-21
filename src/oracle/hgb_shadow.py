"""HGB shadow classifier — HistGradientBoostingClassifier run alongside the rules.

Loaded from the same pkl bundle as the offline ML evaluation; scored at
backfill time (not at forecast time) via `oracle hgb-backfill`. Requires
the `[ml]` extra (sklearn). Never runs in the prod Docker images.

Output schema mirrors `ml_classifier` in run records so the dashboard can
render it identically, but stored as `hgb_classifier` to keep the two
models clearly distinct.
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_PKL = Path(__file__).parent.parent.parent / "data" / "ml" / "replay_full.pkl"

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


def _load_hgb(pkl_path: Path | None = None):
    """Load the HGB model from the bundle pkl. Requires sklearn ([ml] extra)."""
    import pickle

    path = pkl_path or Path(os.environ.get("ML_PKL", str(_DEFAULT_PKL)))
    with open(path, "rb") as f:
        bundle = pickle.load(f)
    fitted = bundle["models"]["hgb"]
    return fitted.model, list(fitted.model.feature_names_in_)


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

    model, features = _load_hgb(pkl_path)

    row: list[float | None] = []
    for name in features:
        raw = pressure.get(name, meteo.get(name))
        if isinstance(raw, bool):
            raw = float(raw)
        try:
            v = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            v = None
        row.append(v)

    import pandas as pd

    # sklearn HGB handles NaN natively for missing values
    X = pd.DataFrame(
        [[v if v is not None else float("nan") for v in row]], columns=features
    )
    proba = model.predict_proba(X)[0]

    # classes_ are ints {0,1,2}. Map back through the SAME encoding training
    # used (oracle.ml.dataset.INT_TO_LABEL: 0=go, 1=maybe, 2=no_go) — a
    # hand-written reverse map here previously swapped go/no_go, which flipped
    # the two extreme classes and drove the HGB column to a worse-than-chance
    # Peirce. See docs/findings/stats-panel-season-scoping-2026-06-21.md.
    from oracle.ml.dataset import INT_TO_LABEL

    classes = [INT_TO_LABEL[int(c)] for c in model.classes_]
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
        if v is not None:
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
