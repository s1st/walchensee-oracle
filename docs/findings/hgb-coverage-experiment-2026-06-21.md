# HGB feature-set experiment: do BLH + CAPE help the HGB shadow? — 2026-06-21

## TL;DR

**No.** Retraining the HGB shadow on the extended 13-feature set (11 ICON-stable
+ `max_boundary_layer_height_m` + `max_cape_j_kg`) does **not** improve
out-of-sample skill — it's flat under LOYO and **worse** on the cleaner
year-blocked split, where it also flips from beating the rule to losing to it.
HGB **memorises the training set in every config** (train Peirce ≈ 1.000), and
the two extra features make the train−test gap *grow* on the year-blocked split
(0.895 → 0.932). This is exactly the overfitting the storm-handling finding
flagged as the risk. Keep the bundle HGB at 11 features. (The logistic *did*
benefit from the same two features — but it's a lower-capacity linear model on
the same ~715-day ICON set.)

Run: `uv run python scripts/hgb_coverage_experiment.py`
(`data/ml/hgb_coverage_experiment.json`, gitignored). Production HGB
hyperparameters mirrored from `src/oracle/ml/train.fit_hgb`.

## Setup

Mirror of `scripts/icon_coverage_experiment.py` (the logistic version), same
data / splits / labels / cost matrix / rule baseline, classifier swapped for
`HistGradientBoostingClassifier` with the **shippable** params (max_iter=200,
lr=0.05, min_samples_leaf=20, class_weight='balanced', early_stopping=False) —
not sklearn defaults. 1912 in-season storm-quarantined rows; ICON era = 715 days
across 2023–2026. ICON population of the candidate features: **BLH 45.7 %, CAPE
14.7 %**.

Each fit is scored on its test window **and on its own training set**, so the
train−test Peirce gap measures memorisation directly.

## Results

### LOYO within ICON (mean across 4 folds)

| config | rule PSS | HGB test PSS | Δ vs rule | HGB train PSS | overfit gap |
|---|---:|---:|---:|---:|---:|
| (b) 11-feature | +0.083 | **+0.146** | +0.063 | +1.000 | 0.854 |
| (c) 13-feature (+BLH+CAPE) | +0.083 | **+0.146** | +0.062 | +1.000 | 0.855 |

Adding the two features changes mean test Peirce by **−0.0008** — noise.

### Year-blocked within ICON (train ≤2024 → test ≥2025, n=428→287)

| config | rule PSS | HGB test PSS | Δ vs rule | train PSS | overfit gap |
|---|---:|---:|---:|---:|---:|
| (b) 11-feature | +0.080 | **+0.105** | +0.025 | +1.000 | 0.895 |
| (c) 13-feature | +0.080 | **+0.068** | **−0.012** | +1.000 | 0.932 |

On the split that best removes the era confound, the extended set **drops test
Peirce 0.105 → 0.068** (now *below* the rule), while the train−test gap **grows
0.895 → 0.932**. Textbook overfit: the extra features buy training fit, not
generalisation.

### Cross-era reference (a): 11-feature, train IFS ≤2022 → test ICON ≥2023

HGB test PSS **+0.208** (Δ vs rule +0.142), train PSS +1.000, gap 0.792 — the
strong number from the original ceiling spike. The larger cross-era train (1197
rows) generalises to the 715-day test better than the tiny ICON-only train
(428 rows) does, despite the era distribution shift.

## Interpretation

- **HGB always memorises.** Train Peirce is 1.000 in every config — expected for
  200-iteration boosting without early stopping on a few-hundred-row set. So the
  absolute gap isn't the headline; the headline is the *test* number and whether
  more features make the gap worse (they do, on year-blocked).
- **Capacity is the difference from the logistic.** The logistic gained from
  BLH+CAPE on the same ICON set because it's linear and low-variance — it can't
  exploit the extra columns to memorise. HGB can, so on ~400-row folds the two
  partially-populated features (CAPE only 15 %!) become overfit fuel, not signal.
- **The bundle HGB should stay at 11 features.** No out-of-sample case for the
  extended set; the cleanest split argues against it.
- **Ties back to storm handling** (`storm-handling-rule-vs-learned-2026-06-21.md`):
  even if we *wanted* HGB to see the storm-energy feature (CAPE) so it could
  grade thunderstorm days, this experiment says it wouldn't generalise from it on
  the current data — CAPE is too sparse (15 %) and HGB too eager to overfit it.

## What would change the answer

- **More ICON data.** The year-blocked train is only 428 rows. As 2026+ seasons
  accumulate (CAPE/BLH are ~100 % populated from 2026), re-running this could
  tip (c) positive. Re-run after each season.
- **Regularised HGB.** Higher `min_samples_leaf`, `max_iter` early stopping, or
  an explicit held-out fold could shrink the 0.85–0.93 gap and let the features
  help. Out of scope here — this experiment used the *current production* params
  to answer "swap the feature set, all else equal".

## Status

Research only — no code, model, or bundle change. The HGB shadow stays at 11
features; nothing touches `overall`. New file
`scripts/hgb_coverage_experiment.py`; sibling of the logistic
`scripts/icon_coverage_experiment.py`.
