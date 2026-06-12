"""Static configuration: stations, thresholds, endpoints.

Thresholds are mixed provenance: several driver rules have been data-fitted
against the Urfeld calibration log (each carries an inline ``n=`` note below),
while the rest are still research-informed guesses (Garda analogues + local
kiter heuristics) awaiting enough ground truth to fit. Treat any threshold
*without* an ``n=`` note as provisional.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from enum import Enum


class StationRole(str, Enum):
    THERMIK_NORTH = "thermik_north"  # north-of-Alps pressure anchor
    THERMIK_SOUTH = "thermik_south"  # south-of-Alps pressure anchor
    FOEHN_NORTH = "foehn_north"            # north side of the Föhn pressure pair
    FOEHN_SOUTH = "foehn_south"            # south side of the Föhn pressure pair
    IGNITION_REFERENCE = "ignition_reference"  # first station to show the thermal
    RIDGE = "ridge"                        # summit, used to detect Föhn aloft
    SHORE = "shore"                        # on-lake station (Urfeld/Galerie/…)


@dataclass(frozen=True)
class Station:
    name: str
    lat: float
    lon: float
    role: StationRole
    provider_id: str | None = None


# --- Pressure-pair anchors ------------------------------------------------
# Thermik (meteorology term: "Alpenpumpe"): north minus south drives the
# cross-Alps thermal engine. The community just calls it "Thermik".
MUNICH = Station("Munich", 48.1374, 11.5755, StationRole.THERMIK_NORTH)
INNSBRUCK_N = Station("Innsbruck", 47.2692, 11.4041, StationRole.THERMIK_SOUTH)

# Föhn pair: Bolzano (south) minus Innsbruck (north) positive = Föhn risk.
# Innsbruck appears in both pairs — south for Thermik, north for Föhn.
INNSBRUCK_F = Station("Innsbruck", 47.2692, 11.4041, StationRole.FOEHN_NORTH)
BOLZANO = Station("Bolzano", 46.4983, 11.3548, StationRole.FOEHN_SOUTH)

# --- Local wind stations around Walchensee --------------------------------
# Krün is ~10 km west of the lake and is the station locals watch for the
# first ignition gust. Herzogstand summit (~1,731 m) is the Föhn/ridge check.
# The three shore stations cover the N→S ignition-fan propagation.
KRUEN = Station("Krün", 47.5772, 11.2622, StationRole.IGNITION_REFERENCE)
HERZOGSTAND = Station("Herzogstand", 47.5839, 11.3081, StationRole.RIDGE)
URFELD = Station("Urfeld", 47.5869, 11.3361, StationRole.SHORE)
GALERIE = Station("Galerie", 47.5783, 11.3225, StationRole.SHORE)
SACHENBACH = Station("Sachenbach", 47.5950, 11.3600, StationRole.SHORE)

# Shore stations in ignition-propagation order (N → S); the thermal typically
# lights up at index 0 first and reaches the last entry ~2 hours later.
SHORE_PROPAGATION: tuple[Station, ...] = (URFELD, GALERIE, SACHENBACH)

# --- Heuristic thresholds -------------------------------------------------
# Mixed provenance: thresholds carrying an inline ``n=…`` note have been fitted
# against the Urfeld calibration log; the rest are still research-informed
# guesses (TODO(calibrate)) awaiting enough ground truth to fit.

MIN_THERMIK_DELTA_HPA = -1.0  # Munich − Innsbruck. Below this the synoptic flow actively
                              # opposes the N-thermal. Set from n=10 calibration: 7/7 logged
                              # GO days (peak ≥12 kt) had Δ ∈ [-0.8, +2.6]; the cross-Alps
                              # delta is a *background* condition for Walchi, not a trigger —
                              # local slope-vs-lake T-gradient is the real driver.
FOEHN_TRIGGER_DELTA_HPA = 4.0    # Bolzano − Innsbruck positive ≥ this => Föhn risk
SYNOPTIC_OVERRIDE_KNOTS = 25.0   # ≥ 3 Bft base wind deforms the thermal cell
                                 # Was 15.0 (research-analogue guess); refitted from
                                 # n=648 ICON-era replay sample (2026-06-12, branch
                                 # threshold-tuning, plan Phase 3, third tune).
                                 # Only 648 of 3,331 days have a non-null
                                 # synoptic_wind_knots (the pre-2021 IFS-HRES
                                 # archive doesn't expose 850 hPa wind). The
                                 # rule fires on 4 days at 15 kt — all 4 are
                                 # fired-anyway (FP), so the rule is
                                 # net-negative at 15. The data-fitted peak
                                 # is at 0 kt (rule always fires, net +22) but
                                 # that's the OPPOSITE of the rule's intent
                                 # (it was meant to be a safety net for
                                 # extreme synoptic days). 25 kt is the clean
                                 # read: only 1 day in the ICON-era sample has
                                 # synoptic >= 25, so the rule is essentially
                                 # a safety net rather than a regular veto.
                                 # The HARD severity stays — when it does
                                 # fire, it's because the synoptic flow is
                                 # strong enough to genuinely override a
                                 # thermal. (Severity is a separate axis; the
                                 # plan's "HARD→SOFT" hint could be a future
                                 # commit if the safety net is over-vetoing.)
IGNITION_WIND_KNOTS = 8.0        # shore reading that signals ignition
MAX_OVERNIGHT_CLOUD_COVER_PCT = 95.0  # 22:00→06:00 average; above this, weak inversion.
                                      # Was 30.0; raised after n=22 calibration — sessions
                                      # fired at up to 94% cloud cover; only the 97.1% day
                                      # was a true NO_GO. Alpine mountain effects dominate
                                      # radiative cooling at this proximity to the ridge.
MIN_MORNING_SOLAR_WM2 = 380.0    # max hourly shortwave radiation 09:00–13:00
                                 # Was 600.0 (research-analogue guess); refitted from
                                 # n=3,263 replay baseline (2026-06-12, branch
                                 # threshold-tuning, plan Phase 3). Sweep on the
                                 # duration-label report: N_C (rule caught a
                                 # didn't-fire day) − N_T (rule wrongly vetoed a
                                 # fired day) peaks at +287 around X=380 W/m²;
                                 # at 600 was +223. 564 days with solar<380 fired
                                 # anyway (the source of the FP-veto
                                 # noise — see 2020-10-03 with peak 30.45 kt and
                                 # solar 481 W/m²) is still larger than at 600
                                 # but the rule's net contribution to the
                                 # model is ~+64 days better.
MIN_DEW_POINT_SPREAD_C = 2.5     # min(T − Td) in morning; below = moisture-suppressed.
                                 # Was 5.0; lowered after n=22 calibration showed full
                                 # sessions at spread 2.8–3.1. The only true NO_GO catch
                                 # sat at spread 2.0 — moisture-cap only bites below ~2.5.
COMFORTABLE_DEW_POINT_SPREAD_C = 8.0  # above this = confidently dry air
MIN_BOUNDARY_LAYER_HEIGHT_M = 600.0   # max BLH in morning; below = capped thermal
GOOD_BOUNDARY_LAYER_HEIGHT_M = 1000.0 # above this = deep mixing, strong thermal potential
WET_SOIL_MOISTURE_M3M3 = 0.35    # soil_moisture_0_to_1cm above this = ground still wet
RAINED_YESTERDAY_MM = 2.0        # threshold for the logged `rained_yesterday` flag.
                                 # No longer drives a veto: n=17 calibration days
                                 # showed 13 FP — post-frontal days fire fine here.
                                 # Kept for the log schema / ML export.
MAX_LIFTED_INDEX = 10.0          # above = atmosphere too stable, thermal capped.
                                 # Was 6.0; raised after n=22 calibration showed full
                                 # sessions at li_max up to 8.9 (and rideable up to 12.3).
                                 # Spring surface heating overpowers a "textbook" cap here.
MIN_LIFTED_INDEX = -2.0          # below = thunderstorm risk, thermal destroyed
MAX_DAYTIME_LOW_CLOUD_PCT = 75.0 # max cloud_cover_low 09:00–13:00; above = slopes shaded
                                 # Was 60.0 (research-analogue guess); refitted from
                                 # n=3,263 replay baseline (2026-06-12, branch
                                 # threshold-tuning, plan Phase 3, second tune).
                                 # Sweep on the duration-label report:
                                 # N_C (rule caught a didn't-fire day) − N_T
                                 # (rule wrongly vetoed a fired day) peaks at
                                 # +139 around X=75%; at 60 was +131. Modest
                                 # +8-day improvement — the cloud distribution
                                 # is bimodal (1,138 days with cloud<10%,
                                 # 744 days with cloud=100%, sparse middle)
                                 # so the rule's contribution in the 30-90%
                                 # borderline band is small either way. The
                                 # raise to 75% is the clean read of the data;
                                 # 66-69% are tied at +138 within noise.
GOOD_DAYTIME_LOW_CLOUD_PCT = 30.0 # below this = unobstructed sun
SYNOPTIC_OPPOSING_DEG = (150, 210)  # 850 hPa wind from SSE counters the N→S thermal
SYNOPTIC_OPPOSING_MIN_KNOTS = 12.0  # SSE direction only vetoes at meaningful 850 speed.
                                    # n=4 calibration days: light SSE drift (2.8–10.3 kt)
                                    # never stopped a session (peaks 10.8–14.0 kt); the
                                    # direction-only veto was 0/4 with no correct catch.
MAX_UPPER_CROSSFLOW_KNOTS = 25.0    # 700 hPa above this decouples valley-wind system

# Lake-temperature rule (air_lake_delta) thresholds.
# TODO(calibrate): no n= yet — docs/future-factors.md sketches air−water > 10 C
# as the working number; lower if the cold-lake regime is over-firing on the
# post-fit rescore-strip.
COLD_LAKE_DELTA_C = 10.0             # air − water > this fires a SOFT NO_GO
MAX_LAKE_TEMP_AGE_HOURS = 168.0      # 7 days; buoy readings older than this
                                     # are "no signal" rather than a fresh veto

# Classic Urfeld ignition window 10:30–11:30; propagation done by ~15:00.
IGNITION_WINDOW_LOCAL: tuple[time, time] = (time(10, 30), time(15, 0))

# First day the oracle logged a forecast. The runs bucket also holds ~3,600
# historical buoy stub records (2016–2026, ground truth only, no verdicts);
# pass this as `since=` to calibrate/rescore/stats walks that should cover
# only the project's own forecast days.
PROJECT_FIRST_DAY = date(2026, 4, 22)

# --- External endpoints ---------------------------------------------------
# Production: live forecast. Replay uses one of the archive hosts below —
# see docs/historical_forecasts.md for model coverage and caveats. The query
# schema is identical across all three; only the host differs.
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
# IFS HRES archive from 2017-01-01, ICON family from 2022-11-24. "First
# hours of each model run" stitched into a continuous hourly timeseries —
# this is the closest faithful replay of "what the oracle would have
# predicted" for a past day, per the doc.
OPEN_METEO_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# Reanalysis (ERA5 / ERA5-Land / IFS analysis). "What really happened",
# not "what was predicted". The Walchi rules are not lead-time sensitive,
# so reanalysis is a fine input for threshold re-fitting.
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/forecast"
BRIGHT_SKY_CURRENT_URL = "https://api.brightsky.dev/current_weather"
ADDICTED_SPORTS_BASE_URL = "https://www.addicted-sports.com"
