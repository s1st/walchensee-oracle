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

## Shipped (2026-06-13, commit abd6948)

1. **`no_insolation` HARD veto** implemented: `daytime_low_cloud ≥ 70% AND
   morning_solar ≤ 400 W/m² → NO_GO`. Thresholds fit on a temporal holdout
   (train ≤2022 / test ≥2023) by minimising cost; HARD because no-sun
   physically precludes a thermal. Wired into `engine.apply_rules`, tested,
   surfaced in the dashboard panel + tooltip.
2. **`SOFT_VETO_BAR` reverted 5 → 2** (restores MAYBE hedging; the veto, not the
   bar, supplies NO_GO).
3. **End-to-end confirmation** (rescore + `calibrate --label thermal --replayed
   --resimulated`, in-season n=1912): Peirce +0.006 → **+0.107**, cost 0.580 →
   **0.491** (now below the cheapest constant, 0.495), era-stable
   (IFS +0.132 / ICON +0.066), McNemar vs as-written **p≈0**. Matches the
   holdout prediction.

## Not pursued / next

- **Per-threshold soft-veto sweep** — dropped: proven within-noise.
- **ML (GH #12):** cloud/solar/thermik-delta dominating a clean AUC ranking is
  exactly the signal a shallow tree would find. The thermal label + this feature
  ranking are the prerequisites; an ML pass is now well-motivated.
- **Soil/BLH rules** still need an ICON-only re-replay before any re-tune (the
  IFS-pinned corpus nulls those fields — see soil-moisture-replay-nulls.md).
