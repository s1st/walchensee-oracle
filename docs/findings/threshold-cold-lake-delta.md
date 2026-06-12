# Threshold tuning: `COLD_LAKE_DELTA_C` — 2026-06-12

Working notes for the **eighth** threshold tune under the
replay-calibration plan (Phase 3). Sibling: threshold-solar-radiation.md,
threshold-daytime-clouds.md, threshold-synoptic-override.md,
threshold-upper-level-wind.md, threshold-foehn-trigger.md,
threshold-boundary-layer-height.md, threshold-wet-soil-moisture.md.
Same discipline: one threshold per commit, n= note, rescore +
calibrate + live-era check.

## The headline finding: the rule's premise is **inverted**

The `air_lake_delta` rule has a 3-band structure:
- `air − water > COLD_LAKE_DELTA_C`: SOFT NO_GO (cold lake
  opposes the thermal)
- `air − water < -COLD_LAKE_DELTA_C`: plain GO (warm lake
  helps the thermal)
- Otherwise: plain GO (neutral band)

The physical premise is "cold lake opposes thermals, warm lake
helps". The data says the opposite:

```
air − water delta | days | fire rate
  -15 to -10 C    |   86 |   38%   ← cold lake (rule says GO)
  -10 to  -5      |  487 |   39%   ← cold lake (rule says GO)
   -5 to  -2      |  712 |   43%   ← cold lake
   -2 to  +0      |  543 |   49%
   +0 to  +2      |  486 |   54%
   +2 to  +5      |  618 |   57%
   +5 to  +8      |  265 |   63%   ← peak
   +8 to +10      |   73 |   52%
  +10 to +12      |   25 |   56%   ← warm side, current NO_GO trigger
  +12 to +15      |    7 |   43%
```

**Fire rate INCREASES with delta** — warm-lake days fire
*more*, not less. The rule's both directions are wrong:
- Rule says GO on `delta < -10` (where fire rate is 38%)
- Rule says NO_GO on `delta > 10` (where fire rate is 56%)

The peak fire rate is at delta +5 to +8 C, the SPRING cold-lake
regime the rule was supposed to suppress.

## The rule-level sweep (n=3,314)

NO_GO side: rule fires when delta > X.

```
 threshold | N_C (rule right) | N_T (rule wrong) | N_C − N_T
 ----------+------------------+------------------+---------
    0 C    |              633 |              838 |     −205
    2 C    |              409 |              576 |     −167
    4 C    |              204 |              317 |     −113
    6 C    |              105 |              145 |      −40
    8 C    |               49 |               55 |       −6
   10 C    |               15 |               17 |       −2   ← current
   12 C    |                4 |                3 |       +1
   14 C    |                0 |                0 |        0
   16 C    |                0 |                0 |        0
```

GO side: rule fires GO when delta < -X.

```
 threshold | nt (warm+ fired) | nc (warm+ didn't fire) | nt − nc
 ----------+------------------+-------------------------+---------
   -0 C    |              804 |                    1036 |     −232
   -2 C    |              539 |                     758 |     −219
   -4 C    |              327 |                     479 |     −152
   -6 C    |              156 |                     258 |     −102
   -8 C    |               75 |                     112 |      −37
  -10 C    |               44 |                      54 |      −10   ← current
```

Both directions are net-negative at every threshold. The "best"
NO_GO threshold is at 12°C (net +1) — at 14°C no day fires.

## Δ when moving 10 → 999

At threshold 10, the rule fires 32 times on the NO_GO side
(delta > 10) and 98 times on the GO side (delta < -10). At
threshold 999, both triggers never fire — the rule is always
in the "neutral band" which returns plain GO.

Wait, this is a subtle point. The rule's neutral band returns
**GO** (not MAYBE). So setting threshold to 999 doesn't disable
the rule — it makes the rule always say GO. That's a different
semantics than the foehn or solar tunes where the rule was
NO_GO veto-driven.

Effectively the change is:
- Before: rule says GO on delta<-10 (38% fire rate — wrong)
       + NO_GO on delta>+10 (56% fire rate — wrong)
       + GO on neutral band
- After:  rule always says GO

Net effect: removes the NO_GO veto on 32 days (where 15 were
right, 17 wrong → +15-17 = -2 net) and removes the wrong-GO
"boost" on 98 days (where 44 fired, 54 didn't — the boost was
just GO which is also what other rules say → no real change).

Expected: 0pp change at the headline (the GO→GO flip from
removing the cold-lake boost is invisible; the NO_GO→? flip
might add a couple of correctly-fired go days, but most of
those 32 days were already maybe from other rules).

## Empirical result: 0pp change at the headline

`oracle calibrate --replayed --resimulated --label duration`:
42% (unchanged). Live era: 57% (unchanged). The rule is
"redundant" with other rules at the aggregator level — its
GO on the neutral band is just "I agree" with whatever the
other rules said, and its vetoes were both wrong on average.

## Why the rule's premise is wrong (the most important finding)

The current physical model says:
- "Cold lake creates a stable boundary layer → suppresses
  thermals"
- "Warm lake creates an unstable boundary layer → helps thermals"

The data is the opposite. The most likely explanation: the
slope-heating signal (which the air temp captures) is the
primary driver of thermals. When the air is warm relative to
the lake, the slopes are warm and thermals work. The lake
lagging behind (cold) doesn't suppress the thermals because
the boundary layer is over land (where the air temp is
measured), not over the lake.

This is a real physical finding — the model was wrong about
the lake's role in thermal dynamics. A future model would
use the air temp alone (which is already in the feature set)
and not penalize the cold-lake regime.

## Why 999 and not 0 or 12

- 12°C was the data-fitted "best" (net +1, but only 7 days fire
  the rule, mostly noise)
- 0°C would be "delta > 0 always NO_GO" (1471 days, mostly
  wrong) — too aggressive
- 999 is "rule never fires" — the cleanest no-op, matches
  the foehn / synoptic / boundary-layer-height pattern of
  setting the threshold to a value where the safety net
  never activates in the available data

999 is a clear "this rule is currently inactive" signal.
Future maintainers will see the data-fitted note in
config.py and the working notes, and the threshold can be
moved if a future tune finds a better value.

## Δ when moving 10 → 999

```
At threshold 10: rule fires 130 times across both sides
  NO_GO side: 32 firings (15 right, 17 wrong)
  GO side:    98 firings (rule says GO instead of maybe/go,
             but other rules already do that, so this is
             effectively a no-op on the verdict)

At threshold 999: rule never fires. The 32 NO_GO firings
disappear. Of those, 15 were right (didn't fire) and 17 were
wrong (fired). The wrong ones become go/maybe, the right ones
might become maybe/go depending on other rules.

Empirical: 0pp change at the headline. The rule is fully
redundant with other rules.
```

## File-rotation policy

This file documents the tune. The 999 value stays in
`config.py` (the rule is effectively disabled but still in
the code, with a docstring note pointing at the data-fitted
finding). Working notes are kept because "the rule's
premise is inverted in the data" is the most important
artifact of the run — it's a real physical finding that
should inform future model work.

## What's next: the aggregator check

This was the last threshold tune in the plan's queue. Per
the plan, after the threshold pass settles the base rate,
the next move is the **aggregator check** — does the
2-soft-veto downgrade bar need to move?
