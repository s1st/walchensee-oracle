# Design — shadow ML classifier + user-facing "ML forecast" (2026-06-14)

**Goal.** Run the distilled logistic classifier alongside the rule layer:
(1) **shadow mode** — log its verdict every forecast for ongoing
calibration, without touching the official `overall`; (2) a **user-facing
"ML Classifier (experimental)"** artifact on the dashboard. This resolves
the one open ship question (the 2026 dip, n=73) with *live* data instead
of betting the verdict on it, and gives riders an extra independent read.

Grounded in the Phase D / TS-CV findings:
- The **logistic, not HGB** (LR beats the rule in 9/10 leave-one-year-out
  folds, mean Peirce +0.215 vs +0.114; HGB is stronger some years but
  erratic and collapses on 2026).
- 3-class multinomial LR (go/maybe/no_go), **argmax** verdict — matches the
  validated CV numbers. Per-rider cost-thresholding is a later knob.

## Why this is *not* "ship a model" in the rejected sense
- **Zero new prod deps.** The scorer is pure Python (`math` only) — verified
  to reproduce sklearn exactly (0/1912 mismatches). No sklearn/numpy/pandas
  in either Docker image. The model is **69 floats** in a committed constant.
- **Fully interpretable.** Output carries the top per-feature contributions
  to the chosen class (standardized-unit terms) — a *more* honest "why"
  than pass/fail rule reasons.
- **Per-rider-cost-respecting.** It emits class probabilities; the GO/MAYBE/
  NO_GO cut stays a threshold the rider could own later. Shadow v1 uses
  argmax and changes nothing user-deciding.
- **Shadow.** Does not feed the aggregator; `overall` is unchanged.

## Components

### 1. Coefficient bundle (offline → committed constant)
`scripts/export_ml_coeffs.py` (extends the verified `/tmp/export_verify.py`):
fits the LR on the full replay (all years, n=1912), dumps a frozen bundle:
```
features[11], labels[3], median[11], mean[11], scale[11],
coef[3][11], intercept[3], trained_on, trained_at, n
```
Written to `src/oracle/knowledge/ml_coeffs.py` as a literal dict (69 floats
+ metadata). Re-running the script is the **retrain** step.

### 2. Pure-Python scorer — `src/oracle/ml_classifier.py`
```python
@dataclass(frozen=True)
class MLForecast:
    verdict: str                      # go | maybe | no_go (argmax)
    probabilities: dict[str, float]   # {go, maybe, no_go}
    contributions: list[tuple[str, float]]  # top-3 signed, standardized units
    reason_en: str
    reason_de: str

def classify(pressure_inputs: dict, meteo_inputs: dict) -> MLForecast | None
```
- **Feature extraction is trivial and train/serve-consistent:** all 11
  features are already keys in the serialized `inputs.pressure` /
  `inputs.meteo` dicts (verified — `calibration._row_for` builds the
  training CSV from the *same* `p.get("munich_hpa")` / `m.get(...)` keys).
  So `classify` reads the feature values straight from those dicts — the
  exact values the model trained on, no snapshot-attribute plumbing, no
  drift risk.
- Median-imputes missing, standardizes, logits → softmax → argmax.
- Returns `None` if either inputs dict is absent (degrade gracefully, like
  a dropped pillar). `forecast_to_dict` already has both dicts in hand.
- `reason_*` templated DE/EN: e.g. EN "Learned model: 54% MAYBE (top
  factors: morning solar +0.28, Δp +0.20, daytime cloud −0.18)."

**The 11 features** (exact keys, present in every record's `inputs`):
`inputs.pressure`: `munich_hpa`, `innsbruck_hpa`, `bolzano_hpa`,
`thermik_delta_hpa`, `foehn_delta_hpa`. `inputs.meteo`:
`overnight_cloud_cover_pct`, `morning_solar_radiation_wm2`,
`min_dew_point_spread_c`, `rained_yesterday` (bool→int),
`yesterday_precipitation_mm`, `max_daytime_low_cloud_pct`.

### 3. Integration — at serialization time in `logger.forecast_to_dict`
`forecast_to_dict` already builds the `inputs.pressure` / `inputs.meteo`
dicts; call `classify(pressure_dict, meteo_dict)` right there and attach the
block. This keeps it **out of** `engine.run_forecast` and `apply_rules`
entirely — structurally impossible to influence `overall`, and it scores
the identical serialized values. (Alternatively attach in `run_forecast`;
serializer-side is cleaner for the shadow invariant.)

### 4. Serialization / logging — `logger.forecast_to_dict`
Add an additive block (omitted when `None`):
```json
"ml_classifier": {
  "verdict": "maybe",
  "probabilities": {"go":0.20,"maybe":0.54,"no_go":0.27},
  "contributions": [["morning_solar_radiation_wm2",0.28], ...],
  "model": {"trained_at": "...", "n": 1912}
}
```
Preserved across re-runs like `ground_truth`. Enables a future
`oracle calibrate --field ml_classifier.verdict` to score it as the live
2026 sample accrues — the whole point of shadow mode.

### 5. Dashboard — `dashboard/main.py` + `index.html`
A clearly-labelled secondary card, **never** replacing the headline verdict:
```
🤖 ML Classifier · experimental
   MAYBE   go 20% · maybe 54% · no_go 27%
   why: ↑ morning solar, ↑ pressure Δ, ↓ daytime cloud
   ⓘ A learned logistic model run alongside the rules, in shadow mode.
     Not the official verdict. [link to this writeup]
```
DE/EN via the existing `reason_de`/`reason_en` pattern. Reads `ml_classifier`
from the same record; no new endpoint. Hidden gracefully if the block is
absent (old records).

### 6. Tests — `tests/test_ml_classifier.py`
- Scorer reproduces a hand-pinned probability vector for a fixed feature
  dict (golden test against the committed coefficients).
- Missing feature → median-imputed, no crash; missing pillar → `None`.
- **Shadow invariant:** a forecast's `overall` is byte-identical with and
  without the ML block (it must not influence aggregation).
- Serialization round-trips; absent block omitted cleanly.
- `rained_yesterday` bool→int cast handled.

### 7. Docs / retraining cadence
- README/CLAUDE note: "shadow ML classifier — interpretable LR, 69 floats,
  re-export with `scripts/export_ml_coeffs.py` after each season; promote
  past shadow only if it tracks/beats the rule as 2026+ ground truth grows."
- Honest banner in the writeup: skill is +0.215 mean (LOYO) but the live
  2026 sample (n=73) currently favors the rule — shadow mode is *how we
  find out*, not a claim it's better.

## Build / deploy plan
- New branch off `main` (`feat-shadow-ml-classifier`). Pure-Python scorer +
  constant live in the **core** package (deploys in both images, no dep
  change). Dashboard card ships via the dashboard image.
- One reviewable PR. Standard suite + ruff + mypy; the shadow-invariant test
  is the safety net that it can't change live verdicts.
- After merge: the daily job logs `ml_classifier` from then on; backfill
  earlier 2026 days by re-running `oracle forecast --day` if we want the
  full season in the shadow log.

## Explicitly out of scope (v1)
- Letting the ML verdict influence `overall` (that's the promote decision,
  after 2026 data).
- Per-rider cost-thresholding (probabilities are logged; the knob comes later).
- HGB / interactions (rejected: black box, 2026 collapse, +deps).
- Auto-retraining (manual `export_ml_coeffs.py`; cadence is a human call).

## Open questions for sign-off
1. **Verdict rule for v1:** plain argmax (simplest, matches CV) vs a fixed
   cost-aware threshold now. Recommend argmax for v1.
2. **Dashboard prominence:** secondary card in the advanced panel (quiet) vs
   always-visible under the headline (more exposure, more "is this the real
   answer?" risk). Recommend advanced panel + a one-line teaser.
3. **Branch/merge:** straight to `main` behind the experimental label, or
   keep on a branch until the 2026 shadow log shows it's trustworthy.
