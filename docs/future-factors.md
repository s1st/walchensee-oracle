# Future Forecast Factors

Additional factors that could improve forecast accuracy beyond the current
model. Prioritized by expected impact and implementation effort. All Open-Meteo
fields mentioned below have been confirmed available.

## High priority

### Dew point spread / humidity

The dew point spread (T - Td) controls two things: how much solar energy goes
into sensible heating (thermal driving force) vs. latent heating (evaporation),
and the cloud base height (early cumulus overdevelopment risk).

- **Data:** Open-Meteo `dew_point_2m` for 09:00-13:00
- **Rule sketch:** spread < 5 C = NO_GO (moisture-suppressed); 5-8 C = MAYBE;
  > 8 C = GO
- **Effort:** Low — one additional field + one rule.

### Boundary layer height

The depth of the convectively mixed atmosphere. Determines how deep the thermal
cell can become. A suppressed boundary layer (e.g. under a persistent
inversion) prevents the valley-wind system from developing.

- **Data:** Open-Meteo `boundary_layer_height`
- **Rule sketch:** max BLH < 600 m = NO_GO; 600-1000 m = MAYBE; > 1000 m = GO
- **Effort:** Low — one additional field + one rule.

### Soil moisture + precipitation history

Wet soil diverts solar energy into evaporation. The well-known "2nd or 3rd
sunny day after rain" rule applies strongly at Walchensee. Vegetation
transpiration stays elevated for 2-3 days post-rain.

- **Data:** Open-Meteo `soil_moisture_0_to_1cm`, `precipitation` with
  `past_days=3`
- **Rule sketch:** rain yesterday or soil moisture > 0.35 m3/m3 = strong
  dampening; 2+ dry days = no penalty; 3+ dry days = bonus
- **Effort:** Low — expand existing Open-Meteo fetch.

## Medium priority

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

### CAPE / Lifted index (convective instability)

The atmosphere's raw potential for vertical motion. Modulates thermal strength:
too stable = capped thermals; moderately unstable = strong thermals; very
unstable = thunderstorm risk and thermal destruction.

- **Data:** Open-Meteo `cape`, `lifted_index`
- **Rule sketch:** LI > 6 = too stable (NO_GO); 2-6 = normal; 0 to -2 =
  strong support; < -2 = storm risk (NO_GO)
- **Effort:** Low — add two fields + one rule.

### Cloud cover by level (daytime development)

The current model only checks overnight total cloud cover. Daytime development
of low clouds is what actually kills the thermal mid-session. The *timing* of
cumulus build-up determines how long the wind window lasts.

- **Data:** Open-Meteo `cloud_cover_low`, `cloud_cover_mid`, `cloud_cover_high`
- **Rule sketch:** low cloud > 60% before 13:00 = window shortened; stays
  below 30% through 15:00 = full window
- **Effort:** Low — three additional fields + one rule.

### Multi-level wind shear + direction

850 hPa speed alone isn't enough. Wind direction at 850 hPa matters (SSE flow
opposes the N-to-S thermal; NW flow reinforces it). Strong 700 hPa crossflow
perpendicular to the valley axis decouples the valley-wind system.

- **Data:** Open-Meteo `wind_speed_700hPa`, `wind_speed_500hPa`,
  `wind_direction_850hPa`
- **Rule sketch:** 850 hPa from 150-210 deg = thermal opposition; 700 hPa
  crossflow > 25 kt = decoupled valley wind (NO_GO)
- **Effort:** Low-medium.

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
