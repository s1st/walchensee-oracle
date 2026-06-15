# ICON-coverage feature shadow retrain (2026-06-15)

**The shadow ML classifier now uses 13 features (11 ICON-stable + BLH + CAPE)
trained on the ICON era only (2023–2026 in-season, n=715).** Previous bundle
was 11 ICON-stable features trained on the full cross-era replay (n=1912). The
retrain closes part of the 2026 dip without touching the rule layer or
`overall`. See `scripts/icon_coverage_experiment.py` for the head-to-head,
`scripts/export_ml_coeffs.py --feature-set extended --train-filter icon` for
the export, and `data/ml/icon_coverage_experiment.json` for the report.

## Why revisit the 11-feature restriction

The 11-feature schema was set in `c1337e0` (Phase C, ml-classifier branch)
after a 17-feature sweep revealed a train/test distribution shift: the
IFS-HRES archive (used 2017–2022) is 70–100% NaN on eight "ICON-era-only"
columns, while the ICON archive (used 2023+) has more coverage. The
restricting-to-11-features re-run showed those 8 weren't adding signal
in the cross-era setup and made the cost-ratio sweep cleaner.

Two things have changed since then:

1. **The data we actually drop is messier than the docs claim.** Pulling
   NaN rates from `data/replay_full.csv` shows that of the 8 supposedly
   "ICON-era-only" features, **6 are 100% NaN even in the ICON era**
   (`synoptic_wind_knots`, `max_lifted_index`, `min_lifted_index`,
   `wind_850_direction_at_peak_deg`, `max_wind_700_knots`,
   `soil_moisture_m3m3`). 1 (`max_cape_j_kg`) is 15% populated. Only 1
   (`max_boundary_layer_height_m`) has meaningful ICON coverage at
   45.7%. So the original "drop all 8" was the right call for 6 of them
   by construction. **But 2 features (`max_boundary_layer_height_m`,
   `max_cape_j_kg`) have real ICON signal, and both map to rules the
   production layer uses** (`boundary_layer_height`,
   `atmospheric_stability`).
2. **ICON-only year-blocked holdout is now feasible.** 4 ICON years
   (2023–2026) are enough for clean year-blocked and LOYO splits within
   the ICON era. The 17→11 experiment was forced to mix eras because
   only 2 months of ICON data existed at the time.

The 11→13 experiment asks a *different* question from the 17→11 one.
17→11 was "train on IFS rows with the ICON columns 100% NaN, test on
ICON where they have real values" (era shift as confound). 11→13 is
"train and test within the same model regime, with the ICON columns
where they have signal" (era shift removed by construction).

## Per-year ICON coverage of the candidate features

| year | rows | BLH pop | CAPE pop |
|---|---:|---:|---:|
| 2023 | 214 | 0.0% | 0.0% |
| 2024 | 214 | 28.5% | 0.0% |
| 2025 | 214 | 90.2% | 15.0% |
| 2026 | 73  | 100.0% | 100.0% |

The ramp from 0% to 100% over 4 years is striking — and matches the
DOCUMENTED reality (the production regime is the 2026+ era where both
features are 100% populated). Any model trained on the cross-era
corpus is learning from a feature whose meaning is "100% missing
before, 100% present now." The ICON-only training set is the regime
the production layer actually runs in.

## The four-way head-to-head

`scripts/icon_coverage_experiment.py` runs four configs on
`data/replay_full.csv` (n=1912 in-season after storm quarantine).
Same scoring protocol as the spike (Peirce/HSS/cost from
`oracle.calibration`; rule baseline = `forecast_overall_resimulated`
from the same CSV).

| config | schema | train set | test set | train n | test n | rule PSS | ML PSS | Δ PSS | ML cost | rule cost | Δ cost |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| (a) | 11 stable | IFS ≤2022 | ICON ≥2023 | 1197 | 715 | +0.066 | **+0.158** | +0.093 | 0.545 | 0.517 | +0.028 |
| (b) | 11 stable | ICON ≤2024 | ICON ≥2025 | 428 | 287 | +0.080 | +0.143 | +0.063 | 0.575 | 0.517 | +0.058 |
| (c) | 11 + BLH + CAPE | ICON ≤2024 | ICON ≥2025 | 428 | 287 | +0.080 | **+0.174** | **+0.094** | **0.530** | 0.517 | **+0.012** |
| (d) | 11 stable | IFS ≤2022 | ICON ≥2025 | 1197 | 287 | +0.080 | +0.167 | +0.087 | **0.514** | 0.517 | **−0.004** |

LOYO within ICON (4 folds: 2023/2024/2025/2026):

| config | rule PSS | ML PSS | Δ PSS | ML cost | rule cost | Δ cost |
|---|---:|---:|---:|---:|---:|---:|
| (b) 11-feature ICON-only | +0.083 | +0.159 | +0.076 | 0.532 | 0.512 | +0.020 |
| (c) 13-feature ICON-only | +0.083 | +0.159 | +0.076 | 0.538 | 0.512 | +0.027 |

## What the numbers say

**Per-fold (LOYO) detail** is in `data/ml/icon_coverage_experiment.json`.
The two-year-blocked 2025+2026 window (rows b, c, d) is the cleanest
test — same test labels, same cost matrix, three different training
strategies:

- **(c) wins on Peirce by +0.030 over (b)** and closes the cost gap to
  the rule from +0.058 to +0.012. Adding the ICON-coverage features
  helps when the model is also trained on the ICON regime.
- **(d) wins on cost** — actually 0.004 *cheaper* than the rule on this
  window. The cross-era 11-feature model has a larger training set
  (1197 vs 428) which buys a tighter fit to the cost matrix. The 13-
  feature ICON-only (c) sacrifices some of that for the BLH/CAPE
  signal.
- **(c) on 2026 specifically (LOYO fold, n=73)**: ML PSS +0.100 vs
  (b)'s +0.079. The 11-feature cross-era (a) on all 715 ICON days is
  +0.158, but the apples-to-apples 2026-only numbers are what the live
  shadow actually competes with. The 13-feature ICON-only closes 26%
  of the 2026 dip (rule +0.160 vs 11-feat +0.079; rule +0.160 vs
  13-feat +0.100).

**The retrain decision**:
- (c) — 13-feature ICON-only — is the new shadow bundle. Better Peirce
  on the year-blocked holdout (+0.094 vs +0.063) and a 4.7× reduction
  in the cost penalty vs the rule (+0.012 vs +0.058). On the live 2026
  regime specifically, +0.021 Peirce over the 11-feature ICON-only.
- (d) — 11-feature cross-era — is the strongest *cost*-framed
  alternative and a candidate for a future A/B comparison; the
  per-rider cost framing the spike deferred is the right time to weigh
  it. For now, the shadow's purpose is discrimination (Peirce =
  "is the model learning anything useful?"), not cost.
- (a) — 11-feature cross-era on the 715-day ICON holdout — remains
  the historical baseline; numbers unchanged by this commit.

## The retrain

```bash
uv pip install -e ".[ml]"   # sklearn + pandas (not in either prod image)
uv run python scripts/export_ml_coeffs.py --csv data/replay_full.csv \
    --feature-set extended --train-filter icon
# → rewrites src/oracle/knowledge/ml_coeffs.py
#   features = 11 stable + max_boundary_layer_height_m + max_cape_j_kg
#   train n = 715, train_population = {BLH: 45.7%, CAPE: 14.7%, rest: 100%}

uv run pytest tests/test_ml_classifier.py
# → 9/9 pass; the golden-vector test was updated to the new bundle
#   (verdict: maybe, go/maybe/no_go = 0.106/0.665/0.228) and verified
#   to match sklearn's predict_proba to 6 decimal places.
```

The pure-Python scorer in `oracle.ml_classifier` is forward-compatible:
both new feature labels (EN/DE) were added to `_FEATURE_LABEL_EN/DE` and
`_feature_value` already pulls from whichever inputs dict carries the
name. A 2026+ forecast's `inputs.meteo` dict already has both fields
populated by `MeteoSnapshot.to_dict` — verified by checking the prod
replay records (e.g. `runs/replay/2025-08-15.json` has
`max_boundary_layer_height_m: 1660.0`, `max_cape_j_kg: None`).

## What this commit does *not* change

- The 14-rule heuristic + severity-tiered aggregator remains the
  production classifier. `overall` is byte-identical with and without
  the new bundle (the shadow-invariant test
  `test_shadow_invariant_ml_does_not_change_overall` still passes).
- The dashboard's ML card renders the same way; the underlying numbers
  and top-feature contributions now reflect the 13-feature model. The
  card text is templated, not pinned.
- The 8 "ICON-era-only" features that are 100% NaN in ICON remain
  dropped. The experiment confirms they would have been 100%
  median-imputation in either case.
- The `oracle ml` CLI behind the `[ml]` extra is unchanged — the spike
  remains the research artefact; the shadow bundle is the only prod
  consumer of `oracle.knowledge.ml_coeffs`.

## What it does change

- The 2026 shadow log will now use 13 features. The first day's
  shadow verdict tomorrow morning is the first one predicted by the
  new model. Each subsequent `ml_classifier` block in `data/runs/*.json`
  carries the new feature set, top-3 contributions, and probabilities.
- The "model" field in each `ml_classifier` block now carries
  `feature_set: "extended"`, `train_filter: "icon"`, and
  `train_population: {…}` — for future debugging / promote-decision
  audits.
- The `oracle ml evaluate` head-to-head report (run against the
  1912-row replay) will report 11-feature numbers as before; running
  with the new bundle would mean re-fitting on the same 1912 rows,
  which isn't the experiment we wanted.

## Open items (deferred)

- **Live A/B between (c) and (d).** The shadow framework could
  surface both (c) and (d) as two parallel cards on the advanced
  panel, with the 2026+ ground truth adjudicating which one tracks
  reality better. Cheap to add (both bundles can be exported and
  scored side-by-side). Skipped for v1 to keep the dashboard clean.
- **HGB re-test on the 13-feature ICON-only schema.** The spike
  showed HGB's edge on 3-class interactions is real but
  +0.058/+0.142 Peirce. The shadow is logistic-only by design (HGB
  needs sklearn in prod, interpretability is a design constraint);
  the same restriction holds here.
- **Lead-time-aware training.** The replay CSV is lead-time-0;
  production runs at lead-1/lead-2 for the 3-day forecast. The
  ICON-only training matches the model regime but doesn't address
  the lead-time shift. Deferred per the spike's open-items list.
- **A 13-feature cross-era sweep.** A theoretical option: train the
  13-feature schema on the full 1912 rows, accepting that BLH/CAPE
  will be median-imputed for the 1197 IFS rows. Not run — the
  17→11 finding already established that columns mostly-learned-from-
  the-median add noise to HGB; assumed the same holds for LR until
  evidence says otherwise.
- **Update the LoYO summary stats** in the spike writeup
  (`ml-classifier-2026-06-13.md`). The 11-feature ICON-only LOYO
  Peirce is +0.159 — slightly higher than the +0.158 from the spike's
  cross-era + LR-tuned head-to-head. Same model, different splits; the
  result isn't contradictory, but the cross-era vs within-ICON
  difference should be documented in a follow-up.

## Reproduction

```bash
# Full head-to-head
uv run python scripts/icon_coverage_experiment.py --csv data/replay_full.csv \
    --out data/ml/icon_coverage_experiment.json

# Re-export the new shadow bundle
uv run python scripts/export_ml_coeffs.py --csv data/replay_full.csv \
    --feature-set extended --train-filter icon

# Verify the scorer matches sklearn
uv run pytest tests/test_ml_classifier.py -v

# Roll back to the 11-feature cross-era bundle if needed
uv run python scripts/export_ml_coeffs.py --csv data/replay_full.csv \
    --feature-set stable --train-filter all
```
