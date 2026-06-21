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
from typing import Any

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

# The HGB bundle (`data/ml/replay_full.pkl`) is trained on the year-blocked
# split (≤2022) and its published +0.208 Peirce is the ≥2023 out-of-sample
# holdout — see docs/findings/ml-classifier-2026-06-13.md and
# docs/findings/stats-panel-season-scoping-2026-06-21.md. Scoring it over the
# whole replay archive mixes its own training years in and is not comparable,
# so the HGB column is restricted to its test era. The distilled logistic
# (ml_coeffs.py) and the rule layer are full-history backtests, not held out.
_HGB_HOLDOUT_SINCE = "2023-01-01"


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
    report: Report, field: str, store: RunStore, since: str | None = None
) -> dict[str, Any]:
    """Score a shadow classifier against the ground-truth days in the report.

    For replay records lacking a pre-computed block (logistic only — pure
    Python, safe to score on-the-fly) we score from stored inputs. HGB
    requires sklearn so it stays 0 until hgb-backfill --replayed has run.

    `since` (ISO date string) restricts scoring to days on or after that date —
    used to hold the HGB model out to its ≥2023 test era (`_HGB_HOLDOUT_SINCE`).
    ISO `YYYY-MM-DD` keys sort lexicographically, so a string compare suffices.
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


def build_payload(store: RunStore | None = None) -> dict[str, Any]:
    """Run the full calibration walk and return the dashboard stats payload.

    Scored on the thermal season only (`config.ACTIVE_SEASON_MONTHS`, Apr–Oct) —
    the product never serves Nov–Mar, and including those trivial winter
    no-wind days inflates the count and flatters specificity.

    Uses the resimulated verdicts (`resimulated=True`) so the panel reflects
    the *current* rule layer, not whatever the aggregator said when each
    replay record was written. This requires the replay archive to have been
    rescored — run `oracle rescore --replayed` (and `oracle hgb-backfill
    --replayed` for the HGB column) before `stats-update`, or every record
    lacks `overall_resimulated` and the walk scores zero days.

    Grades against the `thermal` label — the same target `oracle ml train`
    defaults to — so the rule and both ML models are scored on what they were
    built to predict (foehn/frontal days relabelled NO_GO). The HGB column is
    held out to its ≥2023 test era (`_HGB_HOLDOUT_SINCE`); the rule and the
    distilled logistic are full-history backtests. See
    docs/findings/stats-panel-season-scoping-2026-06-21.md.
    """
    store = store or default_store()
    report = compile_report(
        store, label="thermal", resimulated=True, replayed=True,
        months=config.ACTIVE_SEASON_MONTHS,
    )
    payload = _rule_payload(report)
    payload["ml"] = _model_payload(report, "ml_classifier", store)
    payload["hgb"] = _model_payload(
        report, "hgb_classifier", store, since=_HGB_HOLDOUT_SINCE
    )
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
