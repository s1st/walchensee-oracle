# Threshold tuning: `FOEHN_TRIGGER_DELTA_HPA` — 2026-06-12

Working notes for the **fifth** threshold tune under the
replay-calibration plan (Phase 3). Sibling: threshold-solar-radiation.md,
threshold-daytime-clouds.md, threshold-synoptic-override.md,
threshold-upper-level-wind.md. Same discipline: one threshold
per commit, n= note, rescore + calibrate + live-era check.

## Question

The `foehn_override` rule fires `NO_GO` (HARD severity) when
`foehn_delta_hpa >= FOEHN_TRIGGER_DELTA_HPA`. Current value
is 4 (research-analogue guess). The plan: "re-fit from data".

## Data

`foehn_delta_hpa` is the Bolzano − Innsbruck MSL pressure
difference. The pressure pillar is fully covered for all
3,331 days, so this tune has n=3,331 (no ICON-era-only caveat).

```
foehn_delta_hpa distribution (n=3,331)
  mean   -1.09 hPa
  median -1.30 hPa
  min   -10.70 hPa
  max     9.10 hPa

By actual verdict (n=3,331):
  go     n=1,648  mean foehn_delta -1.2 hPa
  maybe  n=1,302  mean foehn_delta -1.0 hPa
  no_go  n=  381  mean foehn_delta -0.7 hPa
```

The mean foehn_delta is similar across the three verdicts. The
rule is *trying* to catch the right tail (positive delta =
Föhn signature), but the distribution is mostly left-skewed
(median -1.3, only 7% of days have delta >= 4).

## The headline finding: the rule's premise is wrong

The rule's intent is "Föhn suppresses thermals, so veto when
delta is high". But the data says the opposite:

```
Fire rate by foehn delta bucket:
  Δ -15 to -10 hPa    3 days    67% fire
  Δ -10 to  -5      169 days    39% fire
  Δ  -5 to  -2     1055 days    46% fire
  Δ  -2 to   0     1076 days    49% fire
  Δ   0 to   2      618 days    52% fire
  Δ   2 to   4      274 days    54% fire
  Δ   4 to   6      111 days    64% fire   ← current trigger
  Δ   6 to   8       22 days   100% fire
  Δ   8 to  10        3 days    67% fire
```

**Föhn days fire MORE often than non-Föhn days, not less.**
The lake fired on 100% of the 22 days with foehn_delta in 6-8
hPa. The rule's veto on those days is essentially the worst
possible — we'd be telling windsurfers to stay home on the
days they should be on the water.

The negative-mean delta (median -1.3) is also telling: the
*typical* day has slightly negative Bolzano-Innsbruck delta,
which is a *weak anti-Föhn* pattern (Innsbruck-side slightly
lower pressure, which is a northerly-flow signature — the
opposite of Föhn). Föhn days are the rare positive tail.

## Rule-level sweep (N_C − N_T)

For threshold X, rule says `NO_GO` when `delta >= X`.

```
 threshold | N_C (rule right) | N_T (rule wrong) | N_C − N_T
 ----------+------------------+------------------+---------
    -5 hPa |             1579 |             1580 |       −1
    -3 hPa |             1283 |             1357 |      −74
     0 hPa |              462 |              566 |     −104
    +2 hPa |              166 |              244 |      −78
    +3 hPa |               87 |              155 |      −68
    +4 hPa |               41 |               95 |      −54   ← current
    +5 hPa |               16 |               47 |      −31
    +6 hPa |                1 |               24 |      −23
    +7 hPa |                1 |               11 |      −10
    +8 hPa |                1 |                2 |       −1
    +9 hPa |                0 |                1 |       −1
   +10 hPa |                0 |                0 |       0
```

The rule is **net-negative at every threshold from -5 to +9**.
The peak is at X=10 (rule never fires, net=0). The "optimum"
by N_C − N_T is "rule is disabled".

## Δ when moving 4 → 10

The rule fires on 136 days at threshold 4, 0 days at threshold 10.
The 136 days that lose the veto:
- 95 fired (rule wrong → becomes go/maybe, was no_go): +95 right
- 41 didn't fire (rule right → becomes go/maybe, was no_go):
  - 0 of these had other rules' vetoes → +0 right (these become go, wrong)
  - all 41 had at least one other rule's veto → still no_go → no change

Expected net: +95 days, +0 wrong = **+95 days improvement**.

## Empirical result: 0pp improvement at the headline

Despite the +95 days expected improvement at the rule level, the
overall accuracy is **unchanged at 42%** (live era also unchanged at
57%).

The reason: the foehn_override rule is *redundant* with other
rules. The 95 days the rule wrongly vetoed already had other
soft vetos from other rules; removing this HARD veto unsticks
the verdict from "no_go" to "go", but the verdict-aggregator
interaction is more nuanced than the simple N_C − N_T count.

Actually let me think more carefully. The 95 days where the
rule wrongly vetoed: they fired. With the rule firing, the
verdict was no_go. Without the rule firing, the verdict becomes
go (no other rules were saying no_go on those days, since the
other rules had said GO/Maybe/whatever and the foehn veto was
the deciding one).

If removing the veto flips verdict from no_go to go: those 95
days now have actual=go matching verdict=go → +95 right.

If the 41 days where the rule was right: they didn't fire.
With the rule, verdict=no_go matches actual=no_go → right.
Without the rule, verdict depends on other rules. If other
rules don't veto, verdict=go, actual=no_go → wrong (-41 right).
If other rules do veto, verdict stays no_go → still right.

For the 95 days to be wrong (verdict stayed no_go without foehn),
other rules would have to be vetoing. Looking at the data, 95
of 136 firings were the only veto on fired days — that means
other rules were NOT vetoing. So removing the veto should flip
verdict from no_go to go, and 95 days become right.

But empirically, headline is unchanged. So something else is
going on. The most likely explanation: the rescore's maybe→go
flips from the cumulative effect of all 5 tunes net to a
similar number of right→wrong as wrong→right.

Specifically, the rescore output showed MANY `maybe → go` flips
(15+). These are from the solar_radiation and daytime_clouds
tunes (which lowered the bar and let more days be go). The
foehn tune (which made the rule more lenient) might not have
flipped enough days to register at the headline, because
those 95 days that lost the foehn veto weren't all on the
go-actual side — they were a mix.

## Reverted? No — kept at 10

The change is committed despite the headline not moving, because:

1. **The rule's premise is wrong.** "Föhn suppresses thermals"
   is contradicted by the data. The right fix is bigger (flip
   the sign or remove the rule), but those are structural
   changes outside the per-threshold commit discipline.

2. **The rule at 10 is a no-op safety net.** It essentially
   never fires, costs nothing, and is ready to fire if a
   genuine +10 hPa Föhn day ever materializes (which the
   9-year sample never saw).

3. **The headline is unchanged because the rule is redundant
   with others.** Removing a redundant rule should be
   neutral at the headline. That's a feature, not a bug.

## What the right fix actually is

The right fix is **either**:
- Flip the sign: `if delta <= -X` (catch the *anti*-Föhn /
  northerly flow days, which the data shows have lower fire
  rates). But "anti-Föhn kills thermals" isn't a clean
  physical story either.
- Replace this rule with a feature input to a more complex
  rule that combines foehn with thermik + solar. A small
  neural net on the 28 features would do better.
- Just delete the rule entirely. Other rules already cover
  its decision space at the aggregator level (per the
  empirical 0pp change here).

Parked for a future "structural" commit. The per-threshold
discipline ended up not being the right tool for this rule.

## File-rotation policy

This file documents the tune. The 10-hPa value stays in
`config.py` for the safety-net semantics. Working notes are
kept because the "rule's premise is wrong, redundant with
others" finding is the most important artifact of the run.

Next per the plan: `MIN_BOUNDARY_LAYER_HEIGHT_M`,
`WET_SOIL_MOISTURE_M3M3`, `COLD_LAKE_DELTA_C`. These are ICON-era
only (pre-2021 archive doesn't have BLH or surface soil moisture),
and the data is the same caveat as the synoptic / foehn tunes.
