# Data-integrity precheck: soil-moisture nulls in the replay corpus — 2026-06-13

Phase 4 step 1 of the methodology rework (`docs/fable_findings.md` §2/§6).
Before re-sweeping the `post_rain_moisture` / soil rule, confirm whether the
"only 48 non-null soil-moisture days, all late 2022" pattern in
`threshold-wet-soil-moisture.md` is a real archive gap or an artifact.

## Probe

Queried `soil_moisture_0_to_1cm` at the Urfeld coords directly against each
Open-Meteo endpoint (read-only, no corpus needed):

| Endpoint / model | soil_moisture_0_to_1cm |
|---|---|
| Live forecast, 2026-06-11 | **24/24 non-null** |
| Historical-forecast, 2024-06-15, Best Match (→ ICON) | **24/24 non-null** |
| Historical-forecast, 2024-06-15, `models=icon_seamless` | **24/24 non-null** |
| Historical-forecast, 2024-06-15, `models=ecmwf_ifs` | **0/24 — null** |
| Historical-forecast, 2020-06-15, Best Match (→ IFS) | **0/24 — null** |

## Conclusion — not a code bug; an IFS-pin artifact

IFS HRES does not model surface soil moisture; DWD ICON does. The field is
therefore:

- **null** for the IFS era (pre-2022-11-24) under Best Match, and
- **null whenever `ecmwf_ifs` is pinned**, *even for ICON-era dates that would
  otherwise carry it*.

The replay corpus was built pinned to `ecmwf_ifs` (CLAUDE.md; review §6) to keep
one model across eras. That pin forces `soil_moisture` to null across the whole
corpus — which is exactly why `threshold-wet-soil-moisture.md` shows "2023–2026:
0 non-null days" while the live API serves the field daily. The "n=48, late
2022" sample is a fragment, not a representative season of soil data.

## Implications for the re-sweep

1. **The `WET_SOIL_MOISTURE_M3M3 = 0.30` tune is invalid** — fit on 48 artifact
   days. It should be reverted to its pre-tune value and only re-derived from a
   corpus that actually carries soil moisture. (Action deferred into the
   data-dependent re-sweep, not done unilaterally here.)
2. **Model-pin tension (review §6 vs soil coverage):** pinning `ecmwf_ifs` for
   the whole sweep is clean for solar/cloud/BLH comparability but *destroys* the
   soil field. To re-tune the soil rule we must replay the ICON era
   (≥ 2022-11-24) under **Best Match or ICON**, not IFS. So the soil rule's
   re-sweep runs on a different (ICON-only) replay slice than the IFS-era
   thresholds. Document the slice with each tune.
3. **BLH has the same shape** — `boundary_layer_height` is also IFS-null
   (meteo.py notes this), so `MIN_BOUNDARY_LAYER_HEIGHT_M` (n=629 "ICON") is
   subject to the same per-model availability and must be re-checked on an
   ICON-only slice, not the IFS-pinned corpus.

## Repro

The probe is throwaway (not committed); re-run by querying the three config
URLs (`OPEN_METEO_URL`, `OPEN_METEO_HISTORICAL_FORECAST_URL`,
`OPEN_METEO_ARCHIVE_URL`) with `hourly=soil_moisture_0_to_1cm` and
`models=ecmwf_ifs` vs Best Match for an ICON-era date.
