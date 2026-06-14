# Future Buoy Signals

The Addicted-Sports Urfeld buoy exposes a much richer sensor set than the
oracle currently consumes. The CSRF-guarded JSON endpoint at
`getWeatherData.php` returns one entry per 10-minute sample, with the
following fields (per the JS source of the webcam page's media player view
at `/fileadmin/webcam/src/mediaPlayerWebcamView.js`):

| Field | Unit | Physical meaning |
|---|---|---|
| `wsavg` | kts | Wind speed, 10-min average |
| `wsmax` | kts | Wind speed, 10-min peak/gust |
| `temp` | °C | Air temperature at the camera location (Panoramahotel pier, ~830 m) |
| `wtemp` | °C | Water temperature |
| `dp` | °C | Dew point |
| `rh` | % | Relative humidity |
| `rp` | hPa | Local pressure (NOT MSL-reduced; altitude correction needed for cross-station comparison) |
| `rain` | mm | Local rain gauge (interval amount) |

**Status (2026-06-14):** the scraper now *parses and logs* all of these
fields — `measurements.py` surfaces `wtemp`, `temp`, `dp`, `rh`, `rp` and
`rain` as optional fields on both `WindReading` and `UrfeldSample`, so the
historical record carries them. What's still open is *rule consumption*:
only `wsavg`/`wsmax` (`thermal_ignition`) and `wtemp` (`air_lake_delta`)
drive a verdict today. The rest are captured-but-not-yet-wired — available
for calibration and the ML training export, but no rule reads them. (The
older framing of this doc — "everything except wind is dropped on the
floor" — is obsolete; the fields are captured, just not yet acted on.)

This document is a catalog of what could be done with the captured-but-
unused fields — the physical/forecast signal each could power, and what
would need to change to turn it into a rule or feature.

## Local humidity (`temp` + `dp`, or `rh`)

The current `dew_point_spread` rule uses Open-Meteo's 2 m temperature
and dew point from the grid cell centered on Urfeld. The buoy gives the
same measurement at the actual sensor location, on the lake, with a
10-minute temporal resolution. The lake's microclimate — cold-air pool
in winter, lake-breeze boundary in summer — can differ from the
Open-Meteo grid cell in ways that matter for thermal onset.

What it could power: a tighter, more local `dew_point_spread` rule.
Implication: the existing rule was fitted against Open-Meteo data
(n=22 noted in `config.py`). Switching sources requires re-fitting the
threshold from scratch.

## Local rain (`rain`)

Open-Meteo's grid precipitation is a coarse proxy for "did it rain
here?". A 0.5 mm grid-cell average can be entirely dry at Urfeld, or
miss a localized thunderstorm. The buoy's rain gauge is a point
measurement at the actual location.

What it could power: the `post_rain_moisture` rule. The current
`rained_yesterday` flag was *demoted* from a veto to a log field after
n=17 calibration showed 13 false positives (post-frontal days at
Walchensee fire fine once the sun is out; genuinely washed-out days
are caught by soil/cloud/solar). A local reading may tell a different
story than the grid cell average.

Implication: requires re-fitting the rule's thresholds against
local-rain inputs, not grid-rain inputs.

## Local air temperature (`temp` alone)

Open-Meteo gives forecast air temperature for the 09:00–13:00 window.
The buoy gives the *current* air temperature at the sampling site. Lake
temperature changes slowly enough that the most recent buoy reading is
a good proxy for tomorrow's — air temperature does not, so this signal
is mostly useful for "what's happening right now" display, not for
forecasting a day or two out.

What it could power: the live dashboard's current-conditions panel
(alongside wind and water temp). Limited rule-layer value because
Open-Meteo forecasts remain the better source for the target day.

## Local pressure (`rp`)

The existing pressure pillar uses cross-Alps *gradients* (Munich −
Innsbruck, Bolzano − Innsbruck) to drive the Thermik and Föhn rules.
A local absolute pressure reading does not contribute to those
gradients, and would need altitude correction to MSL to be comparable
to the Open-Meteo anchors.

What it could power: possibly a sanity check on the Open-Meteo
pressure pillar (catch drift), or a tertiary input to a future rule.
Marginal value.

## Time-series analysis on the wind curve

Beyond "what is the latest reading?", the buoy gives a continuous
10-minute time series. `fetch_urfeld_day_curve` already pulls the full
day; what we don't currently do is any pattern detection across the
curve.

What it could power:

- **Morning katabatic drainage strength.** Before the thermal ignites,
  katabatic drainage from surrounding peaks is the opposing wind
  regime. A short "calm window" between drainage death and thermal
  onset indicates a powerful thermal day. Extended calm (> 2h) or
  drainage past 10:30 suggests failure. Already on the backlog in
  `docs/future-factors.md`.
- **Ignition timing precision.** `thermal_ignition` currently uses a
  fixed 10:30–15:00 window from `config.IGNITION_WINDOW_LOCAL`. The
  actual first-ignition time per day is a learnable signal.
- **Pre-frontal vs. post-frontal signatures.** Wind direction and
  gust pattern may distinguish "rainy day that won't fire" from
  "rainy day that clears up" before the rule layer commits to a
  verdict.

## The air-lake delta: shipped (on `main`)

`wtemp` was the first non-wind field the oracle wired into a rule. The
`air_lake_delta` rule uses forecast air temperature (Open-Meteo) minus
current water temperature (buoy) to detect the cold-lake regime that
opposes the thermal in spring. See `src/oracle/knowledge/rules.py` and
`docs/thermal-model.md`. It remains the only non-wind buoy field consumed
by a rule; the others above are logged but not yet acted on.

## Cross-cutting concerns

- **Source criticality.** Pressure and meteo are "critical" pillars
  (their failure propagates). Measurements, including the buoy, are
  "tolerant" — a missing buoy reading is dropped, not a fatal error.
  Any rule that starts to depend on a buoy field inherits that
  tolerance: if the buoy is down, the rule simply doesn't fire.
- **Calibration discipline.** Every existing threshold in `config.py`
  with an `n=` note was fitted against the data source the rule
  currently uses. Swapping to a buoy source for an existing rule
  invalidates that fit. Per `CLAUDE.md`: "demand the offender list
  from a sample of ≥10 ground-truthed days, then change one
  threshold per commit so the rescore-strip in the dashboard
  isolates the effect."
- **Backfill / ground truth.** The buoy's day-curve is already pulled
  by `backfill_run` and stored in `ground_truth.machine`. Any new
  buoy-fed signal can be added to the same backfill path to seed
  historical ground truth for the eventual calibration.
