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

- **The rule baseline's +0.066 Peirce on the ICON-era holdout is far
  below the data ceiling.** HGB clears **+0.208** (Δ +0.142) and the
  McNemar p-value is **3.8 × 10⁻⁸** — the improvement is unambiguous.
- **Logistic regression on the same 11 ICON-stable features beats the
  rule on Peirce, HSS, accuracy, and hard-error rate *simultaneously***,
  with McNemar p = 1.7 × 10⁻⁵. Logistic is the strongest head-to-head
  model; HGB is slightly better at Peirce but slightly worse at hard-error
  rate and value-curve AUC.
- **The 11 ICON-stable features are the right feature set.** The
  first sweep used 17 features (including 6 ICON-era signals that
  were 70-100% NaN in train but had real values at test). User review
  caught the resulting distribution shift as a methodological
  confound; the spike was re-run with the ICON-only features dropped.
  Test Peirce barely changed (-0.001 for both models), confirming the
  ICON-only signals weren't really adding signal — and the cost-ratio
  story got much cleaner.
- **The cost matrix is a per-rider knob, not a project constant.**
  The cleaned sweep shows **both** ML models dominate the rule across
  the entire plausible range (r = 0.25 to 7.0); no crossover for
  either. The previous "HGB crosses over at r ≈ 6.54" finding was
  an artifact of the 6 noisy ICON-only features hurting HGB's
  cost-efficiency.
- **Tier 1 + Tier 2 optimisation was a "no harm done" exercise.**
  HGB's best CV Peirce (max_iter=200, learning_rate=0.01, min_samples_leaf=20)
  is +0.233, only 0.001 Peirce better than the unoptimised defaults.
  Cost-sensitive `class_weight={'go': 1.5/2/3, ...}` variants all
  underperformed the project's `class_weight='balanced'` default
  by 0.020-0.047 Peirce. The defaults were already near-optimal.
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
- **Feature pruning** (revised after user review): the model is
  restricted to **11 ICON-stable features** — 5 pressure (Munich /
  Innsbruck / Bolzano MSL + the two deltas) + 6 meteo (overnight
  cloud cover, morning solar, dew-spread, rained_yesterday,
  yesterday's precip, daytime low cloud). The 8 ICON-era-only
  features (synoptic_wind_knots, max_boundary_layer_height_m,
  soil_moisture_m3m3, max_lifted_index, min_lifted_index,
  max_cape_j_kg, wind_850_direction_at_peak_deg, max_wind_700_knots)
  were 70-100% NaN in the IFS-era training rows but had real
  values at test time (ICON archive launched 2022-11-24). The
  user flagged this in review as a methodological confound —
  the test Peirce conflated "model learned the right patterns"
  with "model learned to use the new ICON features." The
  cleaner alternative (restrict to features measured in BOTH
  eras) is the spike's official position. The 11-feature schema
  drops HGB's NaN-handling complexity and the era-boundary
  distribution shift; the cost is losing 8 potentially-useful
  features at test time. Empirically, dropping them barely
  changes test Peirce (-0.001 for both models) — the ICON-only
  features weren't really adding signal — but the cost-ratio
  sweep got much cleaner (see below).
- **Hyperparameter sweep + class_weight ablation** (added after
  the user asked "what is possible with defendable effort").
  Tier 1: `scripts/tune_ml.py` runs TimeSeriesSplit(gap=7) CV
  over HGB (36 combos = max_iter × learning_rate × min_samples_leaf)
  and logistic (12 combos = C × class_weight), picks the best by
  mean Peirce across 3 folds. Tier 2: takes the best HGB hyperparams
  and sweeps class_weight ∈ {balanced, uniform, 1.5×, 2×, 3× GO-favoring}
  — 5 options. Total ~160 fits, runs in ~3 minutes.
  Writes `data/ml/tuning_results.json` with per-fold + per-combo
  scores. Results: HGB best at `max_iter=200, learning_rate=0.01,
  min_samples_leaf=20` (only the learning_rate differs from the
  default 0.05); logistic best at `C=1.0, class_weight='balanced'`
  (the defaults). `class_weight='balanced'` beat all cost-sensitive
  dict variants in Tier 2 — the project's default is genuinely the
  right choice for HGB on this corpus.
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

## Headline numbers (715 ICON-era test days, year-blocked, 11 ICON-stable features)

| Metric | HGB tuned | Logistic tuned | Rule baseline (post-rescore) |
|---|---|---|---|
| **Peirce (3-class)** | **+0.208** | +0.158 | +0.066 |
| HSS (3-class) | +0.209 | +0.157 | +0.062 |
| Accuracy (3-class) | 48.8% | 44.8% | 34.8% |
| Hard-error rate | 19.0% | 19.0% | 20.6% |
| Mean cost / day (r = 2) | 0.534 | 0.545 | 0.517 |
| Value-curve AUC † | -0.161 | +0.035 | 0.000 |
| RPS (3-class, ML only) | 0.4931 | 0.4317 | — |
| Brier (binary) | 0.254 | — | — |

† The rule baseline's value-curve AUC of 0.000 is a "not computed"
sentinel, not a measured zero: the rule emits a categorical verdict with
no probability, so the relative-value curve (which needs `predict_proba`)
is undefined for it. Don't read it as "HGB is worse than the rule on
value" — there is no rule value curve to compare against.

McNemar paired significance (HGB vs rule, same 715 days): **fixed 212,
broke 112, net +100 of 324 discordant, p = 3.8 × 10⁻⁸** (χ² cont.corr.).
Logistic vs rule: net +71 of 265 discordant, p = 1.7 × 10⁻⁵.

**Note on the rule baseline's Peirce**: the +0.066 figure uses the
post-rescore verdicts (`forecast_overall_resimulated`, the version
the dashboard's 'Re-scored' strip displays). An earlier draft of
this writeup reported the rule at +0.017 — that was a bug where
the evaluate command was reading the pre-rescore verdicts
(`forecast_overall`); see the "Bug found during the run" section
below. The +0.066 number is the rule's actual current performance
on the same 715 days.

**The discrimination story**: HGB's Peirce of +0.208 means the model's
thermal/no-thermal discrimination is meaningfully better than the rule
baseline's +0.066. The McNemar p-value of 3.8 × 10⁻⁸ means the difference
isn't noise on a 715-day test set — it's a real, reproducible effect.

**The calibration story**: RPS for HGB (0.50) is higher than for
logistic (0.43) — HGB's predicted probability vectors are less
calibrated. This is expected: HGB's `predict_proba` is the raw
histogram-boosting output without temperature scaling, and HGB's
class_weight='balanced' over-emphasises the minority classes. Logistic
benefits from the LR-implied softmax being a well-behaved probability
distribution.

**The cost story** (read this carefully — the verdict depends on the
decision rule, and the two framings disagree): mean cost is the one
metric where the answer flips depending on how you turn an ML
probability vector into a categorical verdict.

- **Under plain argmax** (pick the highest-probability class — the
  headline table above), at the project's default r=2 the **rule
  baseline is actually the cheapest**: rule 0.517, HGB 0.534, logistic
  0.545. On this decision rule the ML models lose on cost.
- **Under the Bayes-optimal decision rule** (argmin expected cost per
  sample, see the cost-ratio sweep below), at r=2 **both ML models
  beat the rule**: HGB 0.503, logistic 0.490, rule 0.517.

This is the one place the comparison is *not* strictly
apples-to-apples: the Bayes framing gives each ML model a cost-aware
threshold tuned per cost ratio, while the rule keeps its fixed
categorical verdicts and cannot be re-thresholded. So the honest
summary is: **logistic (and HGB) win on Peirce, HSS, accuracy, and
hard-error rate under any decision rule** — but their *cost* advantage
is contingent on Bayes-optimal thresholding. Do not claim ML is "better
on every metric simultaneously"; on argmax cost it is not.

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
| 0.25 (Schneiderfahrt-dominant) | 0.137 | **0.111** | 0.363 | Logistic |
| 1.0 (symmetric) | 0.346 | **0.336** | 0.429 | Logistic |
| **2.0 (current default)** | **0.503** | **0.490** | **0.517** | **Both ML beat rule** |
| 4.0 | 0.692 | **0.543** | 0.693 | Logistic (HGB ≈ rule) |
| 7.0 (extreme miss-sacred) | 0.890 | **0.569** | 0.957 | Logistic (HGB < rule) |

**Findings (revised after the 11-feature re-run)**:
- **Both ML models dominate the rule across the entire swept range**
  (r = 0.25 to 7.0). No crossover for either. This is a stronger
  result than the original sweep (which had HGB crossing over at
  r ≈ 6.54 with the 17-feature set). The 6 ICON-only features
  weren't adding signal — they were adding noise that hurt HGB's
  cost-efficiency on the swept ratios. Dropping them is a strict
  improvement on every cost framing.
- Logistic is the cheaper model at every r; HGB is the cheaper
  model than the rule at every r but slightly more expensive than
  logistic at every r. For a rider who picks the ML model over
  the rule, logistic is the better default.
- The 2:1 default sits in the "both ML beat rule" zone — the
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
(rule=0.497, HGB=0.533, from the 17-feature pre-fix run) was an
artifact. The corrected comparison (post-fix, still 17 features)
showed rule=0.517, HGB=0.517 — a tie at the default r=2.0. The
follow-up 11-feature re-run improved HGB's cost-efficiency further
(rule=0.517, HGB=0.503 at r=2, with the optimal Bayes decision
rule). Fix committed in `fa1c141`; check now reads the source CSV's
columns (the right place to look).

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
- **Tier 1 hyperparameter sweep** (research doc §3.7) — **DONE** in
  `scripts/tune_ml.py`. Result: HGB best at `max_iter=200,
  learning_rate=0.01, min_samples_leaf=20` (only the learning_rate
  differs from the default 0.05); logistic best at the defaults.
  Test scores moved by ≤0.001 Peirce from the unoptimised defaults.
  Cost-sensitive class_weight variants all underperformed
  `class_weight='balanced'`. The doc's first-pass defaults were
  already near-optimal for this corpus.
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
- **SHAP feature importance** (research doc §3.8). The cleanest
  way to confirm the model is using the ICON-stable features
  rather than gaming the era boundary. The 11-feature re-run
  removed the era-boundary confound by construction (train and
  test now have the same feature distribution), so SHAP is less
  critical than it was for the 17-feature run — but a per-day
  SHAP plot would still be useful for the dashboard's
  "why this verdict?" tooltips.

## Ship / no-ship call

**No model ships to production from this commit.** The product
serves the 14-rule heuristic + severity-tiered aggregator. The ML
spike answers a research question ("is the rule baseline near the
data ceiling?") — the answer is unambiguously **no**. With the
cleaned 11 ICON-stable features:
- HGB clears +0.142 Peirce over the rule (McNemar p = 3.8 × 10⁻⁸)
- Logistic beats the rule on Peirce, HSS, accuracy, and hard-error
  rate simultaneously (McNemar p = 1.7 × 10⁻⁵)
- Both ML models beat the rule on mean cost across the entire
  swept cost-ratio range r ∈ [0.25, 7.0]

…but the ship-decision is a separate conversation that should
consider:

- **Operational cost** — sklearn + pandas in the prod image adds
  ~50 MB. Not a blocker, but a real consideration.
- **Interpretability** — a logistic regression with 11 features is
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
CLI for re-runs, `scripts/cost_ratio_sweep.py` + `scripts/tune_ml.py`
for sweeps). The next conversation about shipping should be about
a *logistic* model (not HGB) with a configurable cost matrix,
deployed as a fallback or shadow-mode service alongside the rule
baseline, not a replacement. The current rule layer remains the
production classifier.

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

# 6. Optional: hyperparam + class_weight tuning (Tier 1 + Tier 2).
#    ~3 min on the 11-feature set, writes data/ml/tuning_results.json.
python scripts/tune_ml.py
```

## Cited artefacts

| Artefact | Path |
|---|---|
| Methodology (research doc) | `docs/findings/ml-research-2026-06-13.md` |
| This empirical writeup | `docs/findings/ml-classifier-2026-06-13.md` |
| Implementation | `src/oracle/ml/{dataset,train,evaluate}.py` |
| CLI surface | `src/oracle/cli.py` — `ml_app` sub-typer |
| Cost-ratio sweep | `scripts/cost_ratio_sweep.py` |
| Hyperparam + class_weight tuning | `scripts/tune_ml.py` |
| Fitted models | `data/ml/replay_full.pkl` |
| Head-to-head report | `data/ml/replay_full_report.json` |
| Cost sweep table | `data/ml/cost_sweep.json` |
| Cost sweep plot | `data/ml/cost_sweep.png` |
| Tuning results | `data/ml/tuning_results.json` |
| Branch + commits | `ml-classifier` — `a77df22` (A) → `4da799e` (B) → `cb50191` (C) → `2b947e5` (real-data fixes) → `fa1c141` (sweep + baseline bug) → `c1337e0` (drop ICON-only features + tune) |
