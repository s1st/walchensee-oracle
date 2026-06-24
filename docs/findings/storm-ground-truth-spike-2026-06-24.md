# Storm ground-truth spike — can we score the LI storm flag? (2026-06-24)

**Branch:** `li-decouple-experiment`
**Question (Simon):** how good are we at predicting *actual* thunderstorms, and
where does the dashed "storm" box on the dashboard come from?

## Where the dashed box comes from

It is **forecast-driven, not ground truth.** Every strip row (forecast / ML / HGB
/ **actual**) gets the dashed outline from one flag:

```
d.storm = _storm_suspected(record) = inputs.meteo.min_lifted_index ≤ MIN_LIFTED_INDEX (−2)
```

`min_lifted_index` is the **predicted** lifted index from Open-Meteo, logged at
forecast time. The box is painted on the *actual-wind* row too, but its source is
the forecast — so a dashed box around the actual cell means "a storm was
**forecast**," never "a storm was **observed**." We log no storm observation
anywhere: `ground_truth.machine` holds only Urfeld **wind** (peak avg/gust,
ignition, duration). Storm-prediction skill was therefore never measured.

## What we tried

Bright Sky (the DWD wrapper we already use) exposes a `condition` enum that
*includes* `thunderstorm`, and historical DWD station observations. Spike:
`scripts/storm_ground_truth_spike.py` pulls afternoon (11–21 local) observations
for every in-season replay day carrying a forecast LI (n=428, 2021–2022 — IFS
exposes LI only for those years), nearest station **Jachenau-Obernach, 3.3 km**,
and crosses the LI≤−2 flag with observed `condition` and afternoon precipitation.

## Result

**`condition` is not a usable storm label via Bright Sky.** It never reports
`thunderstorm`:

| Station | window | thunderstorm hours |
|---|---|---|
| Jachenau-Obernach (3.3 km, automatic) | Apr–Oct 2021–22, 428 days | **0** |
| Hohenpeißenberg (25 km, **manned** observatory) | Jun–Aug 2021 | **0** (only dry/rain) |

Even DWD's historic manned mountain observatory shows no `thunderstorm` over a
full convective summer — so this is a Bright Sky derivation / hourly-product gap,
not just a small-station limitation. The 2×2 against `condition` is degenerate
(observed base rate 0/428).

**Precipitation proxy (cheap, imperfect — conflates stratiform rain with storms)**
on the 68 predicted-storm days:

| afternoon max hourly precip | days (of 68) |
|---|---|
| ≥ 0.1 mm | 39 |
| ≥ 1.0 mm | 29 |
| ≥ 5.0 mm | 17 |
| **bone-dry (0 mm)** | **29 (43%)** |

So the LI≤−2 flag **over-warns heavily**: ~43% of predicted-storm days had no
afternoon rain at all (e.g. 2021-06-19, forecast LI −6.0, observed dry all 24 h),
and only ~25% saw ≥5 mm (plausible real convection). We can't pin an exact
thunderstorm hit/false-alarm rate without a real storm label.

## Bearing on the LI-decouple decision

This *reinforces* decoupling: the storm flag fires on predicted instability with a
high false-alarm rate, so using it as a HARD verdict veto threw away ~66 rideable
sessions on a signal that's wrong (no storm) ~40%+ of the time. As a non-vetoing
Caution advisory the false alarms are far cheaper — but the advisory itself still
over-warns, which is worth telling users (and a reason to tighten the trigger,
e.g. fold in CAPE / precip-probability, once we have a real label).

## To actually measure storm prediction (proper follow-ups)

1. **DWD present-weather (ww) codes from CDC directly** (not via Bright Sky) —
   `opendata.dwd.de` publishes hourly/sub-daily present-weather and thunderstorm
   observations at manned stations; Hohenpeißenberg has the ww thunder codes.
2. **Lightning data** — Blitzortung.org archive or DWD's lightning product:
   spatially complete, the gold-standard label, heavier integration.
3. **Webcam** (Simon's idea, partnership asset) — classify the Urfeld (and other)
   webcam frames for cumulonimbus / rain shaft / darkening. **An archive exists**
   (Andy is reworking it to be more prominent, 2026-06), so this is *not* forward-
   only: if the archive reaches back to the 2021–22 replay window we can backfill
   a lake-local observed-storm label for the exact 68 predicted-storm days the
   DWD `condition` field couldn't score — finally closing the loop. Pipeline:
   pull afternoon frames (11–21 local, ~hourly) per day → vision-classify
   (storm / convective cloud / rain shaft / clear) → day label → score the LI≤−2
   flag for a real hit / false-alarm rate. Lightning from stills is unreliable
   (ms flashes vs per-minute snapshots); pair with Blitzortung for the electrical
   label. Needs from Andy: archive depth, access pattern (URL/API vs UI), frame
   cadence. Within the partnership, and a candidate "storm cam" feature for his
   site in return.
4. **Precip + CAPE composite** — cheapest interim proxy; tighten the trigger and
   re-score, accepting it's "convective wet," not "thunderstorm."
