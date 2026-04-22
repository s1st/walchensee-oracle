# Forecast Factors

Factors that improve forecast accuracy beyond a plain
"pressure + radiation + wind" triplet. The "Already shipped" section lists
what the model uses today; the rest remains open work ordered by
expected-value-over-effort.

## ✅ Already shipped

The implementation details live in `src/oracle/pillars/meteo.py` and
`src/oracle/knowledge/rules.py`. Each item below maps to one rule.

### Dew point spread / humidity ✅

Min `(T − Td)` across 09:00–13:00. Below 5 °C → NO_GO (moisture-suppressed);
5–8 °C → MAYBE; ≥ 8 °C → GO. Data: Open-Meteo `dew_point_2m` + `temperature_2m`.

### Boundary layer height ✅

Max BLH across 09:00–13:00. Below 600 m → capped thermal (NO_GO); 600–1000 m →
shallow mixing (MAYBE); ≥ 1000 m → deep mixing (GO). Data: Open-Meteo
`boundary_layer_height`.

### Soil moisture + precipitation history ✅

NO_GO if ≥ 2 mm rain fell yesterday OR current `soil_moisture_0_to_1cm` is
above 0.35 m³/m³. GO otherwise. Captures the "2nd sunny day after rain" rule.
Data: Open-Meteo `soil_moisture_0_to_1cm` + `precipitation` (with day-of
window).

### Atmospheric stability (CAPE / Lifted index) ✅

LI ≥ +6 over the morning window → atmosphere too stable, thermal capped
(NO_GO); LI ≤ −2 → thunderstorm risk destroys the cell (NO_GO); otherwise GO.
Raw CAPE is captured in the log too for future calibration. Data: Open-Meteo
`lifted_index`, `cape`.

### Daytime low-cloud development ✅

Max `cloud_cover_low` during 09:00–13:00. Above 60 % shades the
Herzogstand/Jochberg slopes (NO_GO); below 30 % the slopes are in full sun
(GO); between = MAYBE. Complements `overnight_cooling` which only measures
night-time total cloud cover.

### Multi-level wind shear + direction ✅

Combined rule `upper_level_wind`: NO_GO if 850 hPa wind at the morning's peak
hour blows from 150–210° (SSE, counters the N-thermal) OR if 700 hPa max wind
exceeds 25 kt (crossflow decouples the valley-wind system). Complements the
existing `synoptic_override` which only looks at 850 hPa speed.

## 🔜 Still open

### Lake surface temperature

Walchensee is 192 m deep; surface temperature lags air temperature
significantly. In spring the lake is 6-10 C while air reaches 15-20 C,
creating a cold-surface dome that opposes the incoming thermal flow (a mini
lake breeze). In late summer (17-22 C surface) this opposition diminishes.

- **Data:** Bavarian Water Authority or wassertemperatur.org (external scrape);
  or a seasonal lookup table (Jan ~4 C, Apr ~7 C, Jun ~14 C, Aug ~20 C)
- **Rule sketch:** air-lake delta > 10 C = penalize ignition timing;
  delta < 5 C = no opposition
- **Effort:** Medium — new data source required.

### Snow cover on surrounding peaks

Snow-covered slopes reflect 60-90% of solar radiation instead of absorbing it,
drastically reducing the slope heating that drives the thermal. Also produces
stronger katabatic drainage that delays thermal onset.

- **Data:** Open-Meteo `snow_depth` at Herzogstand/Heimgarten coordinates
- **Rule sketch:** snow depth > 0 at > 1200 m in the catchment = seasonal
  dampening factor. Strongest effect March-May.
- **Effort:** Medium — requires second-location Open-Meteo fetch.

### Kesselberg channeling (Walchensee-specific)

The Kesselberg pass (858 m) connects the Kochelsee basin to Walchensee. Air
from the north is channeled through this bottleneck (Venturi effect). A cold
air pool in the Kochelsee basin can dam up against the Kesselberg and block
thermal flow-through.

- **Data:** Open-Meteo temperature/wind for Kochelsee coordinates (47.65 N,
  11.37 E)
- **Rule sketch:** negative temperature gradient across the Kesselberg (cold
  air dammed below) = thermal flow blocked
- **Effort:** Medium — second location fetch + new rule.

### Seasonal threshold calibration

The ignition window (10:30-15:00) and solar radiation threshold (600 W/m2) are
fixed but should vary by season. June sun delivers ~1000 W/m2 peak at 65 deg
elevation; September only ~700 W/m2 at 45 deg. The thermal window stretches
to 17:00 in midsummer but contracts to 14:30 in late September.

- **Data:** Astronomical calculation or monthly lookup table.
- **How:** Parameterize existing thresholds by month or solar declination.
- **Effort:** Low — no new data source needed.

## Lower priority

### 850 hPa temperature (lapse rate proxy)

Cold 850 hPa relative to surface = steep lapse rate = atmosphere supports deep
thermals. Warm 850 hPa = stable, thermals capped. The paragliding "trigger
temperature" concept uses this to predict thermal onset.

- **Data:** Open-Meteo `temperature_850hPa`, `temperature_2m`
- **Effort:** Low.

### Morning katabatic drainage strength

Before the thermal ignites, katabatic drainage from surrounding peaks is the
opposing wind regime. A short "calm window" between drainage death and thermal
onset indicates a powerful thermal day. Extended calm (> 2h) or drainage past
10:30 suggests failure.

- **Data:** Already-fetched shore station wind data — needs temporal analysis.
- **Effort:** Medium — requires intra-day pattern detection.

### DWD Blauthermik (glider thermal forecast)

The DWD Luftsportbericht includes a "Blauthermik" (blue thermal = thermal
without cumulus formation) indicator used by the glider community. It's
essentially a professional thermal quality forecast. The windsurfing community
already uses it informally.

- **Data:** DWD aviation weather products (would need scraping or API access).
- **Effort:** Medium-high — new external source.

### Visibility / haze

Low visibility (< 20 km) indicates aerosol loading that scatters solar
radiation and weakens surface heating. Partially redundant with the solar
radiation measurement but helps diagnose ambiguous situations.

- **Data:** Open-Meteo `visibility`
- **Effort:** Low.

## Community forecasting resources

Sources used by the Walchensee windsurfing community that the oracle could
integrate or reference:

| Resource | What it provides |
|---|---|
| **DWD Luftsportbericht** | Thermal quality / Blauthermik indicator |
| **kitewetter.at** (Walchensee page) | AROME 1.3 km + ICON-D2 model output, thermal quality index, Föhn pressure diagrams |
| **windinfo.eu** webcam | Visual pattern comparison to archived good-wind days |
| **Windfinder** | Historical station data (Walchensee station offline since ~2010; nearby Krün/Mittenwald active) |
| **wassertemperatur.org** | Lake surface temperature data |
