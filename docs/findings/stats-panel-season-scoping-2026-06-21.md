# Stats panel: season-scoping, label alignment, and an HGB label-swap bug — 2026-06-21

## TL;DR

What began as a copy fix ("Vorhersage-Qualität (ganze Saison)" sat above
**3263 days** — nonsense for a season) turned into a four-layer cleanup of the
`/stats` forecast-quality panel. Net result: the panel now scores the
**thermal** label over the **Apr–Oct season**, on the **current** rule layer,
with each model graded on the target it was actually built for — and a genuine
**label-swap bug** in the HGB serve path is fixed.

Final season-scoped numbers (binary skill = sensitivity + specificity − 1):

| model | n | accuracy | sensitivity | specificity | **Peirce** |
|---|---|---|---|---|---|
| Rule (current rules) | 1912 | 0.379 | 0.893 | 0.275 | **+0.168** |
| ML logistic (shadow) | 1912 | 0.499 | 0.760 | 0.535 | **+0.294** |
| HGB (≥2023 holdout) | 715 | — | 0.663 | 0.611 | **+0.275** |

(HGB row from the corrected-mapping validation; confirmed via re-backfill +
`stats-update`.)

## What was wrong, in layers

### 1. The panel scored the whole year, not the season
`stats_cache.build_payload` called `compile_report(...)` with **no `months=`**,
so it scored all 12 months. The product only serves Apr–Oct
(`config.ACTIVE_SEASON_MONTHS`); the off-season days padded the count to 3263
**and** flattered specificity (trivial winter "no wind" calls). Fix: pass
`months=config.ACTIVE_SEASON_MONTHS`. Header relabelled
"alle Saisons, Apr–Okt" / "all seasons, Apr–Oct".

Effect: 3263 → 1912 days. But this alone *dropped* the headline Peirce from
0.157 to ~0.02 — proof that the winter padding had been doing all the work.

### 2. The replay archive had no resimulated verdicts
`build_payload` reads `resimulated=True` (current rules), i.e.
`overall_resimulated` on each replay record. Those fields were absent on the
bucket (the records predate the last rescore / were re-replayed since), so the
walk scored **zero days** — any `stats-update` would have written `n=0`. The
live `n=3263` cache was stale, built from *original* verdicts. Fix: run
`oracle rescore --replayed` to repopulate `overall_resimulated` (current rules)
before `stats-update`. This is a standing dependency, now documented in
`build_payload`.

### 3. Wrong grading label (duration vs thermal)
The panel graded against the **duration** label, but both ML models are
trained on the **thermal** label:
- `oracle ml train` defaults to `--label thermal` (the HGB bundle).
- `scripts/export_ml_coeffs.py` uses `TARGET = "actual_verdict_thermal"` (the
  distilled logistic in `ml_coeffs.py`).

The `thermal` label is the `duration` label with foehn/frontal "wind" days
relabelled NO_GO (onset-timing + gust-coherence gates in
`calibration.actual_verdict_thermal`). Grading the models on `duration` counted
foehn days as "wind" the models were trained to reject. Fix: `build_payload`
now uses `label="thermal"`.

Effect: rule +0.128 → **+0.168**, logistic +0.127 → **+0.294**. The label fix
*vindicated the shadow logistic* — on its native target it clearly beats the
rule (~+0.13 Peirce).

### 4. HGB was scored over its own training years
The HGB bundle (`data/ml/replay_full.pkl`: `label=thermal`,
`train_until_year=2022`, `test_from_year=2023`) is a **year-blocked** model; its
published +0.208 Peirce is the **≥2023 out-of-sample holdout** (715 days).
`hgb-backfill --replayed` scored it over the whole 2016–2026 archive — mostly
its own training years, an out-of-context aggregate. Fix: `_model_payload`
takes a `since` arg; the HGB column is restricted to `_HGB_HOLDOUT_SINCE =
"2023-01-01"` (n=715, exactly matching the doc). The rule and the distilled
logistic (trained on all years) stay full-history backtests.

### 5. The real bug — HGB go/no_go labels were swapped at serve time
Even on the correct 715-day holdout with the thermal label, HGB scored
**−0.14** (worse than chance) vs the documented +0.208. Root cause in
`hgb_shadow.py`:

```python
_CLASS_MAP = {0: "no_go", 1: "maybe", 2: "go"}   # WRONG
```

Training encodes labels via `oracle.ml.dataset.INT_TO_LABEL`, which is
**`{0: 'go', 1: 'maybe', 2: 'no_go'}`** (it follows `SIGNAL_ORDER` =
GO, MAYBE, NO_GO). The serve map reversed 0 and 2, so every HGB **GO**
prediction was recorded as **NO_GO** and vice versa. A skilled model with its
two extreme classes flipped scores worse-than-chance — exactly the −0.14
Peirce and collapsed specificity (0.19).

Validation on the 715-day holdout (`data/replay_full.csv`, thermal, ≥2023):

```
OLD map {0:no_go,1:maybe,2:go}:  sens 0.666  spec 0.194  Peirce −0.140
FIX map {0:go,1:maybe,2:no_go}:  sens 0.663  spec 0.611  Peirce +0.275
```

Fix: drop the hand-written `_CLASS_MAP`; map class ints back through the same
`INT_TO_LABEL` training used, so the encoding can't drift again.

## Concept clarification (it bit us twice)

The models always **output** the 3-class verdict GO / MAYBE / NO_GO. `peak` /
`duration` / `thermal` are **labelings** — different ways to turn a day's
measured Urfeld wind curve into GO/MAYBE/NO_GO *ground truth* for
training/grading. "Thermal" is not a fourth class. Both ML models aim at the
`thermal` target; the rule is the product's actual output, gradeable against
any label.

## Files changed

- `src/oracle/stats_cache.py` — season filter, `resimulated=True`, `thermal`
  label, `_HGB_HOLDOUT_SINCE`, `_model_payload(since=...)`.
- `src/oracle/hgb_shadow.py` — class map fixed to `INT_TO_LABEL`.
- `src/oracle/dashboard/main.py` — header relabel (DE/EN); HGB note now states
  the ≥2023 holdout window.
- `tests/test_dashboard.py`, `tests/test_ml_classifier.py` — stale imports /
  assertions from the earlier `stats_cache` refactor.

## Regenerating the cache (prod)

Order matters — the archive must be rescored/backfilled before `stats-update`:

```
RUNS_BUCKET=walchi-oracle-prod-runs oracle rescore --replayed       # current rules → overall_resimulated
RUNS_BUCKET=walchi-oracle-prod-runs oracle hgb-backfill --replayed  # HGB blocks (needs [ml] extra)
RUNS_BUCKET=walchi-oracle-prod-runs oracle stats-update             # writes runs/_stats_cache.json
gcloud run services update walchi-oracle-dash --region europe-west1 \
  --update-env-vars STATS_CACHE_BUST=<sha>                          # clear the 1h in-process cache
```

Back up `runs/replay/` and `runs/_stats_cache.json` first (both done 2026-06-21:
`runs-backup-rescore-*` and `_stats_cache.json.bak-*`).

## Addendum — HGB now served at forecast time (no more manual backfill)

Originally HGB was backfill-only (sklearn not in the prod images), so the
*current* day's `/history` HGB cell was always empty until someone ran
`oracle hgb-backfill`, and the per-day blocks carried the pre-fix swapped
verdicts. We moved HGB into the Cloud Run **job** image:

- `Dockerfile.job` installs the lean `[hgb]` extra (scikit-learn + numpy — **no
  pandas/matplotlib**; the serve path is numpy-only), ships
  `data/ml/replay_full.pkl` (committed via a `.gitignore` exception), and sets
  `ML_PKL` + `ENABLE_HGB_SHADOW=1`. ~+45 MB compressed; the dashboard image is
  untouched (it only reads the stored block).
- `logger.forecast_to_dict` attaches `hgb_classifier` when `ENABLE_HGB_SHADOW`
  is set, guarded so a shadow failure never breaks the forecast and so
  local/dashboard/test runs are unchanged.
- `hgb_shadow._load_hgb` is `lru_cache`d (unpickle the 2 MB bundle once/process).

**Two pandas-import traps had to be cleared first** — both surfaced only
in-cluster because the guard swallowed the ImportError (the job ran, wrote *no*
HGB block):

1. `hgb_shadow` imported `INT_TO_LABEL` from `oracle.ml.dataset` (pulls pandas).
   Fixed: derive the map locally from `SIGNAL_ORDER`.
2. Unpickling the bundle imports `oracle.ml.train` → `oracle.ml.dataset`, which
   imported pandas **at module level**. Fixed: `oracle.ml.dataset` now imports
   pandas lazily (`TYPE_CHECKING` + a local import in `load_replay_csv`).

The no-pandas regression test must `_load_bundle_model.cache_clear()` first —
otherwise a prior test's cached model hides the unpickle path (that's why trap #2
slipped through the first time). After re-pinning the job to the new image, an
executed `oracle-forecast` writes `hgb_classifier` (verified 2026-06-21:
`overall=no_go`, `hgb=go`).

## Addendum — two-panel comparison (mismatched day counts looked off)

With HGB held out to ≥2023 (715 days) while the rule and logistic ran
full-history (1912), the three confusion matrices summed to different totals,
which read as inconsistent. Fixed by splitting the panel in two, each with a
single shared day set:

- **`live`** — the deployed models over the whole season history (~1912 days):
  the rule + the distilled logistic (`ml_coeffs.py`, all-years).
- **`holdout`** — a like-for-like three-way head-to-head on the ≥2023 test era
  (715 days), all genuinely out-of-sample: the rule + the bundle's **≤2022**
  logistic and HGB. Reproduces the research ranking — **HGB +0.275 > logistic
  +0.233 > rule +0.149** (binary Peirce).

Implementation: `build_payload` does two `compile_report` walks (full + ≥2023)
and adds a `holdout` block; `_model_payload` gained a `scorer` hook to run a
bundle model on the fly; `hgb_shadow.classify_bundle(which, …)` generalises the
loader to `hgb`|`logistic` (with `classify_hgb` a thin wrapper). The template
uses one `stat_block` macro for all five cards. The `live` panel uses the
all-years logistic (what's deployed); the `holdout` panel uses the bundle's
≤2022 logistic so it's held out like HGB — a deliberate difference.
