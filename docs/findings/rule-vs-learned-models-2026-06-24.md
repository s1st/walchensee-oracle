# Rule vs. learned models on the official session label — and two methodology bugs that nearly fooled us (2026-06-24)

**Branch:** none (analysis only).
**Trigger:** the live forecast called GO for 2026-06-24, the thermal underdelivered;
Andy (Addicted-Sports) asked (1) was that miss bad *inputs* or bad *rules*, and
(2) under which regime is the rule vs. the learned model more reliable.

> ⚠️ **This doc supersedes an earlier same-day draft (`rule-vs-ml-regime-split-…`,
> deleted) whose headline conclusions were WRONG.** Two methodology bugs inflated
> them; both are fixed here. Read §0 before trusting any earlier number.

## §0 — The two bugs (so we never repeat them)

1. **Wrong ground-truth label.** The first pass scored against
   `ground_truth.machine.samples_above_12kt ≥ 1` ("touched 12 kt once"). That field
   is a *frozen reporting counter* (`_SESSION_KT=12`), deliberately decoupled from
   the forecaster, and it is **not** the project's session definition. The official
   label is `calibration.actual_verdict_thermal` → **GO = ~1 h sustained ≥ 11 kt
   avg (`_DURATION_GO_KT`/`_DURATION_GO_SAMPLES`), gated for thermal character**
   (onset in the ignition window + gust coherence, de-contaminating foehn/frontal).
   Base GO rate: **28%** (a rare event), not the 53% the loose label implied.
   **Always score against `actual_verdict_thermal`.**
2. **Corrupt stored HGB block.** The `hgb_classifier` blocks stored in
   `runs/replay/` were backfilled with the *old go/no_go-swapped mapping* (the bug
   fixed in `hgb_shadow.py` on 2026-06-21, but the replay archive was never
   re-backfilled). Reading stored `P(go)` is actually `P(no_go)`. **Never trust the
   stored block — recompute live via `oracle.hgb_shadow.classify_hgb`.** Proof:
   stored `P(no_go)` AUC (0.801) == live `P(go)` AUC (0.801), exact mirror.

## §1 — The 2026-06-24 miss: bad rules, not bad inputs (unchanged, still valid)

Forecast meteo matched or under-sold reality (air–lake delta observed +10.5 °C vs.
forecast +5.2; afternoon dry, sunny). The thermal **ignited** (≥ 8 kt from 10:29,
gusts 12–14) but **stalled at ~11 kt avg, never sustained session strength**. Not an
input/divergence failure — a **strength-grading failure**: the rules emit a binary
GO once thresholds clear and cannot say "favorable but only ~11 kt." The shadow ML
called `no_go`, grading the strength the rules can't.

## §2 — Model comparison, official label (the real result)

Fair basis. The shipped logistic (`ml_classifier`, frozen, full-replay-trained) and
the bundle HGB (year-blocked ≤ 2022) compared on the **2023+ holdout** (both
out-of-sample), in-season, official thermal label, threshold-free:

| model | AUC | best Peirce | vs rule |
|---|---|---|---|
| 14-rule | ~0.58 | +0.12–0.16 | baseline |
| **logistic ML** | **0.62–0.68** | **+0.22–0.26** | clearly better |
| **HGB (bundle, holdout)** | **0.63** | **+0.23** | clearly better, ≈ logistic |

- **Both learned models clearly beat the rule, and are roughly tied with each other**
  (HGB a hair ahead on the true both-holdout cut: AUC 0.632 vs 0.619). Consistent
  with the original ceiling spike (HGB ≳ logistic).
- **No regime ensemble.** On the official label the logistic beats the rule in
  *every* gradient regime (incl. strong-favor, where the rule's earlier "win" was a
  loose-label artifact). The "complementary failure modes / switch on the gradient"
  story from the deleted draft **does not survive the correct label** — the ensemble
  (`ens_ml`) scores *below* the logistic alone. Drop it.
- **Cheap `thermik` HARD-at-≤-3 fix:** only marginal on the official label
  (+0.120 → +0.141). Not the big lever it looked like on the loose label.
- ⚠️ `scripts/ensemble_compare.py`'s in-line HGB column reads ~+0.44 because 2021–22
  are **in-sample** for the bundle HGB. Use the §2 holdout numbers, not that.

## §3 — HGB rehabilitation + a live production bug

The HGB is **not** weak (the deleted draft's "−0.27, anti-correlated" was swapped
stored data, see §0.2). Recomputed live it is a top model (§2).

**Production impact (needs fixing):** `stats_cache.py` (holdout head-to-head) and
`dashboard/main.py` (day-detail card) both **read the stored `hgb_classifier`**, so
the live dashboard currently shows the **inverted** HGB. New live forecasts are fine
(`logger.py` computes it live with the fixed mapping); only the **backfilled archive
is swapped**. Fix:
```
RUNS_BUCKET=walchi-oracle-prod-runs oracle hgb-backfill --replayed   # replay corpus
RUNS_BUCKET=walchi-oracle-prod-runs oracle hgb-backfill --since <PROJECT_FIRST_DAY>
RUNS_BUCKET=walchi-oracle-prod-runs oracle stats-update
gcloud run services update walchi-oracle-dash --region europe-west1 --update-env-vars STATS_CACHE_BUST=<sha>
```
(Needs the `[ml]` extra for the backfill. Local analysis scripts now recompute live,
so they're immune regardless.)

## §4 — Heat features, corrected: a real one emerged

Re-run on the official label:
- **Hot-day streak** (≥28 °C consecutive) — **dead** (Peirce ≈ 0), as before.
- **Absolute warm-night / overnight min** (true DWD Jachenau-Obernach low, 00–07 h) —
  **dead** (no monotone signal).
- **Day-night temperature range** (true: DWD daytime max − overnight min) — **ALIVE
  and additive.** Monotone: range < 8 °C → only **12%** GO vs. 27% base; range 20+ →
  43%. Standalone **AUC 0.65** (≈ the whole logistic), and **combining with the ML
  lifts AUC 0.647 → 0.672** — signal the ML doesn't already carry. This vindicates
  the *core* of the windinfo lore ("big day–night swing = good thermal"), just not
  "long warm spell" or "warm night per se."
  - **Prototype retrain (`scripts/range_feature_prototype.py`), the honest test:**
    binary GO-vs-rest logistic, train ≤2022 / test 2023+, official label. Adding the
    range moves **holdout AUC 0.595 → 0.605 (+0.010), best Peirce +0.208 → +0.225**.
    So **real but marginal** — the crude z-avg additivity (0.647→0.672) over-sold it;
    in the full model the range partly overlaps cloud/solar. And this uses the
    **observed** DWD range = the **value ceiling**; production would use the noisier
    Open-Meteo `temperature_2m_max/min` *forecast*, so the real gain is ≤ +0.01 AUC.
  - **Verdict:** not a clear ship for ~+1pp AUC vs. the cost (two new Open-Meteo vars,
    retrain/export, forecast-vs-observed drift). Document as "real but marginal".
  - **Don't misread the +0.01 as "windinfo was wrong about the temp delta."** It
    wasn't — the day-night swing is a genuine predictor (standalone AUC 0.65, small
    swing → 12% GO vs 27% base). The marginal model gain means the signal is largely
    **redundant** with features already present (clear-sky high pressure drives *both*
    a big diurnal swing *and* high solar / low cloud). Delta claim = **confirmed**,
    just not additive. What windinfo got *wrong*: the "long warm spell weakens it" and
    "warm night per se is bad" elaborations — both **dead** (no signal; warm-night even
    runs slightly the other way, confounded by season).
- DWD Jachenau-Obernach (station 02660) is **precip-only** — pull temperature via
  Bright Sky **lat/lon** merge (supplies a temp station ~1.8 km, back to 2016).

## §4b — Side finding: `max_boundary_layer_height_m` is an era-restricted ML feature (latent train/serve issue)

Surfaced by the range prototype (sklearn warned BLH had no observed values in the
≤2022 training fold). Coverage of `max_boundary_layer_height_m` in the replay inputs:
**2017–2023 = 0 non-null** (absent from the Open-Meteo historical-forecast archive),
2024 ≈ 33%, 2025 ≈ 94%, 2026 (live) full.

- The bundle models (train ≤2022) saw BLH **all-NaN → median-imputed to a constant →
  learned nothing** from it. Dead feature there.
- The shipped logistic (`ml_classifier`, full-replay) learned its BLH coefficient from
  a **coverage-skewed subset (2024–25 only)** and applies it live where BLH is always
  present — a genuine **era-boundary train/serve inconsistency**. Mitigating: BLH isn't
  a top contributor (small coef, learned off a near-constant feature), so practical
  impact is likely small.
- Note the mismatch: CLAUDE.md says the ML uses "11 ICON-stable features (8 era-only
  features dropped to avoid exactly this shift)", but the model actually carries **13**,
  including the two era-restricted ones — **BLH (≥2024)** and **CAPE (≥2021)**. These
  belong in the dropped category.
- **Recommendation:** drop BLH (and reconsider CAPE) from the ML feature set and
  retrain — cleaner than an era-skewed coefficient. Low urgency (small effect), but a
  real hygiene fix; possibly a bigger lever than the range feature.

## §5 — Reproducible scripts (all on the official label, HGB recomputed live)

`scripts/ensemble_compare.py` (rule/fix/ML/HGB + regimes), `hgb_fair_compare.py`
(AUC/best-threshold), `hgb_live_verify.py` (proves the stored-block swap),
`heat_feature_validation.py` + `jachenau_night_feature.py` (heat features),
`range_feature_prototype.py` (range retrain test, §4). Run with the project venv
(`.venv/bin/python …`) — calibration pulls `httpx`; the HGB needs the `[ml]` extra.
`regime_rule_vs_ml.py` was deleted (loose-label, debunked).

## §6 — Recommendations / open actions

1. ✅ **Prod HGB fixed** (2026-06-24): `hgb-backfill --replayed` (3331) + `--since
   2026-04-22` (64) on `RUNS_BUCKET`, `stats-update` (n=1980), dashboard redeployed
   (rev 00115-hx2, bust `hgbfix-88519da-2343`). Stored block verified = live.
2. **Drop the regime-ensemble idea** — label artifact.
3. **Day-night-range feature: real but marginal (+0.01 holdout AUC, §4)** — don't ship
   for that alone.
4. **BLH feature-hygiene fix (§4b)** — drop `max_boundary_layer_height_m` (reconsider
   CAPE) from the ML and retrain; possibly a bigger lever than the range. Also fix the
   CLAUDE.md "11 features" vs. actual 13 drift.
5. The `thermik` HARD-at-≤-3 fix is optional/marginal.
6. Headline for any future writeup: on the official label, the learned models beat
   the 14-rule cleanly and uniformly; the open work is feature hygiene (§4b) and
   whether to promote the shadow — not an ensemble.
