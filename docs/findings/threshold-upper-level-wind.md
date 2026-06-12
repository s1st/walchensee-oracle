# Threshold tuning: `MAX_UPPER_CROSSFLOW_KNOTS` — 2026-06-12

Working notes for the **fourth** threshold tune under the
replay-calibration plan (Phase 3). Sibling: threshold-solar-radiation.md
(first), threshold-daytime-clouds.md (second),
threshold-synoptic-override.md (third). Same discipline: one
threshold per commit, n= note, rescore + calibrate + live-era
check. **The tune was reverted.** The findings from the
attempt are what's worth keeping.

## Question

The `upper_level_wind` rule fires `NO_GO` (HARD severity) when
`max_wind_700_knots > MAX_UPPER_CROSSFLOW_KNOTS`. Current value
is 25 (research-analogue guess). The plan flagged it for re-fit
on the back of "crossflow veto was 0/2 in the live log" (the
n=2 was too small to be meaningful).

## Data

`max_wind_700_knots` is **only available for the ICON-era
(2022-11-24+)** — same caveat as the synoptic tune. n=648
ICON-era days.

```
max_wind_700_knots distribution
  mean   15.6 kt
  median 13.1 kt
  max    67.7 kt

Fire rate by 700 hPa wind bucket:
   0-  5 kt      43 days    44% fire
   5- 10 kt     197 days    52%
  10- 15 kt     133 days    52%
  15- 20 kt     106 days    36%   ← dips here
  20- 25 kt      53 days    55%
  25- 30 kt      54 days    48%   ← current
  30- 40 kt      45 days    42%
  40- 50 kt      11 days    55%
  50- 60 kt       5 days    80%
  60- 80 kt       1 day    100%
```

## The rule-level sweep

For threshold X, the rule says `NO_GO` when 700 hPa wind > X.

```
 threshold | N_C (rule right) | N_T (rule wrong) | N_C − N_T
 ----------+------------------+------------------+---------
    0 kt   |              335 |              313 |     +22
    5 kt   |              306 |              292 |     +14
   10 kt   |              215 |              191 |     +24
   15 kt   |              152 |              123 |     +29   ← peak
   20 kt   |               84 |               85 |      −1
   25 kt   |               59 |               56 |      +3   ← current
   30 kt   |               31 |               29 |      +2
   35 kt   |               12 |               18 |      −6
   40 kt   |                6 |               11 |      −5
   45 kt   |                1 |                7 |      −6
   50 kt   |                1 |                5 |      −4
   55 kt   |                0 |                4 |      −4
   60 kt   |                0 |                1 |      −1
```

The data-fitted peak is at X=15 with N_C − N_T = +29. The current
25 is at +3, net +26 improvement available at the rule level.

## I made the change (25 → 15) and the headline regressed

Following the rule-level N_C − N_T analysis: change 25 to 15,
expect +26 days of net improvement.

Empirical result: **headline accuracy went from 42% to 41%
(−1pp, −33 days)**. Live era unchanged at 57%. The expected
+26 day rule-level improvement did not show up at the verdict
level.

## Why the rule-level analysis was misleading

The 160 days newly covered by lowering the threshold (15 < wind
≤ 25) have a fire rate of ~45% (interpolating between 36% and
55%). So the rule should catch ~88 of them correctly and wrongly
veto ~72. Net +16 days. But the empirical result is **−33 days**.

The missing piece: the rule's HARD severity. Each fire is a HARD
veto, which means:
- A day with 0 → 1 HARD veto: flips to NO_GO (the day is now
  "definitely no thermal")
- A day with 1 → 1 HARD veto: stays NO_GO (no change, just
  confirmed)
- A day with 1 SOFT → 2 SOFT vetoes: downgrades to MAYBE
  (the rule is *adding* a 2nd soft veto via the crossflow HARD,
  but the *other* soft vetos from other rules still count;
  HARD + 1 SOFT is still HARD)

Wait, actually the rule returns HARD severity. Let me re-check.

```python
if crossflow > config.MAX_UPPER_CROSSFLOW_KNOTS:
    return Verdict(..., severity=Severity.HARD)
```

Yes, HARD. So adding the crossflow veto on a previously-1-soft-veto
day makes it 1-HARD + 1-SOFT, which the aggregator treats as HARD
verdict → NO_GO.

For the 88 days that fired (rule wrong): was correctly go/maybe,
now NO_GO → wrong.
For the 72 days that didn't fire (rule right): was correctly
maybe/no_go or wrongly go/maybe, now NO_GO. If was wrongly go:
now right. If was correctly maybe: still right (just NO_GO instead
of maybe). So +72 correct at best, +88 wrong at worst.

The empirical -33 days is the actual net. The rule's N_C - N_T
analysis of +16 was the floor; the actual is worse because the
"new" days were already somewhat right (maybe) and we're flipping
them to NO_GO (still right but only on days that didn't fire).

Actually the simplest read: the 88 fired days that the rule wrongly
vetoes would have been CORRECT without the rule (they were fired,
predicted as go/maybe = right). With the rule vetoing, they become
NO_GO = wrong. So 88 wrong adds.

Of the 72 didn't-fire days, 0 were right without the rule (they
were either go or maybe, both wrong against the actual no_go).
With the rule vetoing, they become NO_GO = right. So 72 right
adds.

Net: -88 + 72 = **-16 days**, expected.

Observed: -33 days. The extra -17 is likely from the maybes
that were already right (correctly hedging that the day might not
fire) getting demoted to NO_GO (still right, but the aggregator
treats them differently in some way I haven't fully decomposed).

## Reverted

I reverted the change — back to 25. The reason isn't "the
rule-level analysis was wrong"; it's "the rule-level analysis
was incomplete because it didn't model the aggregator's
verdict-shift mechanics". The data still says 15 is the better
rule-level threshold, but 25 is the better *aggregator-level*
threshold because the rule's contribution to the verdict is
dominated by how its vetos interact with the consensus aggregator,
not by the simple veto accuracy.

## What this teaches us

The "one threshold per commit, N_C − N_T as the metric" discipline
worked for the first three tunes (solar, daytime_clouds,
synoptic) but **breaks down for HARD-severity rules** that
share their veto with other rules' soft vetos. The aggregator's
"2-soft-veto → MAYBE" bar + the rule's HARD severity means
adding a crossflow veto is more disruptive than the simple
N_C − N_T count suggests.

The right fix is an **aggregator-aware tuner** — one that scores
candidate thresholds against the final verdict's accuracy, not
just the rule's N_C − N_T. The current discipline is "one
threshold per commit, n= note, headline accuracy check" — and
the headline accuracy check caught this. The fact that the
empirical result diverged from the predicted +26 is the value
of running the full rescore-and-recalibrate loop, not a flaw
in the discipline.

## What about the synoptic tune then?

The synoptic_override rule is also HARD severity. Did the
synoptic tune (15 → 25) have the same hidden effect?

At 15 kt: rule fires on 4 days (all wrong).
At 25 kt: rule fires on 1 day (wrong).

Lowering the threshold from 15 to 25 *removed* 3 wrong vetos.
Removing vetos from the verdict can't make accuracy worse (the
day was wrong without the veto, still wrong with it; removing
the veto doesn't change anything else). And the 3 removed
vetos had been NO_GO over fired days — those 3 days become
go/maybe, and the actual was fired → the new verdict is more
likely to match the actual.

So the synoptic tune (lowering activity) was net-positive by
construction. The upper_level_wind tune (increasing activity)
hit the aggregator's complex behaviour. Different signs of
change, different outcomes.

## File-rotation policy

This file documents the *failed* tune. The 25-kt value stays in
`config.py` — no code change is committed for this tune. The
working notes are kept because the "tune failed at the
verdict level" finding is the most important documentation
artifact of the run; future contributors should know that the
per-rule N_C − N_T metric isn't a complete picture for HARD
rules.

Next per the plan: `FOEHN_TRIGGER_DELTA_HPA`, `MIN_BOUNDARY_LAYER_HEIGHT_M`,
`WET_SOIL_MOISTURE_M3M3`, `COLD_LAKE_DELTA_C`. The HARD-severity caveat
applies to `foehn_override` (HARD veto) and possibly others. Watch
the verdict-level result, not just N_C − N_T.
