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

`storm = buoy_gust_front ∨ dwd_heavy_precip ∨ clip_visible`. **Build the label for ALL
in-season days, not just LI≤−2 days**, so we capture storms the current flag *misses*
(the LI≤−2 selection is what we're trying to beat — training only on it bakes in its
blind spots).

### Dataset scope — corrected (the "68 in 10 years?" question)

The spike's 68 was an **artifact of the replay records**, which stored `lifted_index`
for **2021–2022 only** (214 in-season days each; all other years have none). It is 68
LI≤−2 days across **2 seasons**, not 10 years. Re-fetching features fresh lifts this:

| | coverage | in-season days |
|---|---|---|
| stored replay LI | 2021–2022 | 428 |
| **Open-Meteo CAPE/LI (historical-forecast)** | **2021 → 2025** | **~1,070** |
| buoy (gust/pressure) | ≥ 2019 | label, fetch gently |
| Open-Meteo / ERA5 archive | — | **no CAPE/LI before 2021** (hard ceiling) |

So the practical dataset is **2021–2025, ~5 seasons (~1,070 in-season days)** — ~2.5×
the spike. Storm positives scale with it (~21 → ~50). Still small-n (treat as a
research advisory; grow with the 2026 live season), but no longer 2 seasons. **Pre-2021
can't be used**: Open-Meteo exposes no CAPE/lifted_index there, and ERA5 archive
doesn't either, so the deep buoy/DWD history has no features to pair with.

Operational caveat learned: the Addicted-Sports buoy endpoint **429-rate-limits** bulk
pulls — fetch sequentially with a delay (a 4-wide pull got only 40/428 days).

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

1. **Label set** — buoy gust+pressure label for **all 2021–2025 in-season days**
   (~1,070), fetched *sequentially with delay* (429 limit) + DWD precip; small
   hand-checked validation sample.
2. **Feature set** — afternoon-window convective aggregates (CAPE/LI/precip/CIN/
   precip-prob) + deep shear for the same 2021–2025 days from the historical-forecast
   host (no live API cost). Not limited to the replay records (those stop at 2022).
3. **Fit + sweep** — baseline vs CAPE×LI vs logistic; FA/hit curve; pick operating pt.
4. **Wire** — export coeffs, rewrite `is_storm_risk`, update tooltip + tests, rescore.

## First results (2026-06-24, `scripts/thunderstorm_model_spike.py`)

Built the dataset from **stored** buoy curves (`ground_truth.machine.samples` —
gust + pressure, all years, no re-fetch) + a free Open-Meteo feature fetch.
Dataset: **2021–2025 in-season, 1067 days, 89 gust-front storms (8.3% base rate)**.
Label = afternoon buoy `max_gust ≥ 22 kt AND pressure_range ≥ 2 hPa`.
Leave-one-year-out:

| Model | POD | FAR | Peirce |
|---|---|---|---|
| **LI≤−2 (current flag)** | 44% | 87% | **0.178** |
| CAPE × (−LI) | 40% | 84% | 0.213 |
| **Logistic (7 features)** | **63%** | 78% | **0.429** |
| Logistic @ matched recall (44%) | 44% | **75%** | 0.319 |

**The multi-feature logistic ≈ doubles Peirce (0.178 → 0.429)** and at the current
flag's recall cuts FAR 87% → 75%. The LI flag is genuinely poor: it misses 56% of
real gust-fronts *and* 87% of its alarms are false. `precip_prob` alone is noisy
(heavy median separation but large overlap); the *combination* (CAPE + LI + CIN +
precip + precip-prob + shear + low-cloud) is what carries it. FAR stays high (~75%)
because storms are rare and hard — but it's a warning, so it can choose its recall/
FAR operating point with the rider.

### Iteration (recall-favoring; the chosen operating posture)

Added interaction features (CAPE×−LI, CAPE×shear, CAPE×precip-prob, shear×−LI) and
compared logistic vs HGB; tested a looser label. Findings:

- **Strict gust-front label (gust≥22 ∧ dP≥2, 89 storms) is more learnable** than the
  looser one (gust≥20 ∧ dP≥1.5, 156 storms; Peirce only ~0.24–0.32) — the strong
  gust+pressure events tie tightest to the convective features. Keep the strict label.
- **Logistic + interactions, recall-favoring operating point: POD 82%, FAR 84%,
  Peirce 0.418.** vs LI≤−2's POD 44% / FAR 87%. → **~2× the recall (44→82%) at a
  *lower* false-alarm ratio** — strictly better on both axes at the chosen point.
- HGB nudges Peirce (0.451) but is a black box (can't export to pure-Python coeffs);
  the gap is small, so **logistic wins for production** (interpretable, frozen coeffs).
- FAR ~84% is inherent to an 8.3%-base-rate event at high recall — acceptable for a
  *warning* (favor recall: missing a gust-front costs more than crying wolf).

**Decision: ship the logistic at POD≈82%.**

### Productionized (2026-06-24)

Shipped on `li-decouple-experiment`. `precipitation_probability` was **dropped** —
it is all-null on the historical-forecast archive used for training, so it would
drift from the live API. Final model: **9 features** (cape, li, cin, precip, shear,
low_cloud + cape×−li, cape×shear, shear×−li), leave-one-year-out **POD 82% / FAR
84% / Peirce 0.431** vs LI≤−2's 0.178.

- `pillars/meteo.py` — afternoon (12–18) aggregates of cape/li/cin/precip/shear/
  low-cloud added to `MeteoSnapshot` (+ `convective_inhibition`, `wind_speed_10m`
  fetched). All Optional; absent on the archive host / pre-2021.
- `storm_classifier.py` — pure-Python scorer (standardize → logistic → sigmoid);
  reproduces sklearn to 2e-16. Falls back to LI≤−2 when features absent.
- `knowledge/storm_coeffs.py` — frozen coeffs, auto-generated by
  `scripts/export_storm_coeffs.py` (builds the training set through the *pillar's
  own code*, so train/serve can't drift). **No sklearn/numpy at serve time.**
- `is_storm_risk` → `storm_classifier.storm_advisory_*` everywhere
  (`atmospheric_stability`, `calibration.storm_suspected`); the verdict stays
  decoupled. Tooltip updated; `tests/test_storm_classifier.py` adds golden-vector,
  fallback, and shadow-invariant tests. Historical rescore is a no-op (legacy
  records lack afternoon features → LI fallback → unchanged).

Revisit later: a heavy-rain-inclusive label and 2026 live validation to firm up the
operating point (the bootstrap CI on recall is wide: [74%, 90%]).

## Risks

Small labelled n (≈ tens of real storms), buoy outages, label noise (lake-local vs
"storm somewhere nearby"), single LI era (2021–22), season-limited. Treat as a
research advisory until a 2026 live season adds samples — same posture as the shadow
ML classifier ([[project_ml_classifier_plan]]).
