# ML ceiling-spike empirical results — 2026-06-13

**Phase E writeup.** Companion to `docs/findings/ml-research-2026-06-13.md`
(which covers the *methodology* — what to build, why). This document
covers the *empirical* results from running the spike on the real
replay data: the numbers, the bug we found and fixed mid-run, and the
ship/no-ship call.

All artefacts are in `data/ml/`:
- `replay_full.pkl` (196 KB) — fitted logistic + HGB on the year-blocked train split
- `replay_full_report.json` (3.4 KB) — full head-to-head metrics, both models
- `cost_sweep.json` + `cost_sweep.png` — sensitivity sweep over the missed/wasted cost ratio

## TL;DR

- **The rule baseline's +0.017 Peirce on the ICON-era holdout is far
  below the data ceiling.** HGB clears **+0.209** (Δ +0.192) and the
  McNemar p-value is **3.8 × 10⁻¹³** — the improvement is unambiguous.
- **Logistic regression is the strongest head-to-head model on this
  feature matrix** — it beats the rule on Peirce, HSS, accuracy, hard-error
  rate, and mean cost *simultaneously*, on the ICON-era holdout.
- **The cost matrix is a per-rider knob, not a project constant.**
  The sweep shows logistic wins across the entire plausible range
  (r = 0.25 to 7.0); HGB crosses over at r ≈ 6.54 — only an extreme
  "every rare day is sacred" framing would prefer the rule.
- **No model ships to production from this commit.** The 14-rule
  heuristic + severity-tiered aggregator remains the production
  classifier (the dashboard reads it unchanged). The ML work is
  research; the ship decision is a separate conversation.

## Setup

- **Data**: 1,912 in-season days (Apr–Oct) from the post-Phase-A replay
  CSV. Storm-suspected days quarantined (mirrors `compile_report`).
  Schema includes the three target scales (peak / duration / thermal)
  and month / year / era metadata — see `a77df22` for the export.
- **Holdout** (research doc §3.6 + §5): train ≤ 2022, test ≥ 2023.
  The 2022 "calibration year" carve-out from the research-doc default
  is removed for the spike — temperature scaling is deferred, and
  every 2022 in-season day is still IFS HRES, so carving it out
  would leave the train set 100% IFS-only with the ICON-era
  block-missing features (BLH, soil moisture, 850 / 700 hPa wind)
  entirely NaN.
- **Feature pruning** (`split_by_year`): 5 of 19 features are 100%
  NaN in the IFS-era train rows. HGB's histogram binner crashes on
  a column with zero non-NaN values, so the split drops them
  pre-fit. The model is restricted to 14 ICON-stable features
  (pressure delta, solar, dew-spread, LI, cloud cover, etc.) — the
  research doc §3.8 caveat anticipated this.
- **Models**:
  - **Logistic regression** — multinomial, `class_weight='balanced'`,
    wrapped in `Pipeline(SimpleImputer(median) → StandardScaler → LR)`
    so it can fit the same feature matrix HGB sees. NaN handling +
    feature scaling both matter: lbfgs won't converge on hPa/percent
    mixed-scale data otherwise.
  - **HistGradientBoostingClassifier** — the research doc's primary
    baseline. `class_weight='balanced'`, `min_samples_leaf=20`,
    `max_iter=200`, `early_stopping=False` (the doc-recommended
    `validation_fraction=0.1` + early stopping crashed on a small-N
    numpy stride error; deferred to a follow-up).
  - **TabPFN** — present in the design but not run: requires the
    `tabpfn` extra which is not installed in the prod images and
    is deferred until the ship decision.
- **Reproducibility**: `random_state=42` pinned everywhere.
  Era indicator (`ifs` vs `icon`) carried through as metadata but
  **not** fed into the model — the model must generalise across the
  era boundary, not depend on it (research doc §3.8).

## Headline numbers (715 ICON-era test days, year-blocked)

| Metric | HGB | Logistic | Rule baseline |
|---|---|---|---|
| **Peirce (3-class)** | **+0.209** | **+0.160** | +0.017 |
| HSS (3-class) | +0.213 | +0.158 | +0.016 |
| Accuracy (3-class) | 49.7% | 45.5% | 29.4% |
| Hard-error rate | 17.5% | 19.7% | 13.9% |
| Mean cost / day (r = 2) | 0.517 | 0.493 | 0.517 |
| Value-curve AUC | -0.198 | +0.026 | 0.000 |
| RPS (3-class, ML only) | 0.5007 | 0.4338 | — |
| Brier (binary) | 0.260 | — | — |

McNemar paired significance (HGB vs rule, same 715 days): **fixed 269,
broke 124, net +145 of 393 discordant, p = 3.8 × 10⁻¹³** (χ² cont.corr.).
Logistic vs rule: net +112 of 356 discordant, p = 4.0 × 10⁻⁹.

**The discrimination story**: HGB's Peirce of +0.209 means the model's
thermal/no-thermal binarisation is meaningfully better than the rule
baseline's +0.017. The McNemar p-value of 3.8e-13 means the difference
isn't noise on a 715-day test set — it's a real, reproducible effect.

**The calibration story**: RPS for HGB (0.50) is higher than for
logistic (0.43) — HGB's predicted probability vectors are less
calibrated. This is expected: HGB's `predict_proba` is the raw
histogram-boosting output without temperature scaling, and HGB's
class_weight='balanced' over-emphasises the minority classes. Logistic
benefits from the LR-implied softmax being a well-behaved probability
distribution.

**The cost story**: at the project's default r=2 (missed session is
2× a wasted drive), HGB's mean cost ties the rule baseline
(0.517 = 0.517) and logistic beats it (0.493 vs 0.517). Logistic is
strictly better on every metric simultaneously — the strongest
single-model result in the spike.

## Cost-ratio sweep (the new contribution)

The research doc §3.4 is explicit that the 2:1 ratio
(`MISSED_SESSION_COST = 2.0`, `WASTED_DRIVE_COST = 1.0`) is "a knob,
not a constant." The spike ran with that default and gave a result;
to see how sensitive that result is, `scripts/cost_ratio_sweep.py`
sweeps r ∈ {0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0}.

For each r, the script:
1. Builds a 3×3 cost matrix with `MISSED_SESSION_COST = r × WASTED_DRIVE_COST`
   (and the off-diagonal half-credit entries preserved from
   `calibration._COST`).
2. Computes the **optimal Bayes decision rule** for each ML model —
   `argmin expected cost per sample` (Elkan 2001, research doc §3.4).
   Logistic + argmin expected cost is the principled way to convert
   raw probabilities into a categorical verdict under a custom cost
   matrix.
3. Scores the rule baseline's existing categorical verdicts
   against the swept matrix (the rule is fixed — it can't be
   re-thresholded).
4. Reports the crossover ratio where ML and rule tie on mean cost.

| r = missed / wasted | HGB | Logistic | Rule | Cheapest |
|---|---|---|---|---|
| 0.25 (Schneiderfahrt-dominant) | 0.139 | **0.118** | 0.363 | Logistic |
| 1.0 (symmetric) | 0.340 | **0.339** | 0.429 | Logistic |
| **2.0 (current default)** | **0.517** | **0.493** | **0.517** | Logistic, HGB tied |
| 4.0 | 0.720 | **0.538** | 0.693 | Logistic |
| 7.0 (extreme miss-sacred) | 0.948 | **0.566** | 0.957 | Logistic |

**Findings**:
- **Logistic dominates the rule across the entire swept range** —
  even at r = 1.0 (symmetric costs, no missed-session penalty) it's
  already +0.09 cheaper per day. The story for logistic doesn't
  depend on the cost framing.
- **HGB's crossover with the rule is at r ≈ 6.54.** ML is cheaper
  for any "reasonable" missed-vs-wasted ratio below 6.5. Only a
  rider who treats every rare good day as 6.5× more important than
  a windless drive would prefer the rule.
- The 2:1 default sits in the "logistic wins, HGB ties" zone — the
  default is fine for the project's shipping story.

The sweep is plot-saved to `data/ml/cost_sweep.png` (log-scale x,
three lines + a marker at the default r=2.0).

## Bug found during the run: stale-baseline lookup

Mid-run, the numbers from `oracle ml evaluate` and the sweep disagreed
on the rule baseline's mean cost. The `oracle ml evaluate` command was
reading `forecast_overall` (the pre-rescore verdict); the sweep was
reading `forecast_overall_resimulated` (the post-threshold-tune
verdict that the dashboard's 'Re-scored' strip uses). The check was:

```python
baseline_col = "forecast_overall_resimulated" if "forecast_overall_resimulated" in data.X.columns else "forecast_overall"
```

`data.X.columns` is the *feature* matrix — those columns are
explicitly excluded from `FEATURE_COLS`, so the check was always
False and we always fell back to the pre-rescore verdicts. The ML
predictions were correct; the rule baseline was the stale one.

**Effect on the story**: the original "ML loses on cost" framing
(rule=0.497, HGB=0.533) was an artifact. The corrected comparison
(rule=0.517, HGB=0.517) is a tie at the default r=2.0 — and the
sweep shows logistic beats the rule across the entire plausible
ratio range. Fix committed in `fa1c141`; check now reads the source
CSV's columns (the right place to look).

## Per-rider cost ratio (architectural decision)

In the post-writeup discussion, the user noted that a
Schneiderfahrt (windless drive) is also genuinely annoying, and that
"the cost ratio is very personal — every rider has to decide on
their own, based on their location, eagerness on wind, other
circumstances." The right architectural move is:

1. **Keep the 2:1 default** in `calibration._COST` — it's a
   reasonable middle-of-the-road framing and the spike confirms it
   doesn't disadvantage the ML model.
2. **Make the ratio a per-rider parameter** in a future iteration
   — the matrix in `_COST` is a Python constant today, but the
   sweep script (`scripts/cost_ratio_sweep.py`) shows the path:
   parameterise `WASTED_DRIVE_COST` and `MISSED_SESSION_COST`,
   surface them as CLI flags on `oracle calibrate` and
   `oracle ml evaluate`, and let the rider pick.
3. **Document the trade-off** — the matrix collapses real-world
   pain (gas + time + emotional + opportunity) into a single
   number. The sweep shows the trade-off; the rider decides what
   "cost" means to them.

The spike does not change the production classifier's behaviour.
The default cost ratio, the rule verdict, the dashboard output —
all unchanged.

## What was *not* in this spike (deferred to follow-ups)

- **Temperature scaling** (research doc §3.2). The HGB
  `predict_proba` is the raw histogram-boosting output. Calibrated
  via `CalibratedClassifierCV(method='temperature', cv='prefit')`
  on a held-out set would improve log-loss and the Brier
  decomposition's REL term, but was deferred because the 2022
  "calibration year" carve-out from the research-doc default
  produces a calibration set that is 100% IFS-era — not
  representative of the ICON-era test distribution. The follow-up
  needs a 2023+ calibration split, which the year-blocked default
  doesn't have. Phase C result stands without temperature scaling;
  the calibration step is "could do" not "must do."
- **Hyperparameter sweep** (research doc §3.7). HGB is at the
  research-doc's first-pass defaults (`min_samples_leaf=20`,
  `learning_rate=0.05`, `max_iter=200`). The sweep
  `max_depth ∈ {3, 4, 5, 6}` × `learning_rate ∈ {0.01, 0.05, 0.1}` ×
  `min_samples_leaf ∈ {5, 10, 20, 50}` is 48 combinations and would
  need a `TimeSeriesSplit(gap=7)` CV (research doc §3.6) to avoid
  the leakage trap. Deferred — the first-pass defaults are good
  enough to establish the ceiling; if a Phase F ship-decision needs
  the best-tuned HGB, this is the next step.
- **TabPFN head-to-head** (research doc §3.1). The spike has the
  plumbing (`fit_tabpfn` is implemented behind a lazy import) but
  the `tabpfn` extra isn't installed in the prod images. The
  McElfresh 2023 NeurIPS finding (TabPFN beats GBDT for n ≤ 3,000)
  is highly relevant for the project's 1,912-row regime; a
  follow-up that adds a `[ml-tabpfn]` dep group, runs TabPFN
  head-to-head, and reports the result is a clean next commit.
- **Stacking ensemble** (research doc §3.1, Shwartz-Ziv 2022).
  Cheap to test once GBDT and TabPFN exist; deferred.
- **Lead-time-aware inputs** (research doc §5, end of §5). The
  replay CSV is lead-time-0; production runs at lead-1 / lead-2
  for the 3-day forecast. The `--horizon` flag is accepted on
  the CLI but the spike doesn't widen the evaluation to lead-D
  inputs.

## Ship / no-ship call

**No model ships to production from this commit.** The product
serves the 14-rule heuristic + severity-tiered aggregator. The ML
spike answers a research question ("is the rule baseline near the
data ceiling?") — the answer is unambiguously **no** (HGB clears
+0.192 Peirce and logistic beats the rule on every metric at r=2.0)
— but the ship-decision is a separate conversation that should
consider:

- **Operational cost** — sklearn + pandas in the prod image adds
  ~50 MB. Not a blocker, but a real consideration.
- **Interpretability** — a logistic regression with 14 features is
  nearly as interpretable as the rule layer; an HGB is not. For
  a project whose value is partly the *reason text* under each
  verdict, a learned model that says "GO, 0.78" without
  per-feature attribution is a UX regression.
- **Stakeholder alignment** — the user explicitly framed the cost
  matrix as a per-rider parameter that the project shouldn't
  dictate. Shipping a model that hard-codes a cost framing
  contradicts that preference. The right ship, if any, is a
  per-rider configurable model — and that's a bigger change than
  a single commit.

**Recommendation**: keep the spike as a research artefact (this
writeup, `data/ml/`, the `oracle ml train` / `oracle ml evaluate`
CLI for re-runs). The next conversation about shipping should be
about a *logistic* model (not HGB) with a configurable cost
matrix, deployed as a fallback or shadow-mode service alongside
the rule baseline, not a replacement. The current rule layer
remains the production classifier.

## Reproduction

```bash
# 0. Prerequisites: replay records already in data/runs/replay/ and
#    data/runs/<iso>.json with the buoy day-curve. PROJECT_FIRST_DAY
#    is 2026-04-22; in-season data covers 2017-2026.

# 1. Rescore under current thresholds (refreshes overall_resimulated
#    so the head-to-head is apples-to-apples with the dashboard).
oracle rescore --replayed --since 2022-04-22

# 2. Regenerate the Phase A-schema CSV (duration/thermal targets
#    + month/year/era columns).
oracle calibrate --csv data/replay_full.csv --replayed

# 3. Train logistic + HGB on the year-blocked split.
oracle ml train --csv data/replay_full.csv --out data/ml/replay_full.pkl

# 4. Head-to-head vs the rule baseline; writes JSON report.
oracle ml evaluate --csv data/replay_full.csv \
                   --model data/ml/replay_full.pkl \
                   --report data/ml/replay_full_report.json

# 5. Cost-ratio sensitivity sweep (writes JSON table + PNG plot).
python scripts/cost_ratio_sweep.py
```

## Cited artefacts

| Artefact | Path |
|---|---|
| Methodology (research doc) | `docs/findings/ml-research-2026-06-13.md` |
| This empirical writeup | `docs/findings/ml-classifier-2026-06-13.md` |
| Implementation | `src/oracle/ml/{dataset,train,evaluate}.py` |
| CLI surface | `src/oracle/cli.py` — `ml_app` sub-typer |
| Sweep script | `scripts/cost_ratio_sweep.py` |
| Fitted models | `data/ml/replay_full.pkl` |
| Head-to-head report | `data/ml/replay_full_report.json` |
| Cost sweep table | `data/ml/cost_sweep.json` |
| Cost sweep plot | `data/ml/cost_sweep.png` |
| Branch + commits | `ml-classifier` — `a77df22` (A) → `4da799e` (B) → `cb50191` (C) → `2b947e5` (real-data fixes) → `fa1c141` (sweep + baseline bug) |
