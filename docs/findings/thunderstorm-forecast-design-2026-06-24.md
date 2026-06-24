# A better thunderstorm forecast — design (2026-06-24)

**Branch:** `li-decouple-experiment`
**Motivation:** the production storm signal is a single threshold, `min_lifted_index
≤ −2` (`is_storm_risk`). Triangulated against real ground truth it is **~70% false
alarm** (78% lake-local) — see `storm-ground-truth-spike-2026-06-24.md`. Now that
the LI veto is decoupled into an advisory, we want that advisory to be a *calibrated
convective forecast* built from the features we already fetch (CAPE, LI, precip,
shear, BLH), scored against a real observed-storm label.

Goal: **maximise hit rate at an acceptable false-alarm ratio** for "a thunderstorm /
gust front actually reached Urfeld," and replace the internals of `is_storm_risk`
(its three consumers — Caution box, storm border, calibration tally — move together).
The verdict stays decoupled; this only sharpens the advisory.

## Ground-truth label (the dependent variable)

No clean thunderstorm observation exists at lake resolution, so we use a composite,
**buoy-primary** label (lake-local = what reaches the riders):

1. **Buoy gust-front** (authoritative): afternoon `max_gust ≥ ~22 kt` AND a sharp MSL
   `pressure_range ≥ ~2 hPa` in the afternoon window. Catches fronts that rain and
   the webcam miss (2021-06-29: +7.2 hPa, 43 kt gust, only 2 mm DWD rain, CLIP 0.00).
   From `fetch_urfeld_day_curve` (reaches 2021–22). Buoy rain gauge unusable (reads 0).
2. **DWD heavy precip** (Bright Sky, 3.3 km): afternoon `max hourly precip ≥ 5 mm` —
   catches rain-storms with weak surface wind. Caveat: DWD over-reads lake storms
   (storms 3 km away often miss Urfeld), so it's a looser secondary.
3. **CLIP webcam** (optional confirm): high precision, low recall — tie-breaker only.

`storm = buoy_gust_front ∨ dwd_heavy_precip ∨ clip_visible`. Label caveats: buoy
outages (37/68 storm days covered; shared webcam/buoy outage mode), small n, 2021–22
only (IFS-era LI), seasonal (Apr–Oct). **Build the label for ALL in-season days, not
just LI≤−2 days**, so we capture storms the current flag *misses* (the LI≤−2 selection
is what we're trying to beat — training only on it bakes in its blind spots).

## Features (independent variables)

Already fetched by `pillars/meteo.py` (`_HOURLY_VARS`) and in the records:

| Feature | Status | Notes |
|---|---|---|
| `min_lifted_index` | logged | current flag; instability |
| `max_lifted_index` | logged | stability cap |
| `max_cape_j_kg` | logged | **but morning 09–13 window** — convection peaks 14–17h; need afternoon CAPE |
| `precipitation` (forecast) | fetched, not aggregated for target-day afternoon | add afternoon convective-precip aggregate + `precipitation_probability` |
| `boundary_layer_height`, `cloud_cover_low`, `soil_moisture` | logged | deep BLH + moisture feed convection |
| `wind_speed_850hPa`, `wind_speed_700hPa` | logged | **deep-layer shear** = (700 − surface) organises gust fronts |

To add to the fetch (Open-Meteo exposes them): `convective_inhibition` (CIN — a cap
that suppresses CAPE), `precipitation_probability`, and **afternoon-window** (12–18
local) aggregates of `cape` / `lifted_index` / `precipitation` rather than the morning
window. The classic skill comes from the **CAPE × (−LI) "loaded gun" interaction**
gated by low CIN — not any single variable.

> Data action: the replay records hold morning-window CAPE/LI. We need afternoon
> convective aggregates. Re-fetch via the archive host (`OPEN_METEO_ARCHIVE_URL` /
> historical-forecast) for the in-season replay days — same machinery as `oracle
> replay`, just a different aggregation window. ~430 day-fetches, no live API cost.

## Model

Keep it **interpretable and pure-data**, matching the shadow-ML pattern (no
sklearn/numpy in the prod image; coefficients frozen in a constant — cf.
`knowledge/ml_coeffs.py`):

1. **Baseline:** current `LI ≤ −2`. Report its hit/FA on the new label.
2. **Two-feature threshold:** CAPE × (−LI) "loaded-gun" product, single cut. Cheap,
   inspectable, likely most of the gain.
3. **Small logistic** over {CAPE_aft, LI_aft, CIN, precip_prob, deep_shear, BLH}, L2,
   class-weighted; export ~6 floats to a constant, scored in pure Python. Calibrate
   the decision threshold to a target operating point on the FA/hit curve.

Selection rule: simplest model within ~1 FA-point of the best. The advisory is a
*warning*, so we'll bias toward recall (don't miss real gust-fronts) but cap FA well
below today's ~70%.

## Evaluation

- **Hit rate (POD)** and **false-alarm ratio (FAR)** vs the buoy label; full 2×2
  incl. misses (storms on non-LI≤−2 days). **Peirce/Heidke** (base-rate-robust) and a
  **per-rider cost** framing (a missed gust-front warning ≠ a wasted caution) — reuse
  `calibration._COST` philosophy.
- **ROC/threshold sweep** over CAPE/LI cuts; pick the operating point with the user.
- Honest baseline: a calibrated single-CAPE threshold often rivals multi-feature here.

## Integration (when validated)

- Swap the body of `is_storm_risk` (single source of truth) from `li ≤ −2` to the new
  scorer; its three consumers (Caution box, storm border, calibration `storm_days`)
  move together by construction.
- The verdict stays decoupled — this never feeds the aggregator.
- Update the LI tooltip / Caution copy to reflect a multi-factor convective risk.
- Keep the scorer in pure Python (frozen coeffs) so neither Dockerfile gains ML deps.

## Work breakdown

1. **Label set** — extend the buoy fetch to *all* in-season replay days (not just
   LI≤−2); build `storm` label JSON + a small hand-checked validation sample.
2. **Feature set** — re-fetch afternoon-window convective aggregates (CAPE/LI/precip/
   CIN/precip-prob) + deep shear for those days from the archive host.
3. **Fit + sweep** — baseline vs CAPE×LI vs logistic; FA/hit curve; pick operating pt.
4. **Wire** — export coeffs, rewrite `is_storm_risk`, update tooltip + tests, rescore.

## Risks

Small labelled n (≈ tens of real storms), buoy outages, label noise (lake-local vs
"storm somewhere nearby"), single LI era (2021–22), season-limited. Treat as a
research advisory until a 2026 live season adds samples — same posture as the shadow
ML classifier ([[project_ml_classifier_plan]]).
