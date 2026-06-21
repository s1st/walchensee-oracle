"""Pre-computed stats cache for the dashboard.

`build_payload(store)` runs the full calibration walk (replayed=True,
resimulated=True, label=duration) and returns a JSON-serialisable dict
with the same shape the /stats template expects. Write it once a day from
the oracle-forecast Cloud Run job via `oracle stats-update`; the dashboard
reads it directly without touching the GCS replay archive on every request.

Both the rule-layer stats and the two shadow classifiers (logistic + HGB)
are included in the same payload so the dashboard gets everything in one read.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Callable

from oracle import config
from oracle.calibration import (
    Report,
    _empty_confusion as _cal_empty_confusion,
    _label_record as _cal_label_record,
    _merged_replay_record as _cal_merged_replay_record,
    compile_report,
    constant_baselines as _cal_constant_baselines,
)
from oracle.knowledge.rules import SIGNAL_ORDER, Signal
from oracle.logger import RunStore, default_store

_STATS_KEY = "_stats_cache"

# The bundle (`data/ml/replay_full.pkl`) is trained on the year-blocked split
# (≤2022); its published +0.208 HGB Peirce is the ≥2023 out-of-sample holdout —
# see docs/findings/ml-classifier-2026-06-13.md and
# docs/findings/stats-panel-season-scoping-2026-06-21.md. The dashboard's
# `holdout` panel scores all three models on this same ≥2023 window so the
# comparison is apples-to-apples; the `live` panel is a full-history backtest of
# the deployed rule + distilled logistic (trained on all years, not held out).
_HGB_HOLDOUT_FROM = (2023, 1, 1)


def _binary_rates(
    confusion: dict[str, dict[str, int]],
) -> tuple[float | None, float | None]:
    pos = (Signal.GO.value, Signal.MAYBE.value)
    neg = Signal.NO_GO.value
    tp = sum(confusion[f][a] for f in pos for a in pos)
    fn = sum(confusion[neg][a] for a in pos)
    tn = confusion[neg][neg]
    fp = sum(confusion[f][neg] for f in pos)
    sens = tp / (tp + fn) if tp + fn else None
    spec = tn / (tn + fp) if tn + fp else None
    return sens, spec


def _rule_payload(report: Report) -> dict[str, Any]:
    sens, spec = _binary_rates(report.confusion)
    matrix = [
        {
            "forecast": f.value,
            "cells": [report.confusion[f.value][a.value] for a in SIGNAL_ORDER],
        }
        for f in SIGNAL_ORDER
    ]
    baselines = report.baselines() if report.sample_size else {}
    best = max(baselines, key=lambda k: baselines[k]["accuracy"]) if baselines else None
    peirce = (sens + spec - 1.0) if (sens is not None and spec is not None) else None
    return {
        "n": report.sample_size,
        "accuracy": report.overall_accuracy if report.sample_size else None,
        "baseline_class": best,
        "baseline_accuracy": baselines[best]["accuracy"] if best else None,
        "quarantined": len(report.quarantined_days),
        "matrix": matrix,
        "axis": [s.value for s in SIGNAL_ORDER],
        "sensitivity": sens,
        "specificity": spec,
        "peirce": peirce,
    }


def _model_payload(
    report: Report,
    field: str,
    store: RunStore,
    since: str | None = None,
    scorer: "Callable[[dict], str | None] | None" = None,
) -> dict[str, Any]:
    """Score a shadow classifier against the ground-truth days in the report.

    Two modes:
      - `scorer` given: compute the verdict from the merged record on the fly
        (used for the ≥2023 holdout, where we score the bundle's ≤2022 logistic
        and HGB so all three columns share one out-of-sample day set).
      - else, read the stored `field` block; for `ml_classifier` fall back to
        scoring the distilled logistic on the fly (pure Python).

    `since` (ISO date string) restricts scoring to days on or after that date —
    used to hold the bundle models out to their ≥2023 test era. ISO
    `YYYY-MM-DD` keys sort lexicographically, so a string compare suffices.
    """
    from oracle.ml_classifier import classify as _classify_logistic

    valid = {s.value for s in SIGNAL_ORDER}
    confusion = _cal_empty_confusion()
    n = 0
    for iso in report.days_with_ground_truth:
        if since is not None and iso < since:
            continue
        # Use the merged record (replay inputs + main-record ground truth),
        # same join compile_report uses — raw replay records have machine=None.
        record = _cal_merged_replay_record(store, iso)
        if not record:
            continue
        if scorer is not None:
            ml = scorer(record)
        else:
            ml = (record.get(field) or {}).get("verdict")
            if ml is None and field == "ml_classifier":
                inputs = record.get("inputs") or {}
                result = _classify_logistic(inputs.get("pressure"), inputs.get("meteo"))
                ml = result.verdict if result else None
        if ml not in valid:
            continue
        actual = _cal_label_record(record, report.label_mode)
        if actual is None or actual not in valid:
            continue
        confusion[ml][actual] += 1
        n += 1

    if n == 0:
        return {
            "n": 0, "accuracy": None, "baseline_class": None,
            "baseline_accuracy": None, "quarantined": len(report.quarantined_days),
            "matrix": [], "axis": [s.value for s in SIGNAL_ORDER],
            "sensitivity": None, "specificity": None,
        }
    sens, spec = _binary_rates(confusion)
    peirce = (sens + spec - 1.0) if (sens is not None and spec is not None) else None
    matrix = [
        {"forecast": f.value, "cells": [confusion[f.value][a.value] for a in SIGNAL_ORDER]}
        for f in SIGNAL_ORDER
    ]
    baselines = _cal_constant_baselines(confusion)
    best = max(baselines, key=lambda k: baselines[k]["accuracy"]) if baselines else None
    hits = sum(confusion[s.value][s.value] for s in SIGNAL_ORDER)
    return {
        "n": n,
        "accuracy": hits / n,
        "baseline_class": best,
        "baseline_accuracy": baselines[best]["accuracy"] if best else None,
        "quarantined": len(report.quarantined_days),
        "matrix": matrix,
        "axis": [s.value for s in SIGNAL_ORDER],
        "sensitivity": sens,
        "specificity": spec,
        "peirce": peirce,
    }


def _clean(obj: Any) -> Any:
    """Make the payload JSON-safe: replace NaN/Inf with None."""
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _bundle_scorer(which: str) -> "Callable[[dict], str | None]":
    """A scorer that runs a bundle model (`hgb`/`logistic`, both trained ≤2022)
    on a record's stored inputs — for the ≥2023 holdout head-to-head."""
    from oracle.hgb_shadow import classify_bundle

    def score(record: dict) -> str | None:
        inputs = record.get("inputs") or {}
        res = classify_bundle(which, inputs.get("pressure"), inputs.get("meteo"))
        return res["verdict"] if res else None

    return score


def build_payload(store: RunStore | None = None) -> dict[str, Any]:
    """Run the calibration walk and return the two-panel dashboard stats payload.

    Everything is scored on the thermal season (`config.ACTIVE_SEASON_MONTHS`,
    Apr–Oct) against the `thermal` label (the target `oracle ml train` and the
    distilled logistic are trained on — foehn/frontal days relabelled NO_GO),
    with the *current* rule layer (`resimulated=True`). This requires the replay
    archive to have been rescored — run `oracle rescore --replayed` (and
    `oracle hgb-backfill --replayed`) before `stats-update`, or the walk scores
    zero days.

    Two panels, so every confusion matrix in a panel shares one day set:
      - `live`: the deployed models over the whole archive — the rule and the
        distilled logistic (`ml_coeffs.py`, trained on all years). Full-history
        backtest (~1900 days).
      - `holdout`: a fair three-way head-to-head on the ≥2023 test era
        (`_HGB_HOLDOUT_FROM`), all genuinely out-of-sample — the rule plus the
        bundle's ≤2022 logistic and HGB (`replay_full.pkl`). ~715 days,
        matching docs/findings/ml-classifier-2026-06-13.md.

    Top-level keys mirror `live` (rule fields + `ml`) for backward compatibility
    with the loading/empty-state checks. See
    docs/findings/stats-panel-season-scoping-2026-06-21.md.
    """
    store = store or default_store()
    months = config.ACTIVE_SEASON_MONTHS
    # Panel 1 — live models, full-history.
    report = compile_report(
        store, label="thermal", resimulated=True, replayed=True, months=months,
    )
    payload = _rule_payload(report)
    payload["ml"] = _model_payload(report, "ml_classifier", store)
    # Panel 2 — ≥2023 holdout: one shared out-of-sample day set, all three
    # scored on it (rule + the bundle's ≤2022 logistic + HGB).
    holdout = compile_report(
        store, label="thermal", resimulated=True, replayed=True, months=months,
        since=date(*_HGB_HOLDOUT_FROM),
    )
    payload["holdout"] = {
        "rule": _rule_payload(holdout),
        "ml": _model_payload(holdout, "ml_classifier", store,
                             scorer=_bundle_scorer("logistic")),
        "hgb": _model_payload(holdout, "hgb_classifier", store,
                              scorer=_bundle_scorer("hgb")),
    }
    return _clean(payload)


def write_cache(store: RunStore | None = None) -> dict[str, Any]:
    """Build and persist the stats payload. Returns the payload."""
    store = store or default_store()
    payload = build_payload(store)
    store.write(_STATS_KEY, payload)
    return payload


def read_cache(store: RunStore | None = None) -> dict[str, Any] | None:
    """Read the pre-computed payload, or None if not yet written."""
    store = store or default_store()
    return store.read(_STATS_KEY)
