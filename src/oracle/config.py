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
FOEHN_TRIGGER_DELTA_HPA = 10.0   # Bolzano − Innsbruck positive ≥ this => Föhn risk
                                 # Was 4.0 (research-analogue guess); refitted from
                                 # n=3,331 replay baseline (2026-06-12, branch
                                 # threshold-tuning, plan Phase 3, fifth tune).
                                 # The rule's premise — "Föhn suppresses
                                 # thermals" — is *contradicted* by the data.
                                 # Fire rate by Bolzano−Innsbruck Δ bucket:
                                 #   Δ 0-2:    52% fire   (no Föhn)
                                 #   Δ 2-4:    54% fire   (weak Föhn)
                                 #   Δ 4-6:    64% fire   (Föhn — current trigger)
                                 #   Δ 6-8:   100% fire   (strong Föhn)
                                 #   Δ 8-10:   67% fire   (very strong Föhn)
                                 # Föhn days fire *more* often than non-Föhn
                                 # days, not less. The rule is net-negative at
                                 # every threshold from -5 to +9 hPa (N_C − N_T
                                 # ranges from -1 to -104; the best is -1 at
                                 # X=-5 and 0 at X≥10). At the current 4 the
                                 # rule fires on 136 days: 41 correct vetoes,
                                 # 95 wrong vetoes. Raising to 10 essentially
                                 # disables the rule (no day in the sample has
                                 # Δ ≥ 10). The "right" fix would be to flip
                                 # the sign or to remove the rule entirely —
                                 # both are bigger changes than a threshold
                                 # tweak. 10 is the clean "safety net"
                                 # value; a follow-up commit should consider
                                 # replacing this rule with a feature input
                                 # to the thermik or foehn-aware boundary
                                 # logic.
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
MAX_OVERNIGHT_CLOUD_COVER_PCT = 100.0  # 22:00→06:00 average; above this, weak inversion.
                                      # Was 30.0 → 95.0 (n=22) → 100.0 (effectively
                                      # disabled; n=1912 replay, 2026-06-14, ML-distill
                                      # Cut 3). At 95 the overnight_cooling SOFT veto fired
                                      # 478× on the replay, 424 of them false-positive (a
                                      # real GO/MAYBE session). Removing the veto improves
                                      # Peirce (+0.063→+0.072), mean cost (0.535→0.517) AND
                                      # accuracy (44.0→45.1%) together — a clean Pareto win.
                                      # The discriminative content of overnight cloud is in
                                      # the 50–71% mid-range (thermal mean 52 vs no-go 71),
                                      # not the >95% tail the veto fired on; and as a SOFT
                                      # veto it only ever mattered as the 2nd veto tipping a
                                      # day down, so removal (not re-tuning) is the lever.
                                      # 100 keeps the rule wired/visible but it can never
                                      # fire (cloud ≤ 100 always) — same disable idiom as
                                      # FOEHN_TRIGGER_DELTA_HPA / SYNOPTIC_OVERRIDE_KNOTS.
                                      # See docs/findings/ml-distill-cut3-2026-06-14.md.
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
MIN_BOUNDARY_LAYER_HEIGHT_M = 400.0   # max BLH in morning; below = capped thermal
                                 # Was 600.0 (research-analogue guess); refitted
                                 # from n=629 ICON-era replay sample (2026-06-12,
                                 # branch threshold-tuning, plan Phase 3, sixth
                                 # tune). The pre-2021 IFS-HRES archive doesn't
                                 # expose BLH so the tune is ICON-era only —
                                 # same caveat as the synoptic / foehn tunes.
                                 # Sweep on the duration-label report: N_C
                                 # (rule caught a didn't-fire day) − N_T (rule
                                 # wrongly vetoed a fired day) peaks at +57
                                 # around X=400m; at the current 600 was
                                 # +53. Modest +4-day improvement — the rule's
                                 # net value plateaus at +50 to +60 across the
                                 # 200-3000m range; the data has ~145 days with
                                 # BLH<300m and that's where most of the
                                 # discriminating signal is. The "absolute"
                                 # optimum is at X=3000m (rule fires on
                                 # essentially all days, +63) but that's
                                 # "rule does nothing different from baseline"
                                 # and loses the BLH-specific veto semantics.
                                 # 400 keeps the intent: catch clearly-shallow
                                 # mornings as a thermal cap.
GOOD_BOUNDARY_LAYER_HEIGHT_M = 1000.0 # above this = deep mixing, strong thermal potential
WET_SOIL_MOISTURE_M3M3 = 0.30    # soil_moisture_0_to_1cm above this = ground still wet
                                 # Was 0.35 (research-analogue guess); refitted
                                 # from n=48 ICON-era replay sample (2026-06-12,
                                 # branch threshold-tuning, plan Phase 3, seventh
                                 # tune). SMALL SAMPLE CAVEAT: only 48 ICON-era
                                 # days have non-null soil_moisture_m3m3, all from
                                 # 2022-11-24 onward (the DWD ICON launch window).
                                 # The Open-Meteo archive response for this
                                 # variable is inconsistent across years; the
                                 # data we have is ~5-6 weeks of late-2022 soil
                                 # moisture. Use this tune with caution.
                                 # Sweep on the duration-label report: N_C − N_T
                                 # peaks at +28 around X=0.10-0.25 (the rule
                                 # fires on every day with non-null soil moisture
                                 # in the sample), plateauing through X=0.30
                                 # (+27, fires on 43 of 48 days). At the current
                                 # 0.35 the rule never fires (the maximum
                                 # observed value is 0.339) — it's a no-op.
                                 # Fire rate by soil moisture bucket:
                                 #   0.20-0.30 m³/m³    4 days    50% fire
                                 #   0.30-0.40 m³/m³   44 days    18% fire
                                 # The 0.30 threshold catches the "wet band" only
                                 # and avoids the borderline 0.20-0.30 days
                                 # where the fire rate is 50% (no signal).
RAINED_YESTERDAY_MM = 2.0        # threshold for the logged `rained_yesterday` flag.
                                 # No longer drives a veto: n=17 calibration days
                                 # showed 13 FP — post-frontal days fire fine here.
                                 # Kept for the log schema / ML export.
MAX_LIFTED_INDEX = 10.0          # above = atmosphere too stable, thermal capped.
                                 # Was 6.0; raised after n=22 calibration showed full
                                 # sessions at li_max up to 8.9 (and rideable up to 12.3).
                                 # Spring surface heating overpowers a "textbook" cap here.
MIN_LIFTED_INDEX = -2.0          # below = thunderstorm risk. No longer a verdict
                                 # veto (LI-decouple experiment): the thermal is
                                 # scored on its merits and the storm shows as a
                                 # separate Caution advisory. This is now the
                                 # advisory's trigger threshold (is_storm_risk).
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

# Combined-insolation HARD veto. Heavy daytime cloud AND low morning solar
# together mean no surface heating → no thermal. Decisive as a *combination*
# even though either signal alone is only a SOFT hint (and the prior pass had
# loosened both). This is the structural fix that supplies the HARD NO_GO the
# aggregator lacked: on the thermal label it lifts held-out Peirce −0.012 →
# +0.066 (McNemar p=6e-8), the only large, significant, era-stable gain found in
# either tuning pass. Thresholds fit on a temporal holdout (train ≤2022 /
# test ≥2023) by minimising cost; cost-positive on both splits.
# See docs/findings/structural-insolation-veto.md.
NO_INSOLATION_CLOUD_PCT = 70.0    # daytime low-cloud % AND ↓ ; n=1912, holdout-validated
NO_INSOLATION_SOLAR_WM2 = 400.0   # morning solar W/m² ; both must hold to fire the veto
SYNOPTIC_OPPOSING_DEG = (150, 210)  # 850 hPa wind from SSE counters the N→S thermal
SYNOPTIC_OPPOSING_MIN_KNOTS = 12.0  # SSE direction only vetoes at meaningful 850 speed.
                                    # n=4 calibration days: light SSE drift (2.8–10.3 kt)
                                    # never stopped a session (peaks 10.8–14.0 kt); the
                                    # direction-only veto was 0/4 with no correct catch.
MAX_UPPER_CROSSFLOW_KNOTS = 25.0    # 700 hPa above this decouples valley-wind system
                                 # (Reverted: data-fit sweep suggested 15 kt was
                                 # better at the rule level — N_C − N_T = +29
                                 # there vs +3 at 25 — but the verdict-level
                                 # aggregator shifted −1pp on the full 3,263
                                 # day sample when the rule fired on 160 more
                                 # days. Each new fire is a SOFT veto that can
                                 # push a go-day down to maybe/no_go via the
                                 # 2-soft-veto downgrade bar. The rule-level
                                 # N_C − N_T analysis didn't capture that
                                 # interaction. 25 is "barely-active safety
                                 # net" — the clean read is that the rule's
                                 # value is dominated by the aggregator, not
                                 # by the simple veto accuracy. Parked: an
                                 # aggregator-aware tuner (or changing the
                                 # rule's severity to NONE so it doesn't
                                 # shift verdicts at all) is the right fix
                                 # if this rule is ever to earn more.)
                                 # Was originally 25.0 (research-analogue
                                 # guess, plan flagged for re-fit). See
                                 # docs/findings/threshold-upper-level-wind.md
                                 # for the full data and the alternative
                                 # that did worse on the aggregator.

# Lake-temperature rule (air_lake_delta) thresholds. Refitted n=3,314 (see the
# inline note below) — the 999.0 sentinel disables the rule because its premise
# is contradicted by the data, not because it is uncalibrated.
COLD_LAKE_DELTA_C = 999.0            # air − water > this fires a SOFT NO_GO
                                 # air − water < -this fires a plain GO
                                 # (warm lake helps the thermal, per the rule's
                                 # physical premise)
                                 # Was 10.0 (research-analogue guess); refitted
                                 # from n=3,314 replay sample (2026-06-12, branch
                                 # threshold-tuning, plan Phase 3, eighth tune).
                                 # The rule's premise — 'cold lake opposes
                                 # thermals, warm lake helps' — is *inverted*
                                 # in the data:
                                 #   delta -15 to -10 C    86 days  38% fire
                                 #   delta -10 to  -5     487 days  39% fire
                                 #   delta  -5 to  -2     712 days  43% fire
                                 #   delta  -2 to  +0     543 days  49% fire
                                 #   delta  +0 to  +2     486 days  54% fire
                                 #   delta  +2 to  +5     618 days  57% fire
                                 #   delta  +5 to  +8     265 days  63% fire  ← peak
                                 #   delta  +8 to +10      73 days  52% fire
                                 #   delta +10 to +12      25 days  56% fire
                                 #   delta +12 to +15       7 days  43% fire
                                 # Fire rate INCREASES with delta — warm-lake
                                 # days fire MORE, not less. The rule's both
                                 # directions are wrong: it says GO on
                                 # delta<-10 (where fire rate is 38%) and
                                 # NO_GO on delta>+10 (where fire rate is 56%).
                                 # The data is the opposite of the premise.
                                 # Sweep on the duration-label report: the
                                 # NO_GO trigger is net-negative at every
                                 # threshold from 0 to 10 C (N_C − N_T ranges
                                 # from -205 to -2). The "best" threshold is
                                 # 12 C (net +1) — at 14 C no day fires. The
                                 # GO trigger (delta < -X) is also net-negative
                                 # at every threshold (N_C − N_T ranges from
                                 # -232 to -10). Setting COLD_LAKE_DELTA_C
                                 # to 999 effectively disables the rule (no
                                 # day in the sample has delta > 999) while
                                 # keeping the safety-net appearance. Same
                                 # pattern as the foehn rule: the data says
                                 # the rule's premise is wrong, the cleanest
                                 # no-op move is to disable it. A future
                                 # structural commit should consider removing
                                 # the rule or flipping its sign.
MAX_LAKE_TEMP_AGE_HOURS = 168.0      # 7 days; buoy readings older than this
                                     # are "no signal" rather than a fresh veto

# Aggregator: how many SOFT vetos are needed to downgrade a GO verdict to MAYBE.
# Reverted 5 → 2 (the project's pre-replay default) on 2026-06-13. The 2→5
# change was fit on the *contaminated* peak-label accuracy metric (+2.9pp); under
# the corrected thermal label it lands at near-zero skill (Peirce +0.006) and the
# bar5→bar2 difference is within noise (McNemar p=0.20) and era-unstable (IFS
# wants 2, ICON 1). bar=2 restores meaningful MAYBE hedging (≈920 vs 56 days).
# The soft bar only moves GO↔MAYBE; NO_GO now comes from the no_insolation HARD
# veto. See docs/findings/aggregator-bar-recalibrated.md + structural-insolation-veto.md.
SOFT_VETO_BAR = 2                 # SOFT vetos required to downgrade → MAYBE

# Classic Urfeld ignition window 10:30–11:30; propagation done by ~15:00.
IGNITION_WINDOW_LOCAL: tuple[time, time] = (time(10, 30), time(15, 0))

# First day the oracle logged a forecast. The runs bucket also holds ~3,600
# historical buoy stub records (2016–2026, ground truth only, no verdicts);
# pass this as `since=` to calibrate/rescore/stats walks that should cover
# only the project's own forecast days.
PROJECT_FIRST_DAY = date(2026, 4, 22)

# The product only serves the thermal season — the project shuts down Nov–Mar
# (no GCP cost, no samples accrue off-season). Calibration must score on the
# same window it serves: scoring year-round lets winter (which dominates the
# negative class) turn univariate thresholds into "is it winter?" detectors.
# Apr–Oct inclusive. See docs/fable_findings.md §2.
ACTIVE_SEASON_MONTHS: frozenset[int] = frozenset({4, 5, 6, 7, 8, 9, 10})

# Open-Meteo Best Match flips the underlying NWP model here: IFS HRES (9 km)
# before this date, DWD ICON-D2 (2.2 km) from it. Solar/cloud/BLH distributions
# differ across the two, so replay-fitted thresholds must be checked per era —
# a single corpus-wide optimum can hide an IFS/ICON split (Fable review §6).
ICON_ERA_START = date(2022, 11, 24)

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
