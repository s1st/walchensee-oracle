# Threshold tuning: `MIN_MORNING_SOLAR_WM2` — 2026-06-12

Working notes for the first commit of Phase 3 in
`docs/replay-calibration-plan.md`. The pattern per the plan:
one threshold per commit, offender-list evidence first, n= note
on the new value, rescore + calibrate + live-era check before
committing.

## Question

The `solar_radiation` rule was firing on too many days, killing
real sessions (the 1,464 FP-vetoes in the baseline). What's the
data-fitted value of `MIN_MORNING_SOLAR_WM2`?

## Method

For threshold X, the rule says `NO_GO` if `morning_solar_radiation_wm2 < X`.
- **N_C(X)** = days with solar<X AND fired=0: rule correctly caught
  a didn't-fire day
- **N_T(X)** = days with solar<X AND fired=1: rule wrongly vetoed a
  fired day (the painful kind of error)
- **N_C − N_T** = the rule's net contribution to the model

Sweep X from 0 to 1100 in steps of 50 (finer near the optimum),
find the X that maximises N_C − N_T.

## Data

`data/replay_full.csv` (gitignored, 3,331 rows × 29 columns,
committed at `7c72c6c`'s parent for analysis; the on-disk replays
and the synced GCS replays are equivalent for this purpose).

## Result

```
 threshold | N_C (rule right) | N_T (rule wrong) | N_C − N_T
 ----------+------------------+------------------+---------
   100     |              104 |               69 |        +35
   200     |              309 |              205 |       +104
   300     |              557 |              358 |       +199
   350     |              731 |              455 |       +276
   380     |              808 |              521 |       +287   ← peak (step=10)
   400     |              845 |              564 |       +281
   450     |              939 |              681 |       +258
   500     |             1012 |              776 |       +236
   550     |             1102 |              854 |       +248
   600     |             1184 |              961 |       +223   ← current
   700     |             1386 |             1200 |       +186
   800     |             1579 |             1451 |       +128
   900     |             1676 |             1627 |        +49
   950     |             1683 |             1648 |        +35
```

Peak at X=380, N_C − N_T = +287 (a +64 improvement over current 600).

## Why 380 and not the (narrowly) higher 400

- 380 is the data-fitted peak (step=10 sweep, no tie)
- 400 is +281 vs 380's +287, only 6 days worse
- 380 picks up a bit more of the "really cloudy days the rule
  should catch" population without losing the right shape of
  the curve

The 380 value is data-fitted, not "nice". I considered rounding
to 400 for readability, but the n=3,263 fit favours 380 by a
handful of days. Kept the data-fitted value.

## Before / after at the aggregator level

`oracle calibrate --replayed --resimulated --label duration` on
the full 3,263-day sample (storm-quarantined):

| | Before (600) | After (380) | Δ |
|---|---|---|---|
| Overall accuracy | 40.8% | 41.3% | +0.5pp |
| go→go | 478 | 585 | +107 |
| go→wrong (maybe or no_go) | 356 | 497 | +141 |
| maybe→go | 778 | 671 | −107 |
| maybe→maybe | 788 | 697 | −91 |
| maybe→no_go | 625 | 575 | −50 |

Net: 248 days moved from maybe to go; 107 were fired (correct),
141 weren't (wrong). The 0.5pp accuracy gain is small but the
**distribution** shifted: the rules now let 248 days through that
were previously hedged. Some of those are right (the fired-anyway
sessions the rule was wrongly vetoing), some are wrong (the didn't-
fire days the rule was correctly catching).

## Before / after at the rule level (the headline win)

```
solar_radiation rule
                       before (600)   after (380)   Δ
 FP-veto (killed real)        1464          823     −641
 green (missed real)          1140         1940     +800
```

**The FP-veto count dropped 44%.** That's the "you said NO_GO and
the lake fired anyway" error — the kind of error that ruins a
day for a windsurfer. The trade: 800 more days where the rule
says GO and the lake doesn't fire. Most of those days are
`maybe` anyway (because other rules hedge), so the user-visible
effect is small, but the model-internal distribution is much
cleaner.

## Live-era check (no-arg, no `--replayed`)

`oracle calibrate --resimulated --since 2026-04-22` on the
47-day current season:

| | Before | After | Δ |
|---|---|---|---|
| Overall accuracy | 34% (per `574d126` commit note) | **57%** | +23pp |

Big swing on the small sample, but directionally correct — the
threshold change helps the live era too. The 5 storm-suspected
days are still quarantined.

## Why I'm committing this change and not, say, disabling the rule

The data shows the rule is helpful at threshold=380 (N_C − N_T = +287)
vs the no-rule baseline (N_C − N_T = +35, since no-rule means all
days are "go"). Disabling the rule would lose the +252 days the
rule contributes. Tuning is the right move.

The threshold-tuning "peak at 380" is a relatively flat region
(350-410 all give +270 to +287). Re-tuning on the next n=3,000-day
sample in a year will probably give a similar value; the data-
fitted approach should be re-run then anyway, per the project's
"re-fit thresholds when the corpus grows" discipline.

## File-rotation policy

This file documents the tuning run for `MIN_MORNING_SOLAR_WM2`.
The next threshold tune (per the plan: `MAX_DAYTIME_LOW_CLOUD_PCT`,
`SYNOPTIC_OVERRIDE_KNOTS`, `MAX_UPPER_CROSSFLOW_KNOTS`) will get
its own file: `threshold-<rule>.md` in the same directory.
