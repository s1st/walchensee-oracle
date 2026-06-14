# Phase C — Ceiling spike (ML training + evaluation)

## Scope (per the handoff and `docs/findings/ml-research-2026-06-13.md`)

Build the `oracle.ml` subpackage behind the Phase B `oracle ml train` shell,
plus a new `oracle ml evaluate` subcommand for the head-to-head scoring.
Goal: get an honest answer to "does the rule baseline's +0.107 Peirce
represent the data ceiling, or is there room for a learned model?"
**The spike does not ship a model to production** — that decision is Phase E.

## Approach

### Models (head-to-head, all multinomial 3-class)
1. **Logistic regression** — linear baseline, well-understood calibration
2. **`HistGradientBoostingClassifier`** — the research doc's primary baseline
   (`class_weight='balanced'`, `min_samples_leaf=20`, `random_state=42`)
3. **TabPFN** — optional, lazy-imported; only runs if `tabpfn` is installed
   (the 200 MB+ prior is a separate dep group `[ml-tabpfn]`). Skipped in CI.

### Validation protocol
- **Year-blocked holdout** (matches research doc §3.6 + §5): train ≤ 2022,
  test ≥ 2023. Mirrors production deployment (model trained on the past,
  asked to predict a future it has not seen).
- **Calibration set** = 2022 alone (the most recent year, ICON-era, ~365
  days). Used for `CalibratedClassifierCV(method='temperature', cv='prefit')`.
- **No hyperparameter sweep** for the first spike pass — defaults from the
  research doc. Sweep is a stretch goal, not a blocker.
- `random_state=42` everywhere for reproducibility (per research doc §3.6).

### Scoring protocol (matches research doc §4 + the rule baseline's anchor)
- **Headline**: Peirce on binarised thermal/no-thermal (matches the +0.107
  rule baseline anchor; reuse `peirce_skill_score` from `calibration.py`).
- **Categorical**: 3-class HSS (reuse `heidke_skill_score`), 3-class accuracy,
  hard-error rate (days the forecast said GO when actual was NO_GO or vice versa).
- **Probabilistic** (ML only — the rule baseline is categorical): 3-class RPS,
  binary Brier with Murphy (1973) decomposition `BS = REL − RES + UNC`.
- **Economic value**: relative-value curve across C/L ∈ [0.05, 0.95] in 0.05
  steps; area-under-curve as the single-number summary.
- **Significance**: McNemar's test (exact binomial if discordant < 25,
  else χ²) on the year-blocked test set, ML vs `forecast_overall_resimulated`.
  Reuse `mcnemar` from `calibration.py`.

### Era indicator handling (per research doc §3.8)
- Carry `era` through into the feature matrix as a *flag* (available for
  analysis), but **do not feed it into the model**. The model should
  generalise across the era boundary, not depend on it.
- Report per-era breakdown in the evaluation output (IFS vs ICON) so the
  user can see if the model is using the ICON-stable features or the
  era-specific ones.

## Module layout

```
src/oracle/ml/
  __init__.py        # package marker; lazy-imports train/evaluate
  dataset.py         # load_replay_csv, year-blocked split, label encoding
  train.py           # fit_logistic, fit_hgb, fit_tabpfn; serialize to joblib
  evaluate.py        # metrics (RPS, Brier+Murphy, relative-value), report formatters
```

CLI:
- `oracle ml train --csv PATH [--out MODEL_PATH] [--label thermal]`:
  replaces the Phase B stub. Fits, optionally saves, prints a one-line
  summary (Peirce on the *training* fold + n_train/n_test/era split).
  Skips evaluation (that's the separate `evaluate` subcommand).
- `oracle ml evaluate --csv PATH [--model PATH] [--report PATH] [--label thermal]`:
  runs the head-to-head. If `--model` is given, loads it; otherwise
  refits in-memory (default). Writes JSON metrics to `--report` (default
  `data/ml/report.json`) and a text report to stdout.

## Verification
- `pytest tests/test_ml_*.py` — unit tests for each metric (RPS, Brier
  decomposition, relative-value curve, McNemar reuse) and one
  end-to-end integration test on a synthetic CSV
- `pytest` — full suite stays green
- `ruff check` + `mypy src` — clean (no new mypy errors)
- End-to-end smoke: `oracle ml train --csv <synthetic.csv> --out /tmp/m.joblib`
  + `oracle ml evaluate --csv <synthetic.csv> --model /tmp/m.joblib` on
  synthetic data — all metrics computed, JSON written, text report printed

## Out of scope
- Phase D (distill), Phase E (honest comparison writeup)
- Shipping a model to production (deferred until a ship-decision)
- Lead-time-aware inputs (the replay CSV is lead-time-0; widening to
  lead-D is a follow-up per the handoff)
- Hyperparameter sweep (deferred; defaults first, sweep if defaults
  underperform)
- Stacking ensemble (deferred; only if HGB vs TabPFN are tied)
