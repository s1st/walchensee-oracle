# Threshold tuning: `MAX_DAYTIME_LOW_CLOUD_PCT` — 2026-06-12

Working notes for the **second** threshold tune under the
replay-calibration plan (Phase 3). First tune was
`MIN_MORNING_SOLAR_WM2` (`docs/findings/threshold-solar-radiation.md`).
Same discipline: one threshold per commit, offender-list evidence
first, n= note on the new value, rescore + calibrate + live-era
check before committing.

## Question

The `daytime_clouds` rule fires `NO_GO` when
`max_daytime_low_cloud_pct > MAX_DAYTIME_LOW_CLOUD_PCT`. The
current value is 60 (research-analogue guess, no n= note). What's
the data-fitted value?

## Data

`data/replay_full.csv` (gitignored, 3,331 rows × 29 columns, same
source as the `solar_radiation` tune).

`max_daytime_low_cloud_pct` distribution is **strongly bimodal**:

```
  cloud 0-10%  : 1,138 days  (mostly sunny)
  cloud 10-30% :   402 days  (mostly clear, sparse middle)
  cloud 30-50% :   298 days  (borderline)
  cloud 50-60% :   134 days
  cloud 60-70% :   123 days  ← current NO_GO trigger band
  cloud 70-80% :   116 days
  cloud 80-90% :   134 days
  cloud 90-100%:   242 days
  cloud = 100% :   744 days  (fully overcast — common winter morning)
```

1,138 days are under 10% (sunny); 744 days are exactly 100%
(overcast). The "borderline" band (10-90%) has only ~1,200 days
across 9 years. The rule's contribution in this band is small
either way, because there aren't many days to score on.

## Method

For threshold X, the rule says `NO_GO` if
`max_daytime_low_cloud_pct > X`. (Strictly greater than, per the
rule's `if pct > config.MAX_DAYTIME_LOW_CLOUD_PCT`.)

- **N_C(X)** = days with cloud > X AND fired=0 (rule correctly
  caught a didn't-fire day)
- **N_T(X)** = days with cloud > X AND fired=1 (rule wrongly
  vetoed a fired day)
- **N_C − N_T** = the rule's net contribution to the model

Sweep X from 0% to 100% in steps of 1 (focused on 65-80% where
the optimum is).

## Result

```
 threshold | N_C (rule right) | N_T (rule wrong) | N_C − N_T
 ----------+------------------+------------------+---------
   60%     |              739 |              608 |     +131   ← current
   65%     |              708 |              572 |     +136
   66%     |              704 |              566 |     +138
   67%     |              695 |              557 |     +138
   68%     |              693 |              555 |     +138
   69%     |              687 |              549 |     +138
   70%     |              678 |              541 |     +137
   75%     |              658 |              519 |     +139   ← new
   80%     |              613 |              492 |     +121
   85%     |              565 |              452 |     +113
   90%     |              548 |              430 |     +118
  100%     |                0 |                0 |       +0
```

Peak at X=75, N_C − N_T = +139. The 66-69% plateau is tied at
+138 — within rounding noise of the peak. The improvement over
the current 60% is **+8 days**, modest compared to the
`solar_radiation` tune's +64.

## Δ when moving from 60 → 75

```
  At 60%: rule fires on 1,347 days (739 right, 608 wrong)
  At 75%: rule fires on 1,177 days (658 right, 519 wrong)

  Rule fires on 170 fewer days.
    Saved wrong vetoes:   89
    Lost correct vetoes:  81
    Net:                 +8
```

## Why 75 and not 70 (a round number)

- 75% is the data-fitted peak. 70% gives +137, only 2 days worse.
- Both are within rounding noise of each other. The 66-69%
  plateau is +138, also within noise.
- I picked 75 because the docstring wants a single number and
  the data-fitted value is 75. If the next re-fit (in a year,
  on the n≈5,000 sample) shifts the optimum by 2-3%, this
  number is easy to nudge.

## Before / after at the aggregator level

`oracle calibrate --replayed --resimulated --label duration` on
the full 3,263-day sample (storm-quarantined):

| | After `solar_radiation` tune | After `solar_radiation` + `daytime_clouds` tunes | Δ from prior |
|---|---|---|---|
| Overall accuracy | 41.3% | **42.0%** | +0.7pp |
| go→go | 585 | 609 | +24 |
| go→wrong (maybe or no_go) | 497 | 515 | +18 |
| maybe→go | 671 | 647 | −24 |
| maybe→maybe | 697 | 685 | −12 |
| maybe→no_go | 575 | 569 | −6 |

Net: 24 days moved from maybe to go; 24 were fired (correct go→go),
6 weren't (the maybe→no_go shift in the wrong direction). Tiny but
directionally correct.

## Before / after at the rule level

```
daytime_clouds rule
                       before solar tune   after solar tune   after solar + clouds tune
 FP-veto (killed real)        968                968                  845
 green (missed real)         1496               1496                 1496
```

Wait, why did `daytime_clouds` FP-veto not move after the
`solar_radiation` tune? Because the solar tune touched the
`solar_radiation` rule only; it didn't affect the daytime_clouds
rule's verdicts. The day-to-day confusion-matrix moved, but the
per-rule stats didn't.

Comparing apples to apples: 968 → 845 is the actual
`daytime_clouds` rule change. **−123 FP-vetos (−13%)**. The rule
fires on 170 fewer days (per the Δ above), and the wrong-veto
days that drop out (89) are the days that were above 60% but
≤ 75% — borderline-cloud days that actually fired.

## Live-era check

`oracle calibrate --resimulated --since 2026-04-22` on the
47-day current season: 57% accuracy (unchanged from after the
`solar_radiation` tune — the small sample doesn't move with
an +8-day improvement at the rule level).

The 5 storm-suspected days are still quarantined.

## Why the improvement is so small

Three reasons, in order of importance:

1. **The cloud distribution is bimodal** — most days are
   either very clear (cloud<10%, 1,138 days) or fully overcast
   (cloud=100%, 744 days). The "borderline" band where the
   threshold matters (cloud 30-90%) has only ~1,200 days across
   9 years. The rule fires correctly or incorrectly mostly on
   these ~1,200 days, and the rest of the days the rule either
   doesn't fire (clear) or fires unconditionally (overcast).
2. **The 60% value was already close to optimal.** The optimum
   is at 75%, only +8 days of net improvement. The solar tune
   had +64 because the data range was wider and the
   over-tuning was larger.
3. **Other rules catch the borderline days too.** A day at
   60% low cloud is likely also borderline on solar radiation
   and dew-point spread. The aggregator's "2-soft-veto → MAYBE"
   logic already hedges these days. The daytime_clouds rule
   alone is less decisive than it looks in isolation.

## Open question — should we keep the 3-band structure?

The rule has 3 bands (`>X` = NO_GO, `<30` = GO, otherwise
MAYBE). The `GOOD_DAYTIME_LOW_CLOUD_PCT = 30` is also a
research-analogue guess. The data-fitted analysis for the
GO band would be: what's the X where days with cloud<X are
most clearly fired?

Quick check from the baseline:
```
  cloud 0-10%:  fire rate 51.1%  (rule says GO)
  cloud 10-20%: fire rate 55.4%
  cloud 20-30%: fire rate 53.8%
  cloud 30-40%: fire rate 55.3%  (rule says MAYBE here)
  cloud 40-50%: fire rate 57.0%
  cloud 50-60%: fire rate 47.0%
```

The "GO" band (cloud<30) is barely more "fired" than the
"MAYBE" band (30-50). A more honest rule might say "GO" only
when cloud<10 (or just always maybe for any cloud<60).

This is a follow-up: changing the structure of the rule (3-band
→ 2-band, or re-tuning the MAYBE thresholds) is a larger
change than "one threshold per commit". Parked for after the
threshold pass completes.

## File-rotation policy

This file documents the tuning run for `MAX_DAYTIME_LOW_CLOUD_PCT`.
Next threshold tune (per the plan: `SYNOPTIC_OVERRIDE_KNOTS`)
will get `threshold-synoptic-override.md` in this directory.
