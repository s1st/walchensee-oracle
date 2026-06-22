# Historical forecast data — what the oracle can replay

Research notes on how far back the oracle's data sources go, with a focus on
**historical forecast** data (the prediction as issued at the time, not the
reanalysis). Compiled 2026-06-12 from the live API docs at
`open-meteo.com/en/docs/historical-forecast-api` and `brightsky.dev/docs/`.

This file is the reference for any future work that needs to back-test or
re-fit the rules against past forecasts. The "Already shipped" section lists
what we use today; the rest is the open work.

## ✅ Already shipped

The live pipeline (see `src/oracle/pillars/` + `src/oracle/logger.py`):

- **Pillar 1 — Pressure pairs.** Open-Meteo Forecast API, current
  `pressure_msl` at Munich / Innsbruck / Bolzano. Single-shot request, no
  history.
- **Pillar 2 — Meteo.** Open-Meteo Forecast API, hourly variables at Urfeld
  coords (47.5869, 11.3361): cloud_cover, shortwave_radiation,
  wind_speed_850hPa, temperature_2m, dew_point_2m, boundary_layer_height,
  soil_moisture_0_to_1cm, precipitation, cape, lifted_index,
  cloud_cover_low, wind_speed_700hPa, wind_direction_850hPa. Spans day-1
  through target day. No history.
- **Pillar 3 — Live measurements.** Bright Sky (`/current_weather` → DWD
  OpenData, nearest synoptic station) + Addicted-Sports Urfeld scrape
  (buoy-anchored anemometer, ~1.6 m above water at deepest part of the
  lake; page is hosted at the Panoramahotel on shore but wind reading
  itself comes from the buoy). The Urfeld buoy payload carries
  wsavg/wsmax + wtemp/temp/dp/rh/rp/rain — only wind + wtemp are used by
  rules today, the rest are captured for replay.
- **Ground truth log.** `data/runs/<date>.json` (or `gs://$RUNS_BUCKET/runs/`
  in prod) contains historical Urfeld buoy data in `ground_truth.machine`
  for records written before 2026-06-22. No new machine ground truth is
  written — Addicted-Sports refused data permission; `oracle backfill` and
  the Cloud Scheduler job are removed/paused.

### On the "buoy" naming

The "Urfeld station" the oracle scrapes is a **buoy-anchored anemometer** mid-lake,
~1.6 m above the water. Addicted-Sports hosts the page at the Panoramahotel
Karwendelblick on the shore, and the webcam + air/water/humidity/pressure
sensors are also at the hotel, but the wind reading comes from the buoy.
Direction is not exposed. The webcam and the anemometer share an outage
mode — a missing Urfeld reading is not a bug.

## Data-source inventory (live pipeline)

| Pillar | Service | Underlying data origin | What we get | Source file |
|---|---|---|---|---|
| Pressure | Open-Meteo Forecast API | ECMWF IFS / GFS | MSL pressure at Munich, Innsbruck, Bolzano | `pillars/pressure.py` |
| Meteo | Open-Meteo Forecast API | ECMWF IFS / GFS | 13 hourly variables at Urfeld coords | `pillars/meteo.py` |
| Measurements | Bright Sky | DWD OpenData (SYNOP, hourly climate) | Latest 10 m wind from nearest DWD station | `pillars/measurements.py` |
| Measurements | Addicted-Sports (private scrape) | Urfeld buoy anemometer + hotel weather sensors | wsavg/wsmax + wtemp/temp/dp/rh/rp/rain | `pillars/measurements.py` |
| Ground truth | GCS / local | Urfeld buoy | Peak avg/gust, ignition time, ≥8 kt / ≥12 kt duration | `logger.py` |

Open-Meteo is a *free aggregator* over ECMWF IFS and NOAA GFS — it is not a
measurement network. For the DWD station data directly (not just model
output), Bright Sky is the only path we use.

## Historical data depth — what the providers actually expose

The user-facing question is *"can I get the forecast the oracle would have
produced for date X in the past, then compare to what actually happened?"*.
The answer differs sharply by source.

### Open-Meteo — four products, only one is a forecast archive

| Product | Endpoint host | What it is | Start | Forecast? |
|---|---|---|---|---|
| **Historical Weather API** | `archive-api.open-meteo.com` | Reanalysis (ERA5 / ERA5-Land / IFS analysis). Model re-run with full observation assimilation. | ERA5 **1940**, ERA5-Land **1950**, IFS analysis **2017** | No — reanalysis |
| **Historical Forecast API** | `historical-forecast-api.open-meteo.com` | Continuous hourly timeseries stitched from the **first hours of each model run**. Each run is initialized from real measurements, so it tracks actual conditions closely. | **~2021 / 2022** depending on model | **Yes** ✅ |
| **Previous Runs API** | `previous-runs-api.open-meteo.com` | Same data, but at fixed lead-time offsets (1, 2, 3, … 7 days ahead) — for forecast-skill evaluation. | Jan 2024 (GFS Mar 2021, JMA 2018) | Yes, but wrong shape |
| **Single Runs API** | `single-runs-api.open-meteo.com` | Full forecast horizon of a specific run, selected by `run=YYYY-MM-DDThh:mm`. | IFS HRES Mar 2024; others Sep 2025 | Yes, but too recent |

The Forecast API and the Historical Forecast API share the same query
schema (same variables, same params, same response format) — only the
host differs. Migration is one line in each pillar.

**Model coverage at Walchensee coords (47.58°N, 11.34°E)** — from
`open-meteo.com/en/docs/historical-forecast-api`:

| Model | Region / resolution | Update cadence | Archived from | Notes |
|---|---|---|---|---|
| **ECMWF IFS HRES** | Global, 9 km (O1280) | 6-hourly | **2017-01-01** | Best global option for 2016+ gap-fill |
| NOAA GFS | Global, 0.25° (~25 km) | 6-hourly | 2021-03-23 | Coarser than IFS |
| NOAA GFS Pressure | Global, 0.25° | 6-hourly | 2021-03-23 | Pressure-level vars (we'd want this for the 850 / 700 hPa winds) |
| NOAA HRRR | US only, 3 km | hourly | 2018-01-01 | Not relevant — CONUS only |
| **DWD ICON** | Global, 0.1° (~11 km) | 6-hourly | **2022-11-24** | The German national model — best regional coverage for Bavaria |
| **DWD ICON-EU** | Europe, 0.0625° (~7 km) | 3-hourly | **2022-11-24** | Better than ICON global for the Alps |
| **DWD ICON-D2** | Central Europe, 0.02° (~2 km) | 3-hourly | **2022-11-24** | Best spatial match for the lake |
| JMA GSM | Global, 0.5° | 6-hourly | 2016-01-01 | Wrong hemisphere focus for Alps thermal |
| JMA MSM | Japan, 0.05° | 3-hourly | 2016-01-01 | Irrelevant — Japan only |
| ECMWF IFS 0.4° | Global, 0.4° | 6-hourly | 2022-11-07 | Lower-resolution IFS — superseded by HRES |
| ECMWF IFS 0.25° | Global, 0.25° | 6-hourly | 2024-02-03 | Newer than HRES for some vars |
| AIFS / HGEFS / AIGFS | Global ML models | 6-hourly | 2024–2026 | Not yet relevant for back-testing |
| Météo-France ARPEGE / AROME | France / Europe | 6–12-hourly | 2022–2024 | Adjacent, but French-side |

**Practical takeaways for back-testing from 2016:**

- **2016**: no forecast archive. Reanalysis (ERA5) via Historical Weather API is the only option; understand it's "what really happened" not "what the model predicted". For the Walchi rules, which use single-morning windows (no lead-time skill), this is actually fine for re-fitting thresholds.
- **2017-01-01 → 2021-03-22**: ECMWF IFS HRES via Historical Forecast API. Single global model, 9 km. Surface variables are complete (cloud, radiation, 2 m temp/dew, precipitation, low cloud); the **pressure-level fields the oracle uses** — `wind_speed_850hPa`, `wind_speed_700hPa`, `wind_direction_850hPa`, `lifted_index`, `cape`, `boundary_layer_height`, `soil_moisture_0_to_1cm` — are all `None` for this era, so the `upper_level_wind`, `synoptic_override`, `atmospheric_stability`, `boundary_layer_height`, and `post_rain_moisture` rules emit `MAYBE no signal` rather than crashing. Verdict is partial.
- **2021-03-23 → 2022-11-23**: ECMWF IFS HRES + GFS + GFS Pressure via Historical Forecast API. Two-model ensemble is possible; `wind_speed_850hPa` etc. become available. Soil moisture / BLH still partial.
- **2022-11-24 → today**: full DWD ICON / ICON-EU / ICON-D2 + IFS HRES + GFS available. The 2 km ICON-D2 is the most faithful match for the lake's local thermal, and all oracle variables are populated.
- **For 2016 specifically**: the 12-month gap would have to be filled by either ERA5 reanalysis (different product) or by self-hosting an ECMWF MARS extraction of the IFS HRES runs (free for non-commercial research, but not on Open-Meteo).

### Bright Sky / DWD — observations only, no forecast archive

From `brightsky.dev/docs/`:

> **Historical coverage**: Bright Sky serves historical data going back to
> **January 1st, 2010**. If you need data that goes further back, check out
> our [infrastructure repository](https://github.com/jdemaeyer/brightsky-infrastructure)
> to easily set up your own instance of Bright Sky!

Bright Sky has **no historical forecast archive** exposed via the public
API. The `/weather` endpoint's `source=forecast` parameter lets you *choose*
between observations and MOSMIX model output, but it only returns the
current/upcoming forecast — never a past one.

- **Historical station observations**: 2010-01-01 → present (DWD SYNOP,
  hourly climate observations, POI). Useful for re-evaluating the
  `live_measurements` rule retroactively. Not useful for re-forecasting
  past days.
- **Forecast**: only the current run, ~10 days ahead, MOSMIX.
- **DWD ICON model archive** on `opendata.dwd.de` is limited to recent
  years; not a clean public time-series for 2016+.

If we ever need the DWD forecast as-issued for past dates, we'd have to
hit DWD's open-data server directly (no Bright Sky wrapper) and likely
self-archive — out of scope for a casual back-test.

## Open work

### Decide what "back-test" means for the rules

The rules in `src/oracle/knowledge/rules.py` consume the *current
conditions* of a single morning window (09:00–13:00). They do **not**
consume a forecast lead time. That means:

- For **re-fitting thresholds** (the work the threshold-tuner
  in `config.py` is preparing for), the Historical Weather API
  (ERA5 reanalysis) is a fine input. The reanalysis values are
  "what really happened", which is what the rules implicitly assume
  their inputs are. Available 1940+.
- For **re-evaluating the oracle as a user would have seen it**
  (forecasts issued the day before, with skill degrading over the
  forecast horizon), the Historical Forecast API is the right input.
  Coverage starts 2017 for IFS HRES, 2022 for DWD ICON.

The current oracle runs the **Forecast API** in production, so the
closest faithful replay of "what the oracle would have predicted" is
the Historical Forecast API's first-hour stitches (≈ what an overnight
forecast looks like at lead time 0).

### Code change to support replay

> **Shipped 2026-06-12**: `oracle replay --day <date>` (single day, scrapes
> the buoy live) and `oracle replay --from <date> --to <date>` (batch —
> two archive requests per year via `src/oracle/replay.py`, buoy curve
> reconstructed from the stored ground truth, per-day holes skipped and
> reported). `--models` pins a model for cross-era scoring runs. Replay
> pressure samples 08:00 Europe/Berlin, the live job's sampling hour.
>
> **Scoring (the join)**: `oracle calibrate --replayed` scores the replay
> verdicts against the ground truth in the matching main records;
> `--csv` exports the joined feature/outcome rows for offline ML. The
> tuning loop after a threshold change is `oracle rescore --replayed`
> (re-scores replay records from stored inputs, no API traffic) followed
> by `oracle calibrate --replayed --resimulated`. Caveat: the storm
> quarantine reads the lifted index from the replay inputs, which is
> None pre-2021 — gust-front days in that era are not quarantined.

Swap the host in `pillars/pressure.py` and `pillars/meteo.py`:

```python
# production
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
# replay (forecast-as-issued)
OPEN_METEO_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# replay (reanalysis)
OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/forecast"
```

The query schema is identical (same variables, same parameters). For
the Historical Forecast API, the default `Best Match` model selection
picks the highest-resolution model available for the location; pass
`&models=...` to pin a specific model when comparing across dates
(don't mix IFS HRES and DWD ICON in the same scoring run — they're
different models with different biases).

Caveat for the Historical Forecast API: *"Not suitable for long time
series due to model version changes over time."* If we want to compare
2017 vs 2024 sessions, expect step-changes where ECMWF upgraded the
IFS cycle. Document the cycle version per scoring window in the log.

### 2016 gap

If 2016 is genuinely required, two options:

1. **Use Historical Weather API (ERA5 reanalysis) for 2016 only.** Different
   product, but the Walchi rules are not lead-time sensitive so the
   mismatch is small. Be explicit in the scoring run metadata.
2. **Self-extract ECMWF MARS IFS HRES runs for 2016.** Free for
   non-commercial research on the CDS, but requires
   `cdsapi` + an account and storage. Out of scope unless the
   back-test specifically needs 2016.

### Addicted-Sports / Urfeld buoy

The Urfeld buoy scrape (`_fetch_urfeld_entries`) supports an arbitrary
`window_start` / `window_end` window, so retrospective pulls for any
date the server has data for are already supported by
`fetch_urfeld_day_curve`. Constraint is the server's own archive depth,
not the oracle's. The server has been up since **2016-06-01** (verified
2026-06-12 by direct probe — earlier years return "No Weatherdata
available"); see `CHANGELOG.md` for the bulk historical backfill
(2026-06-12) that pulled ~3,600 in-season days into the calibration
bucket. For the IFS-HRES 2017-01-01 → today forecast window the buoy
ground truth is therefore available across the full 9+ year overlap.
