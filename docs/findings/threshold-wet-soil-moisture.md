# Threshold tuning: `WET_SOIL_MOISTURE_M3M3` — 2026-06-12

Working notes for the **seventh** threshold tune under the
replay-calibration plan (Phase 3). Sibling: threshold-solar-radiation.md,
threshold-daytime-clouds.md, threshold-synoptic-override.md,
threshold-upper-level-wind.md, threshold-foehn-trigger.md,
threshold-boundary-layer-height.md. Same discipline: one
threshold per commit, n= note, rescore + calibrate + live-era
check.

## ⚠ Small sample caveat

This tune has the smallest sample of the queue. The
Open-Meteo Historical Forecast API's `soil_moisture_0_to_1cm`
field is **only populated for late 2022** (the DWD ICON
launch window of 2022-11-24 onward). Across the 3,331-day
sample:

- 2017-2021: 0 days with non-null soil_moisture_m3m3
- **2022: 48 days** (mean 0.311, range 0.289-0.339)
- 2023-2026: 0 days

So this tune is on n=48 days, all from late 2022. The
Open-Meteo archive's behavior for this variable is
inconsistent across years. Use this tune with caution —
the data is real but the sample is too small to be
high-confidence.

## Question

The `post_rain_moisture` rule fires `NO_GO` (SOFT severity) when
`soil_moisture_m3m3 > WET_SOIL_MOISTURE_M3M3`. Current value
is 0.35 (research-analogue guess).

## Data

```
soil_moisture_m3m3 (n=48, late 2022 only)
  mean   0.311
  median 0.310
  min    0.289
  max    0.339

Fire rate by soil moisture bucket:
  0.20-0.30 m³/m³    4 days    50% fire
  0.30-0.40 m³/m³   44 days    18% fire
```

The 0.30-0.40 m³/m³ band has 18% fire rate (well below the 50%
baseline). The 0.20-0.30 band has 50% (no signal). The "wet"
signal is real but the data is bimodal: most days are in
the 0.30-0.40 band, almost no days in 0.20-0.30 or below.

## The rule-level sweep

For threshold X, the rule says `NO_GO` when `sm > X`.

```
 threshold | N_C (rule right) | N_T (rule wrong) | N_C − N_T
 ----------+------------------+------------------+---------
  0.10 m³/m³ |              38 |               10 |     +28
  0.15 m³/m³ |              38 |               10 |     +28
  0.20 m³/m³ |              38 |               10 |     +28
  0.25 m³/m³ |              38 |               10 |     +28
  0.30 m³/m³ |              35 |                8 |     +27
  0.35 m³/m³ |               0 |                0 |      0    ← current
  0.40 m³/m³ |               0 |                0 |      0
  0.50 m³/m³ |               0 |                0 |      0
```

Peak at X=0.10-0.25 (+28). At the current 0.35 the rule never
fires (the maximum observed soil moisture is 0.339).

## Why 0.30 and not 0.10-0.25

The 0.10-0.25 range all give +28 (the rule fires on every
non-null day in the sample — the 4 "borderline" days in
0.20-0.30 happen to be 50/50 fired/didn't-fire so they don't
move the net). The 0.30 threshold fires on the 0.30-0.40 band
only — the 44 days where the "wet" signal is real (18% fire
rate).

**0.30 is the cleaner read**: it catches the "wet" days where
the signal is real and avoids the borderline 0.20-0.30 days
where there's no signal. The data-fitted peak plateau is at
+27-28 across 0.10-0.30; the choice between them is "rule
catches everything" vs "rule catches the wet band only".

## Δ when moving 0.35 → 0.30

The current 0.35 threshold never fires (max observed is 0.339).
The new 0.30 threshold fires on 43 of 48 days (the 0.30-0.40
band). Of those 43:
- 35 didn't fire (rule right, +35 right)
- 8 fired (rule wrong, -8 right)
- Net: +27 days at the rule level

## Empirical result: 0pp change at the headline

The post-tune rescore + recalibrate run shows:
- Full pass: 42% (unchanged)
- Live era: 57% (unchanged)

The 48-day sample is too small to move the 3,263-day headline.
The +27 days at the rule level are spread over 43 days
out of 3,263 (1.3% of the corpus), and the verdict-level
effect is washed out by the noise.

## Why I committed this anyway

The current 0.35 is a no-op (no day in the sample has soil
moisture > 0.34). The data says the wet signal is real
(0.30-0.40 band has 18% fire rate). Even with only 48 days
of data, the signal is clear and the change is a real
improvement over "rule that never fires". The 0.30 value
captures the wet band; future data (if the Open-Meteo archive
expands soil_moisture coverage to more years) will refine
this.

## Sample-size caveat in the constant's docstring

The `n=48` note in `config.py` calls out the small sample
explicitly. Future contributors should know that this tune
is on a tiny data slice; a re-fit on a larger sample is
the right move when one becomes available.

## File-rotation policy

This file documents the tune. The 0.30 value stays in
`config.py`. Working notes are kept because the
"Open-Meteo archive inconsistently exposes soil_moisture"
finding is the most important artifact of the run — it
tells future contributors that this variable's coverage is
spotty and the n=48 is what it is.

Next per the plan: `COLD_LAKE_DELTA_C` (10) — the
`air_lake_delta` rule's threshold. Then the aggregator
re-check.
