# Review of the 2026-06-12 threshold-tuning pass — Fable findings

External review (Claude Fable 5, 2026-06-12) of the nine-change threshold
pass on the `threshold-tuning` branch and the replay-calibration work on
`main`. Companion to `docs/2026-06-12-historical-calibration-findings.md`
(the pass's own summary) and the per-tune notes in `docs/findings/`.

## Verdict

The infrastructure is genuinely good — the replay loop, the
one-constant-per-commit discipline, the honest revert of the crossflow
tune, working notes that keep the mistakes in. But the analysis has one
systemic flaw that likely invalidates the pass's three headline
"the rule's premise is wrong" findings, the objective metric is wrong
(the tuned system scores *below* a trivial always-GO baseline on the
very metric it optimized), and the sequencing of the nine changes
invalidates the first eight.

---

## 1. The ground-truth label measures "wind", not "thermal" (critical)

`actual_verdict` / `actual_verdict_duration` bucket by Urfeld wind
alone (`src/oracle/calibration.py:52`). Any wind source counts as
"fired": foehn storms, frontal passages, winter gradient wind. The
storm quarantine is lifted-index-based — it catches convection, not
foehn (dry, stable, high LI), and it is blind pre-2021 entirely. The
plan even warned: "check a few offender days by hand before blaming a
threshold". No tune did the hand-check.

What fell out of the contaminated label:

- **Foehn "100% fire rate at Δ 6–8 hPa"** (`threshold-foehn-trigger.md`)
  — foehn *is* strong southerly wind at Walchensee. The anemometer
  firing on a foehn day is the foehn itself, not a thermal the rule
  wrongly vetoed. The rule's premise was never "foehn days are
  windless"; it's "foehn wind is not the thermal" (and here, it's the
  dangerous wind). "Premise contradicted by data" is a labeling
  artifact, not a physical finding.
- **Cold-lake delta "inverted"** (`threshold-cold-lake-delta.md`) —
  air−water delta is maximally positive on warm dry advection days
  (incl. foehn) and correlates with season. The monotonic
  fire-rate-vs-delta table is exactly what a season/foehn confound
  produces. Same artifact.
- **The "outrageous misses" justifying the solar tune** — 2017-12-14
  (December, 28 kt, solar 147, dew spread 2.3), 2020-02-10 (February,
  dew spread 0.0), 2019-11-15, 2020-10-03: winter/autumn days with
  near-zero dew spread peaking 25–30 kt. That profile is frontal or
  foehn wind, not a thermal. All pre-2021 — exactly where the
  quarantine is blind. The strongest evidence for 600 → 380 is
  contaminated.

**Fix:** the backfilled buoy records contain full sample curves
(direction, onset time, gust structure). Build a "thermal session"
label: wind in the thermal sector, onset inside the 10:30–15:00
ignition window, smooth avg/gust ratio. Expect the foehn and
cold-lake findings to flip back once the label is fixed.

## 2. ~40% of the tuning corpus is out of season (critical)

The project shuts down Nov–Mar, yet every sweep ran on all 12 months,
and winter dominates the negative class.

- **The soil-moisture tune (n=48) is purely a winter detector.** All
  48 days are from the ICON launch window — late Nov/Dec 2022. The
  "18% fire rate in the wet band" is the deep-winter base rate, not a
  wet-soil signal. The new 0.30 threshold fires on 43/48 of all
  observed days — a near-always-on soft veto fitted to five weeks of
  one winter. **Should be reverted, not caveated.**
- Solar at 380 W/m² and BLH at 400 m are substantially "is it winter?"
  classifiers — low solar and shallow BLH *are* winter. Univariate
  thresholds fitted year-round optimize toward season detection, not
  day-of skill within the season the product serves.
- Also: 0 non-null soil-moisture days in the 2023–2026 replay while
  the live API serves the field daily smells like a **replay fetch
  bug**, not "archive inconsistency". Check before trusting any soil
  data.

**Fix:** re-run every sweep on Apr–Oct only (rescore is seconds).
Expect different optima.

## 3. The objective metric is wrong, switched, and selectively redefined

- **3-class exact-match accuracy is beatable by a constant.** Peak-label
  actuals: go 1,648 / maybe 1,302 / no_go 381 (n=3,331) → "always GO"
  scores **49.5%**. The tuned system's celebrated 48.3% is *below*
  that. Use a skill score that zeroes out constant forecasts (Peirce /
  Heidke — standard forecast verification) or an explicit cost matrix
  (missed session vs. wasted drive).
- **The bar tune switched labels.** Thresholds were fitted on the
  duration label; `SOFT_VETO_BAR` on the peak label, justified with
  "duration doesn't lend itself to sensitivity analysis" — it does;
  rescore+calibrate was used as the empirical check throughout.
  Picking the label that shows +2.9pp over the one showing +1pp is
  metric shopping.
- **"Hard-error rate unchanged across all bars" is true only under a
  silently narrowed definition.** The baseline defined hard errors as
  go↔no_go *both directions*; `aggregator-bar.md` redefines it as
  NO_GO-on-fired-day only (bar-invariant by construction — NO_GO comes
  solely from HARD vetoes). The other direction exploded: at bar=2
  there were 569 forecast-MAYBE / actual-no_go days (the pass's own
  confusion matrix); at bar=5 essentially all become forecast-GO —
  several hundred new "told you to go, lake was dead" days, unreported.
- **The +1pp duration-label claim doesn't reconcile.** Flipping the
  bar-2 MAYBEs to GO using the published post-tune confusion (647
  actual-go become right, 685 actual-maybe become wrong, 569
  actual-no_go stay wrong) nets **−38 days**, not +33. Re-run and
  publish the post-bar confusion matrix.
- The MAYBE collapse (60% → 5% of days) trades honest hedging for
  accuracy points. For a forecast product, MAYBE is calibrated
  uncertainty, not an error to be optimized away.

## 4. Sequencing invalidates the eight threshold fits

All eight thresholds were fitted under `SOFT_VETO_BAR = 2`; the final
commit changed the bar to 5. Every soft-rule sweep's relationship to
the verdict — the thing tune #4 painfully demonstrated dominates the
outcome — was computed in a regime that no longer exists. Most
pointed: the crossflow revert happened *because* of bar=2
interactions; under bar=5 the 15 kt value might be fine.

Tune #4's own lesson ("N_C − N_T is incomplete, score against the
verdict") was stated and then not applied: tunes 5–8 continued
sweeping N_C − N_T with verdict-level results only as a post-hoc check
that never moved.

**Fix:** fix the aggregator first (dominant term), then re-sweep all
thresholds verdict-level under the new bar.

## 5. No holdout, no significance, a powerless safety check

- Everything is in-sample on the same 3,263 days. Year-wise CV or a
  2017–2023 fit / 2024–2026 test split was free and unused. The one
  era split computed (bar optimum: IFS=4, ICON=5) shows instability
  and went unremarked.
- No significance tests. Daytime clouds: 89 newly-right vs 81
  newly-wrong discordant days → McNemar χ² ≈ 0.38, **p ≈ 0.54**. BLH
  is +4 days. Both are noise committed as "data-fitted (n=3,263)" —
  the n is real, the evidence isn't.
- The live-era check (n=47) never moved across eight consecutive
  tunes. A check with zero discriminating power isn't a check.
- Selection criteria drift per tune: solar refuses to round 380→400
  ("data-fitted, not nice"), clouds picks 75 over a tied 66–69
  plateau, soil picks 0.30 off-peak on a narrative. Within-noise
  plateaus mean the data doesn't distinguish — say so uniformly.

## 6. Smaller but real

- **Safety:** with `FOEHN_TRIGGER_DELTA_HPA = 10` (max observed in 9
  years: 9.1) the dedicated foehn detector is dead. `upper_level_wind`
  (HARD, >25 kt at 700 hPa) catches many foehn storms, but moderate
  foehn at Δ 6–8 hPa now renders GO on the public dashboard.
  Reinstate on a corrected label before next season.
- **Model mismatch:** replay pinned to `ecmwf_ifs` 9 km; production
  consumes Best Match (ICON-D2, 2.2 km). Solar/cloud/BLH distributions
  differ between models, so thresholds were fitted on a different
  input distribution than they'll see live. Quantify by replaying the
  live era under both models.
- **Lead time:** replay ≈ lead-time-0 was flagged in the plan, never
  revisited when committing thresholds that run at day+1/+2.
- **Doc rot:** `config.py` still says "TODO(calibrate): no n= yet"
  directly above the refitted `COLD_LAKE_DELTA_C`; the
  `air_lake_delta` GO-side comment still states the premise the tune
  declared inverted; the main findings doc's bottom stats table is
  stale ("Threshold tunes shipped: 1"); `engine.aggregate`'s docstring
  embeds a full sensitivity table that will rot — point at
  `docs/findings/aggregator-bar.md` instead.

## What was genuinely good (keep)

- Replay + rescore infrastructure; the calibration join via
  `_merged_replay_record`; the batch-mode archive fetcher.
- One constant per commit with `n=` notes; the headline-check loop
  that caught the crossflow regression; the honest revert.
- The per-rule FP-veto table; noticing that a 60% MAYBE rate was
  excessive; the 2021/2022 anomaly exploration; small-n caveats where
  they existed.

## Recommended plan, in order

1. **Build a thermal-session label** from the buoy curves (direction
   sector + onset window + gust smoothness) and **restrict scoring to
   Apr–Oct**. Highest leverage; everything downstream is currently
   fitted to a contaminated target.
2. **Re-fit the aggregator bar first, then re-sweep all thresholds
   verdict-level** under the new bar, on the corrected label, with
   year-wise CV and a per-change significance test. Adopt a skill
   score or explicit cost matrix — never raw 3-class accuracy again,
   given always-GO beats the current system.
3. **Revert the soil tune**; re-examine foehn before the season;
   investigate the 2023–2026 soil-moisture nulls as a probable replay
   bug.
4. Keep the infrastructure and commit discipline as-is — they make
   redoing the analysis cheap.

## Note on the ML plan (GH issue #12)

Train a classifier on the current label and it will eagerly learn
"foehn means GO" and "winter means NO_GO". The label fix is a
prerequisite for both the ML approach and a fair answer to "how strong
can the rule-based model get".
