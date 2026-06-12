# Threshold tuning: `SYNOPTIC_OVERRIDE_KNOTS` — 2026-06-12

Working notes for the **third** threshold tune under the
replay-calibration plan (Phase 3). Sibling: threshold-solar-radiation.md
(first tune), threshold-daytime-clouds.md (second tune). Same
discipline: one threshold per commit, n= note, rescore + calibrate
+ live-era check.

## Question

The `synoptic_override` rule fires `NO_GO` (HARD severity) when
`synoptic_wind_knots >= SYNOPTIC_OVERRIDE_KNOTS`. The current
value is 15 (research-analogue guess). Per the plan: "HARD→SOFT
or a higher bar" — backed by the `n=2` in the live log suggesting
the bar is too low.

## Data

The pressure-level archive (which carries `synoptic_wind_knots`)
is **only available for the ICON-era (2022-11-24+)**. The
pre-2021 IFS-HRES archive doesn't expose 850 hPa wind. So this
tune is on **n=648** ICON-era days, not the full 3,263. That's
a real caveat — the data-fitted value is reliable for the
modern era but might miss something specific to the pre-2021
synoptic pattern.

```
synoptic_wind_knots distribution (n=648 ICON-era days)
  mean   5.0 kt
  median 4.6 kt
  min    1.2 kt
  max   29.1 kt

By actual verdict:
  go     n=313  mean 5.4 kt  median 4.9 kt
  maybe  n=256  mean 4.6 kt  median 4.3 kt
  no_go  n= 79  mean 4.9 kt  median 4.2 kt
```

The mean synoptic wind is **essentially identical** across the
three verdicts (5.4, 4.6, 4.9 kt). The standard deviation is
much larger than the difference between groups. **At this
range of values, synoptic wind is not a useful predictor of
whether the lake fires.**

## Method

For threshold X, the rule says `NO_GO` when
`synoptic_wind_knots >= X`.
- **N_C(X)** = days with synoptic >= X AND fired=0
- **N_T(X)** = days with synoptic >= X AND fired=1
- **N_C − N_T** = the rule's net contribution

Sweep X from 0 to 50 in steps of 1.

## Result

```
 threshold | N_C (rule right) | N_T (rule wrong) | N_C − N_T
 ----------+------------------+------------------+---------
    0 kt   |              335 |              313 |     +22
    5 kt   |              128 |              152 |     −24
   10 kt   |               11 |               17 |      −6
   15 kt   |                0 |                4 |      −4   ← current
   20 kt   |                0 |                2 |      −2
   25 kt   |                0 |                1 |      −1
   30 kt   |                0 |                0 |       0
```

The rule is **net-negative at every threshold from 5 to 25**.
The data-fitted peak is at X=0 (rule always fires, net +22) but
that's the OPPOSITE of the rule's design intent — it was meant
as a *safety net* for extreme synoptic days, not a regular
veto.

## Why the rule is broken in this data

The ICON-era mean synoptic wind is 5.0 kt. The 15-kt threshold
fires on 4 days. Of those 4, all fired (rule wrong 100% of the
time). The threshold is too low — it catches non-extreme
days that the model would have correctly called GO.

At a higher bar (25-30 kt), the rule would only fire on truly
extreme synoptic days — exactly what the safety net is for.
But our ICON-era sample has only 1-2 such days, so we can't
data-fit the bar. The judgment call: **25 kt** is "the
threshold below which the rule never earned its keep in 648
days". Picking 25 vs 30 vs 999 is essentially a coin flip on
this data; 25 leaves the safety net in place for any future
extreme event.

## Δ when moving from 15 → 25

```
  At 15 kt: rule fires on 4 days (0 right, 4 wrong)
  At 25 kt: rule fires on 1 day  (0 right, 1 wrong)
```

The rule fires on 3 fewer days. None of them were correctly
vetoed, so the net effect on accuracy is 0 (the previously-
wrong-vetoed days now have other rules deciding).

## Before / after at the aggregator level

`oracle calibrate --replayed --resimulated --label duration` on
the 3,263-day sample: **42%** (unchanged from after the
daytime_clouds tune). The rule was net-negative and barely
active; removing those 3 wrong vetoes doesn't move the
headline.

Live era: **57%** (unchanged). Same reason.

## Why HARD severity stays

The plan hinted at "HARD→SOFT or a higher bar". I picked the
higher bar. The HARD severity is appropriate when the rule
*does* fire: a 25+ kt synoptic day is genuinely a condition
where the thermal cell can't develop, and a HARD veto is
the right call. Lowering to SOFT would mean a 25+ kt day
gets demoted to "1 of 2 soft vetos" instead of a hard veto,
which is the wrong direction for an extreme-condition
safety net.

If future data shows the rule over-vetoing at 25+ kt, the
right follow-up is to change severity to SOFT (or to remove
the rule entirely if it's still not earning its keep).

## Open question — should we just disable this rule?

The ICON-era data shows the rule is net-negative at every
threshold from 5 to 25. The cleanest move might be to
**disable the rule** (set threshold to 999, or remove the
constant entirely and have the rule always return GO).

I went with 25 instead of 999 because:
- The rule's *physical* intent (extreme synoptic kills
  thermals) is sound; we just don't have enough such days
  in 648 to data-fit it
- A safety net is cheap to keep — it costs us nothing
  except a constant value
- Future data (e.g., a 2027 replay that includes a major
  storm event) might vindicate the rule

If 25-kt never fires in the next 12 months, that argues for
disabling. Parked as a follow-up.

## File-rotation policy

This file documents the tuning run for `SYNOPTIC_OVERRIDE_KNOTS`.
Next per the plan: `MAX_UPPER_CROSSFLOW_KNOTS` will get
`threshold-upper-level-wind.md` (or similar) in this directory.
