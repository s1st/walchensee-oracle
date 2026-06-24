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

## Webcam archive — access confirmed + visually validated (2026-06-24)

The Addicted-Sports webcam archive is **directly fetchable, programmatically, and
reaches the replay window** — so it *can* backfill an observed-storm label for the
2021–22 days. Confirmed from the archive's network calls:

- **Frame URL:** `https://www.addicted-sports.com/fileadmin/webcam/walchensee/{YYYY}/{MM}/{DD}/{HHMM}_hd.jpg`
  (sizes `_sm` ~15 KB / `_lm` ~40 KB / `_hd` ~185 KB; plain GET, no auth).
- **Cadence:** every 10 min (`HHMM`, MM ∈ {00,10,20,30,40,50}).
- **Listing/metadata (JSONP):** `…/fileadmin/webcam/v14include/list.php?img={YYYY}/{MM}/{DD}/{HHMM}&wc=walchensee&exif=1&samehour=1`
  (also 10-min `.mp4` timelapse clips per slot).
- **Coverage:** 2021 and 2022 both present; per-day gaps exist (webcam shares the
  buoy's outage mode — e.g. 2021-06-19 is missing all afternoon).

**Visual validation** (predicted-storm days, `_hd`): 2022-06-24 16:00 (DWD 26.6 mm)
shows raindrops on the lens + dark low cloud + a rain shaft — unmistakable storm.
2021-06-20 14:00 (the 0 mm false alarm) shows high flat overcast but dry, with the
Karwendel visible to the horizon. The storm/no-storm contrast is obvious; a vision
classifier should separate them easily. This closes the gap the DWD `condition`
field left.

Proposed backfill: afternoon frames (≈11–21 local, hourly is enough — every 6th
10-min slot) for the 68 predicted-storm days → vision-classify
(storm / convective / rain / clear) → day label → real hit/false-alarm for LI≤−2.
Hand-label ~20 days first to measure the classifier. Bulk archive access is a
partnership courtesy — give Andy a heads-up before scaling, and offer the
classifier back as a "storm cam" feature.

## Multi-signal triangulation — the LI flag's real false-alarm rate

No single source is a clean thunderstorm label at lake resolution, so we
triangulated four independent signals on the 68 LI-predicted-storm days
(`scripts/storm_label_multisignal.py`). They converge:

| Signal | What it measures | Verdict on the 68 |
|---|---|---|
| **Buoy gust + pressure jump** (lake-local, authoritative) | gust spike + sharp MSL dP = gust front | 8 storms / 37 covered → **78% FA** |
| DWD precip (3.3 km) | convective wet proxy | 29/68 bone-dry; 17 ≥5 mm |
| CLIP zero-shot (webcam `_hd`) | visible rain (high precision, low recall) | fires on 1 blatant day |
| Frame density (webcam) | capture ramps on big-wind days (noisy) | 6→12/hr on one gust-front, not the other |
| **Combined (any signal)** | — | **21 storms / 68 → 69% false alarm** |

**The LI≤−2 storm flag is wrong ~70% of the time** (78% lake-local). Of the 68,
~18–28 were *rideable, dry thermals* — the exact "storm day that's still a good
thermal day" the decouple recovers.

Key cross-source findings:
- **The buoy is the right ground truth, not DWD.** Storms DWD logs 3.3 km away
  often miss the lake: 2022-06-05 was DWD 36 kt / 14 mm but buoy **24 kt / 0 mm**;
  2021-07-30 DWD 36 kt / 8 mm but buoy **13 kt / 0 mm**. DWD over-reads lake storms.
- **Pressure jump catches what rain/vision miss.** 2021-06-29: buoy gust 43 kt,
  **+7.2 hPa**, gustiness 1.79 — a textbook gust front — with DWD rain only 2 mm and
  CLIP 0.00. Gust+pressure is the strongest lake-local storm detector.
- **Pretrained weather CNNs fail** (`prithivMLmods/Weather-Image-Classification`,
  `dima806/weather_types_image_detection`): rain prob ~0.3 vs ~0.3 storm-vs-dry —
  domain shift from automotive/close-up training data; would need fine-tuning on
  lake-cam frames (the 2017-thesis workflow). **CLIP zero-shot** separates the
  hand-validated pair cleanly (0.95 vs 0.01) but has low recall without on-lens rain.
- Buoy + webcam archives both reach the 2021–22 replay window (with shared
  outage-mode gaps — 37/68 buoy, near-full webcam).

### Bearing on the decouple + the production storm trigger
This strongly reinforces the LI decouple (a ~70%-false-alarm signal must not HARD-
veto the verdict). For the *advisory* itself, the **buoy gust + pressure-jump**
signature is the cheapest reliable lake-local storm label and reuses the existing
`fetch_urfeld_day_curve` scrape — the path to a real hit/false-alarm metric and a
tightened Caution trigger (fold CAPE / precip-prob in, score against buoy fronts).

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
