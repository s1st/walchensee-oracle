"""Shadow ML classifier — a distilled logistic model run alongside the rules.

A multinomial logistic regression (go/maybe/no_go), fitted offline on the
replay calibration set and frozen to ~69 floats in
`oracle.knowledge.ml_coeffs.ML_MODEL`. Scored here in **pure Python** (no
sklearn/numpy/pandas) so it adds zero dependencies to the production images.
Verified to reproduce sklearn's `predict` exactly on the training data.

It is **shadow only**: `classify` is called at serialisation time and its
output is logged + surfaced on the dashboard as an experimental extra, but it
never feeds the rule aggregator — the official `overall` is unaffected. The
point is to accumulate a live ground-truth log so a future season can decide
whether to promote it. See docs/findings/ml-shadow-classifier-design-2026-06-14.md
and ml-distill-cut{1,2,3}-2026-06-14.md for the why (logistic, not HGB).

Features are read straight from the same `inputs.pressure` / `inputs.meteo`
dicts the serialiser builds, which are the exact keys the training CSV was
built from (`calibration._row_for`) — so train/serve cannot drift.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from oracle.knowledge.ml_coeffs import ML_MODEL

# Human-readable feature labels for the "why" line (EN / DE). Keys are
# the column names from the replay CSV / pressure+meteo snapshot dicts.
# Adding a feature to the bundle requires adding its label here or the
# scorer falls back to the raw column name.
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
    "max_boundary_layer_height_m": "boundary-layer height",
    "max_cape_j_kg": "CAPE",
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
    "max_boundary_layer_height_m": "Grenzschichthöhe",
    "max_cape_j_kg": "CAPE",
}

_LABEL_DE = {"go": "GO", "maybe": "VIELLEICHT", "no_go": "FLAUTE"}


@dataclass(frozen=True)
class MLForecast:
    """One shadow-classifier prediction. Not a verdict — an experimental extra."""

    verdict: str  # "go" | "maybe" | "no_go" (argmax of probabilities)
    probabilities: dict[str, float]  # {"go": .., "maybe": .., "no_go": ..} sums to 1
    contributions: list[tuple[str, float]]  # top-3 (feature, signed term) for the winning class
    reason_en: str
    reason_de: str

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "probabilities": self.probabilities,
            "contributions": [[f, v] for f, v in self.contributions],
            "reason_en": self.reason_en,
            "reason_de": self.reason_de,
            "model": {"trained_at": ML_MODEL["trained_at"], "n": ML_MODEL["n"]},
        }


def _feature_value(name: str, pressure: dict, meteo: dict) -> float | None:
    """Pull a feature from whichever inputs dict carries it; bool -> int."""
    raw = pressure.get(name, meteo.get(name))
    if isinstance(raw, bool):
        return float(raw)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def classify(pressure: dict | None, meteo: dict | None) -> MLForecast | None:
    """Score the 11 features from the serialised inputs dicts. Pure Python.

    Returns None if either inputs block is missing (degrade like a dropped
    pillar). Missing individual features are median-imputed from the bundle,
    matching the training pipeline's SimpleImputer(strategy="median").
    """
    if not pressure or not meteo:
        return None
    m = ML_MODEL
    feats, med, mean, scale = m["features"], m["median"], m["mean"], m["scale"]
    coef, intercept, labels = m["coef"], m["intercept"], m["labels"]

    z: list[float] = []
    for i, name in enumerate(feats):
        x = _feature_value(name, pressure, meteo)
        if x is None:
            x = med[i]  # median-impute
        z.append((x - mean[i]) / scale[i])

    logits = [
        intercept[k] + sum(coef[k][i] * z[i] for i in range(len(z)))
        for k in range(len(labels))
    ]
    hi = max(logits)
    exps = [math.exp(v - hi) for v in logits]
    total = sum(exps)
    probs = {labels[k]: exps[k] / total for k in range(len(labels))}
    verdict = max(probs, key=probs.__getitem__)

    # Frame contributions for *wind*, not for the winning class, so a green (+)
    # always reads "spoke for a rideable day" regardless of the verdict (a red
    # (−) on a FLAUTE card = a point in favour of wind that got outvoted). The
    # wind/calm axis is the no_go logit — lowering it argues for wind — so we
    # take the negated no_go contribution as the "for-wind" term, and rank the
    # top-3 by how much each input moved that axis.
    kc = labels.index("no_go") if "no_go" in labels else labels.index(verdict)
    contributions = sorted(
        ((feats[i], -coef[kc][i] * z[i]) for i in range(len(z))),
        key=lambda t: -abs(t[1]),
    )[:3]

    reason_en, reason_de = _reasons(verdict, probs, contributions)
    return MLForecast(verdict, probs, contributions, reason_en, reason_de)


def reason_groups(
    contributions: list[tuple[str, float]] | list[list], lang: str
) -> dict[str, list[str]]:
    """Split the top contributions into human-readable feature labels that
    spoke *for* wind vs. *against* it.

    The signed terms are framed on the wind/calm axis (see `classify`): a
    positive term argued for a rideable day, a negative one against it —
    independent of the verdict the model landed on. Accepts the in-memory tuples
    or the stored [feature, value] lists, so the dashboard can colour the same
    split it logs as plain text.
    """
    labels = _FEATURE_LABEL_DE if lang == "de" else _FEATURE_LABEL_EN
    return {
        "for": [labels.get(f, f) for f, v in contributions if v > 0],
        "against": [labels.get(f, f) for f, v in contributions if v <= 0],
    }


def _reasons(
    verdict: str, probs: dict[str, float], contributions: list[tuple[str, float]]
) -> tuple[str, str]:
    # The verdict + probability are already shown in the card's headline and
    # the probs row — don't repeat them here. This line exists only to name
    # the input measurements that moved the model most, so a reader can see
    # *why* (and that it reads raw measurements, not the 14 rules).
    #
    # A positive signed term spoke *for* wind, a negative one *against* it (the
    # wind/calm framing is set in `classify`). We mark that with (+)/(−) rather
    # than ↑/↓ arrows, which read as a wind trend (and clash with the live
    # panel's ↗/↘). The dashboard colours the same split green/red.
    def _clause(lang: str) -> str:
        g = reason_groups(contributions, lang)
        parts = []
        if g["for"]:
            parts.append(f"(+) {', '.join(g['for'])}")
        if g["against"]:
            parts.append(f"(−) {', '.join(g['against'])}")
        return "; ".join(parts)

    reason_en = f"Strongest inputs — {_clause('en')}."
    reason_de = f"Stärkste Eingabemessgrößen — {_clause('de')}."
    return reason_en, reason_de
