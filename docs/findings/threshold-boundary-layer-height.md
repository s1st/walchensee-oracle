# Threshold tuning: `MIN_BOUNDARY_LAYER_HEIGHT_M` — 2026-06-12

Working notes for the **sixth** threshold tune under the
replay-calibration plan (Phase 3). Sibling: threshold-solar-radiation.md,
threshold-daytime-clouds.md, threshold-synoptic-override.md,
threshold-upper-level-wind.md, threshold-foehn-trigger.md. Same
discipline: one threshold per commit, n= note, rescore +
calibrate + live-era check.

## Question

The `boundary_layer_height` rule fires `NO_GO` (SOFT severity)
when `max_boundary_layer_height_m < MIN_BOUNDARY_LAYER_HEIGHT_M`.
Current value is 600 (research-analogue guess). The plan: "re-fit
on the n=3,263 sample, ICON-era only since pre-2021 archive
doesn't carry BLH".

## Data

`max_boundary_layer_height_m` is **only available for the
ICON-era (2022-11-24+)** — same caveat as the synoptic and
foehn tunes. n=629 ICON-era days.

```
max_boundary_layer_height_m distribution (n=629)
  mean   789 m
  median 710 m
  min     35 m
  max   2370 m

Fire rate by BLH bucket:
   0-  250 m    114 days    28% fire   ← rule should fire here
 250-  500 m    117 days    50% fire   ← borderline
 500-  750 m     95 days    53% fire
 750- 1000 m     87 days    55% fire
1000- 1500 m    142 days    47% fire
1500- 2000 m     67 days    39% fire   ← counter-intuitive dip
2000- 3000 m      7 days    14% fire
```

The 0-250m range has a clearly lower fire rate (28%) — the
rule's signal is real for "really shallow BLH". Above 250m, fire
rate is roughly 50% (neutral). Above 2000m, fire rate drops
again (14%) but the sample is tiny (7 days).

## The rule-level sweep

For threshold X, the rule says `NO_GO` when `BLH < X`.

```
 threshold | N_C (rule right) | N_T (rule wrong) | N_C − N_T
 ----------+------------------+------------------+---------
    100 m  |               14 |                0 |      +14
    200 m  |               61 |               16 |      +45
    300 m  |               99 |               46 |      +53
    400 m  |              127 |               70 |      +57   ← new
    500 m  |              140 |               91 |      +49
    600 m  |              162 |              109 |      +53   ← current
    700 m  |              179 |              132 |      +47
    800 m  |              194 |              147 |      +47
    900 m  |              209 |              167 |      +42
   1000 m  |              224 |              189 |      +35
   1500 m  |              299 |              256 |      +43
   2000 m  |              340 |              282 |      +58
   3000 m  |              346 |              283 |      +63
```

Peak at X=400 with N_C − N_T = +57 (vs +53 at 600). Plateau
at +50 to +60 across 200-3000m. The "absolute" peak is at 3000
(rule always fires, +63) but that's "rule does nothing different
from baseline".

## Δ when moving 600 → 400

The 400-600m band has 74 days. Of those, ~50% fire rate. Adding
the veto to those 74 days:
- 37 fired (rule wrong, +wrong): -37 right
- 37 didn't fire (rule right, +right): +37 right
- Net: 0 (with maybe a +4 from the rule-level sweep)

Empirically: 0pp change at the headline. Same redundancy
pattern as the foehn tune — the rule's contribution is
dominated by how the aggregator handles it, and the BLH
verdict is already well-covered by other rules (overnight
cooling, daytime_clouds, etc.).

## Why 400 and not 300 or 500

- 400 is the data-fitted peak in the "low BLH" range (the
  intent of the rule: catch the clearly-shallow days)
- 300 would be slightly more aggressive (net +53, smaller
  sample) but the difference is within noise
- 500 is +49 (worse than 400)
- The 200-2000m plateau is +50-60; any choice in that range
  is essentially equivalent at the rule level

The "absolute" peak at 3000 is "rule always fires" which
loses the BLH-specific veto. 400 keeps the intent.

## Δ when moving 600 → 400

Before this tune: 42% / 57%.
After: 42% / 57%.
No change at the headline.

Live era: 57% unchanged (47-day sample doesn't move with +4
days at the rule level).

The rule is **redundant with other rules at the aggregator
level**. Other rules' verdicts already cover the BLH signal:
shallow BLH days tend to also have other thermal-suppressing
signals (high cloud cover, low solar, etc.), so the
aggregator's verdict is no_go via those other rules even
without the BLH rule's veto.

## Why I committed this anyway

Two reasons:

1. **The data-fitted value is 400, not 600.** Even if the
   empirical effect is zero, the constant should reflect
   what the data says. Future contributors (or the next
   re-fit in a year) shouldn't have to re-derive that
   600 was a guess.

2. **The rule is more focused at 400.** It fires on
   "clearly-shallow" mornings (0-400m), which is what the
   rule was meant to catch. At 600 it was firing on the
   borderline 400-600m band where the fire rate is 50%
   (no real signal).

## File-rotation policy

This file documents the tune. The 400-m value stays in
`config.py` — the empirical 0pp change is the headline; the
400-m value is the cleaner data-fitted intent.

Next per the plan: `WET_SOIL_MOISTURE_M3M3` (0.35) and
`COLD_LAKE_DELTA_C` (10). Both are ICON-era only.
