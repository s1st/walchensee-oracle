# Walchensee Thermal Wind Model

This document captures the domain knowledge behind the oracle's forecast logic.
Walchensee (802 m ASL, 16 km², 192 m deep) is one of Germany's premier
windsurfing lakes — often called the "German Lake Garda". Nearly all usable
wind is thermal; standard forecast models (GFS, ECMWF) do not resolve it.

## How the thermal works

The thermal is driven by **slope heating** on the south-facing flanks of
Herzogstand (1,731 m) and Jochberg (1,565 m). As these slopes warm through
the morning, air rises along them, creating a low-pressure draw that pulls
cool air from the Kochelsee basin (600 m) to the north. This air is
compressed through the narrow saddle between the two peaks — the
**Düseneffekt** (funnel/jet effect) — producing a consistent N to NNE wind
across the lake.

The windsurfing community calls this the **"Thermik"** or
**"Walchensee-Thermik"**. Meteorologists call the larger-scale cross-Alps
pumping (Munich toward Innsbruck) the "Alpenpumpe" — the synoptic driver
behind the local thermal cell. The codebase follows the community and uses
**"Thermik"** for this pressure signal, noting the meteorological term in
comments where disambiguation helps.

### The formula

> Cold night + clear morning sun on the slopes + large day/night temperature
> delta = thermal wind by midday.

## Triggering conditions

What you need:

- **Clear, starry night** — strong radiative cooling builds the temperature
  inversion that the next day's solar heating must break. The deeper the
  overnight cooling, the more energy is stored for the thermal engine.
- **Unobstructed morning sun on Herzogstand/Jochberg slopes** — clouds over
  the lake and Karwendel range significantly degrade conditions. Clouds
  elsewhere matter less.
- **Large day-night temperature differential** — the bigger the delta, the
  stronger the wind. Spring and autumn excel here; midsummer struggles because
  warm nights shrink the delta.
- **Stable high-pressure system** — ensures clear skies and suppresses
  synoptic-scale disturbances.
- **Light background wind from NW/N/NNE (< 3 Bft)** — a weak northerly or
  NE regional flow amplifies the thermal by aligning with its natural
  direction. Stronger synoptic winds (>= 3 Bft) deform or destroy the
  thermal cell.

## What kills the thermal

- **Föhn (south wind):** Blows from the opposite direction and directly
  counteracts the thermal. The Bolzano-Innsbruck pressure gradient is the
  detection signal (positive delta >= ~4 hPa = Föhn risk).
- **South or SE winds aloft:** Warm Mediterranean air suppresses thermal rise
  even without full Föhn conditions.
- **Clouds over the lake/Karwendel:** Block solar heating of the critical
  slopes.
- **Extended heat waves (July-September):** Upper atmosphere already warm,
  shrinking the temperature differential. Thermal potential degrades after
  several consecutive hot days.
- **Post-rain moisture:** Wet soil diverts solar energy into evaporation
  instead of sensible heating. The "2nd or 3rd sunny day after rain" rule is
  well-known — full thermal strength returns once the ground dries out.

## Timing and spatial progression

### Onset

| Conditions | Typical ignition |
|---|---|
| Spring, after cold night | 10:30 - 11:30 |
| Normal summer day | 11:00 - 14:00 |
| Extended hot spell | 13:00 or later |

### Spatial pattern (N to S)

The wind appears **first at the north end** (Urfeld / Galerie) and spreads
southward like a hand with splayed fingers. The timing difference between
north and south spots can exceed 2 hours.

| Spot | Onset | Offset | Character |
|---|---|---|---|
| Urfeld / Schweinebucht | First | ~15:30 | Most reliable onset, strong but short |
| Galerie | Shortly after Urfeld | Slightly longer | Strongest wind, gusty (compressed zone) |
| Sachenbacher Bucht | Similar to Galerie | Similar | ~1 Bft stronger than Galerie, most stable quality |
| Wiese / WCW Center | Last | 17:00 - 18:00 | Longest session, late NW shift from Herzogstand fallwind |

### Visual cue

A distinct **Kabbelwasser** (choppy water stripe) is visible from shore,
contrasting with flat surrounding water. This signals thermal arrival roughly
1 hour before it reaches the shore. The webcam at windinfo.eu facing NE
toward Sachenbacher Bucht and Jochberg is the primary visual forecasting
tool.

## Seasonal patterns

| Season | Thermal quality | Notes |
|---|---|---|
| Spring (Apr-May) | Peak season, 5-6 Bft possible | Maximum day-night delta. Lake still very cold (6-10 C), creating cold-surface opposition but also strong inversion. |
| Early summer (Jun) | Good | Still large deltas before prolonged heat sets in. |
| Midsummer (Jul-Aug) | Weaker, 3-4 Bft typical | Warm nights reduce the delta. Lake surface warms to 17-22 C, reducing cold-surface opposition. |
| Autumn (Sep-Oct) | Second peak | Large deltas return. Also Föhn season — rare "Big Days" with genuine strong southerly wind (very gusty, minimal warning). |

### Nebel sessions (fog events)

In autumn, lowland fog covers the lake (800-1000 m elevation) while mountain
peaks above get sunshine. Explosive thermals develop within/below the fog
layer, reaching 5-7 Bft. Visibility critically low — experienced surfers
only.

## Pressure pairs

### Thermik (Alpenpumpe): Munich minus Innsbruck

The large-scale thermal pump from the Bavarian plains toward the Alps. A
positive delta (Munich higher than Innsbruck) indicates air flowing southward
toward the mountains — favourable for the thermal.

- **Threshold:** >= 2.5 hPa (informed guess; Garda uses ~3 hPa but
  Walchensee's thermal cell is smaller and needs less driving force).
- **Stations:** Munich (48.14 N, 11.58 E) vs. Innsbruck (47.27 N, 11.40 E).
- **Backend:** Open-Meteo MSL-reduced pressure so elevation differences don't
  swamp the signal.

### Föhn: Bolzano minus Innsbruck

A positive delta indicates southerly pressure forcing across the Brenner —
Föhn risk. Innsbruck serves as north anchor for both pairs.

- **Threshold:** >= 4.0 hPa positive = Föhn risk, thermal suppressed.
- **Stations:** Bolzano (46.50 N, 11.35 E) vs. Innsbruck.

## Meteorological pillars

### Overnight cloud cover (22:00 - 06:00)

Mean cloud cover during the previous night. Clear skies allow strong
radiative cooling and deep inversion buildup.

- **Threshold:** <= 30% mean cloud cover = good inversion.
- **Source:** Open-Meteo hourly `cloud_cover` at Urfeld coordinates.

### Morning solar radiation (09:00 - 13:00)

Peak hourly shortwave radiation during the morning heating window. This is
the energy that drives slope heating and powers the thermal cell.

- **Threshold:** >= 600 W/m² peak hourly.
- **Source:** Open-Meteo hourly `shortwave_radiation` at Urfeld.
- **Caveat:** threshold should vary seasonally (June peaks ~1000 W/m²,
  September ~700 W/m²). Currently fixed.

### Synoptic wind at 850 hPa (09:00 - 13:00)

The wind above the boundary layer. If this is already strong, it overrides
and destroys the local thermal cell regardless of surface conditions.

- **Threshold:** >= 15 kt (roughly 3 Bft) = thermal override.
- **Source:** Open-Meteo hourly `wind_speed_850hPa` at Urfeld.

## Live measurements

### Bright Sky (DWD)

Nearest synoptic station (~13 km south of the lake, typically
Mittenwald/Obb.). Provides wind speed, gust, and direction. Used as an
ignition reference — the DWD station picks up the thermal early.

### Addicted-Sports Urfeld

Private anemometer on the Panoramahotel Karwendelblick buoy at Urfeld. The
actual shore reading. Scraped via a CSRF-guarded JSON endpoint. Direction is
not exposed.

- **Ignition threshold:** >= 8 kt average = thermal has fired.

### Missing: Galerie and Sachenbach

Config defines these as shore stations but no fetcher is wired yet. Both are
needed to track the N-to-S ignition propagation pattern.

## Community data: windinfo.eu chat

The Wind-Wetter-Chat on windinfo.eu is a WordPress "Wise Chat Pro" plugin
behind a login. The oracle scrapes it for messages mentioning Walchensee
keywords (walchensee, walchi, urfeld, galerie, nordufer, sachenbach, wiese,
zwergern, einsiedl, kesselberg, herzogstand, jochberg).

Currently display-only — messages are shown but no structured signal is
extracted for the forecast verdict.

## Threshold calibration status

All thresholds are informed guesses based on Garda analogues and local kiter
heuristics. None have been validated against logged Walchensee observations.
An observation logging system is needed to record (inputs, actual conditions)
pairs and enable data-driven calibration.

| Threshold | Current value | Confidence | Notes |
|---|---|---|---|
| Thermik delta | >= 2.5 hPa | Low | Garda uses ~3; scaled down for smaller cell |
| Föhn trigger delta | >= 4.0 hPa | Medium | Well-established Föhn indicator |
| Synoptic override | >= 15 kt | Medium | Standard ~3 Bft threshold |
| Ignition wind | >= 8 kt | Low | Needs shore-station validation |
| Overnight cloud cover | <= 30% | Low | Guess; needs seasonal adjustment |
| Morning solar radiation | >= 600 W/m² | Low | Must vary with season |
