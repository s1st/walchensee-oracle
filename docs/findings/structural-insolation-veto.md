# Structural fix: the missing NO_GO comes from insolation, not foehn — 2026-06-13

Phase 4 pivot (user call, 2026-06-13): the bar/threshold sweep hit a low skill
ceiling because the ruleset structurally can't produce NO_GO on non-thermal days
(NO_GO is bar-invariant — see `aggregator-bar-recalibrated.md`). This diagnoses
*where* NO_GO skill can come from. Scored on the thermal label, in-season
replay, n=1912 (520 GO / 851 NO_GO / 541 MAYBE).

## Which features separate thermal from non-thermal days?

Rank AUC for discriminating thermal-NO_GO (GO vs NO_GO, MAYBE dropped):

| feature | AUC | GO median | NO_GO median |
|---|---|---|---|
| max_daytime_low_cloud_pct | **0.683** | 11% | 62% |
| overnight_cloud_cover_pct | 0.663 | 52% | 84% |
| thermik_delta_hpa | 0.653 | −1.75 | −0.90 |
| morning_solar_radiation_wm2 | 0.344 | 685 | 548 |
| min_dew_point_spread_c | 0.381 | 3.2 | 2.1 |
| **foehn_delta_hpa** | **0.391** | −1.10 | −1.90 |
| max_wind_700_knots | 0.593 | 10.3 | 13.8 |

The non-thermal days are **cloudy, low-solar, weak-thermik-gradient days** — not
foehn days. `foehn_delta` barely separates (AUC 0.39) and in the "wrong"
direction. This corrects *both* the prior pass ("foehn premise inverted") and
the Fable review's §1/§6 emphasis on foehn: foehn simply isn't the dominant
non-thermal mode at Walchensee in season. **Cloud and solar are.**

This is doubly damning for the prior pass, which *loosened* exactly these
signals on the contaminated metric: solar 600→380, daytime cloud 60→75. Those
are the strongest non-thermal discriminators, and they're SOFT vetoes the bar=5
aggregator almost never surfaces.

## A "no sun → no thermal" veto recovers real skill

Added a HARD NO_GO when `daytime_low_cloud ≥ 50% AND morning_solar ≤ 450 W/m²`
(physically: no insolation → no thermal), simulated by injecting the veto and
re-aggregating `verdicts_resimulated`:

| | Peirce | cost | McNemar vs baseline |
|---|---|---|---|
| bar=2 baseline | +0.020 | 0.502 | — |
| bar=2 **+ veto** | **+0.115** | 0.500 | fixed 263, broke 81, net +182, **p=1.7e-22** |
| bar=5 baseline | +0.006 | 0.580 | — |
| bar=5 **+ veto** | **+0.119** | 0.526 | fixed 263, broke 56, net +207, **p=8.9e-31** |

Era-stable (the bar tune was not): IFS +0.039→+0.142, ICON −0.012→**+0.071** at
bar=2; both eras improve at bar=5. Unlike every threshold/bar tune in this and
the prior pass, this is a large, highly significant, out-of-era-stable gain — a
~6× Peirce improvement — because it adds the HARD NO_GO the architecture lacked.

## Honest caveats

- **Cost vs skill diverge at bar=2:** Peirce soars but mean cost is ~flat
  (0.502→0.500) because the veto's wrongly-vetoed GO days (missed sessions, 2×
  weight) offset the wasted-drive savings. At bar=5 cost clearly improves
  (0.580→0.526). A shipped veto should be **cost-tuned** (how aggressive) and
  **holdout-validated**, not fixed at the in-sample C=50/S=450 grabbed here.
- The C/S thresholds are in-sample to the thermal label; the *effect size* and
  era-stability make it clearly real, but exact thresholds need a holdout fit.
- The thermal label has its own physics-set gates (gust 2.2, onset window); the
  cloud/solar separation is robust to those, foehn is not the lever regardless.

## Recommendation

1. **Implement a combined insolation veto** (`no_insolation`: heavy daytime
   cloud + low morning solar → NO_GO). This is the single highest-leverage
   change found in either pass. Pick its thresholds on a year-holdout against
   the cost metric, decide HARD vs SOFT by the missed-session tradeoff, wire
   into `engine.run_forecast`, test, and surface in the dashboard panel.
2. **Pair it with reverting the bar toward 2–3** (restores MAYBE hedging; the
   veto, not the bar, supplies NO_GO).
3. **Don't pursue the per-threshold soft-veto sweep** — proven within-noise.
4. **ML (GH #12):** cloud/solar/thermik-delta dominating a clean AUC ranking is
   exactly the signal a shallow tree would find. The thermal label + this
   feature ranking are the prerequisites; an ML pass is now well-motivated.
