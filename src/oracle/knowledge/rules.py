"""Heuristic knowledge base.

Each rule consumes whatever pillar data it needs and returns a `Verdict` with
both a German and an English reason string. The engine combines verdicts into
an overall forecast; the dashboard picks the language per visitor. Rules
encode local experience that global weather models miss — keep them short,
named, and individually testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from oracle import config, storm_classifier
from oracle.pillars.measurements import LakeTempSnapshot, WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureSnapshot


class Signal(str, Enum):
    GO = "go"
    MAYBE = "maybe"
    NO_GO = "no_go"


# Canonical iteration order (best → worst). Used wherever a confusion matrix
# or row layout needs the same ordering as the dashboard's strip rows.
SIGNAL_ORDER: tuple["Signal", ...] = (Signal.GO, Signal.MAYBE, Signal.NO_GO)


class Severity(str, Enum):
    """Veto strength for a NO_GO verdict.

    Only `HARD` vetos can flip the overall forecast to NO_GO. `SOFT` vetos
    (advisory — thermal attenuated but not destroyed) can only downgrade
    GO → MAYBE in the aggregator. `NONE` is the default for GO/MAYBE
    verdicts that have no veto semantics.
    """
    HARD = "hard"
    SOFT = "soft"
    NONE = "none"


@dataclass
class Verdict:
    rule: str
    signal: Signal
    reason_en: str
    reason_de: str
    severity: Severity = Severity.NONE

    @property
    def reason(self) -> str:
        """Default rendering is English (used by the CLI and legacy JSON readers)."""
        return self.reason_en


def thermik(snapshot: PressureSnapshot) -> Verdict:
    delta = snapshot.thermik_delta_hpa
    if delta >= config.MIN_THERMIK_DELTA_HPA:
        return Verdict(
            "thermik", Signal.GO,
            reason_en=f"Δ={delta:.1f} hPa — synoptic flow not opposing N-thermal",
            reason_de=f"Δ={delta:.1f} hPa — Höhenströmung arbeitet nicht gegen N-Thermik",
        )
    return Verdict(
        "thermik", Signal.NO_GO,
        reason_en=f"Δ={delta:.1f} hPa — pressure gradient pushing against the thermal",
        reason_de=f"Δ={delta:.1f} hPa — Druckgradient drückt gegen die Thermik",
        severity=Severity.SOFT,
    )


def foehn_override(snapshot: PressureSnapshot) -> Verdict:
    delta = snapshot.foehn_delta_hpa
    if delta >= config.FOEHN_TRIGGER_DELTA_HPA:
        return Verdict(
            "foehn_override", Signal.NO_GO,
            reason_en=f"Bolzano−Innsbruck Δ={delta:.1f} hPa — Föhn suppressing thermal",
            reason_de=f"Bozen−Innsbruck Δ={delta:.1f} hPa — Föhn unterdrückt die Thermik",
            severity=Severity.HARD,
        )
    return Verdict(
        "foehn_override", Signal.GO,
        reason_en=f"no Föhn pressure signature (Δ={delta:.1f} hPa)",
        reason_de=f"keine Föhn-Signatur (Δ={delta:.1f} hPa)",
    )


def overnight_cooling(meteo: MeteoSnapshot) -> Verdict:
    pct = meteo.overnight_cloud_cover_pct
    if pct <= config.MAX_OVERNIGHT_CLOUD_COVER_PCT:
        return Verdict(
            "overnight_cooling", Signal.GO,
            reason_en=f"{pct:.0f}% overnight cloud cover — cooling sufficient",
            reason_de=f"nachts {pct:.0f}% Bewölkung — Abkühlung ausreichend",
        )
    return Verdict(
        "overnight_cooling", Signal.NO_GO,
        reason_en=f"{pct:.0f}% overnight cloud cover — weak inversion",
        reason_de=f"nachts {pct:.0f}% Bewölkung — schwache Inversion",
        severity=Severity.SOFT,
    )


def solar_radiation(meteo: MeteoSnapshot) -> Verdict:
    wm2 = meteo.morning_solar_radiation_wm2
    if wm2 >= config.MIN_MORNING_SOLAR_WM2:
        return Verdict(
            "solar_radiation", Signal.GO,
            reason_en=f"peak solar radiation {wm2:.0f} W/m² ≥ threshold",
            reason_de=f"Strahlung {wm2:.0f} W/m² ≥ Schwellwert",
        )
    return Verdict(
        "solar_radiation", Signal.NO_GO,
        reason_en=f"peak solar radiation {wm2:.0f} W/m² below {config.MIN_MORNING_SOLAR_WM2:.0f}",
        reason_de=f"Strahlung {wm2:.0f} W/m² unter {config.MIN_MORNING_SOLAR_WM2:.0f}",
        severity=Severity.SOFT,
    )


def dew_point_spread(meteo: MeteoSnapshot) -> Verdict:
    s = meteo.min_dew_point_spread_c
    if s < config.MIN_DEW_POINT_SPREAD_C:
        return Verdict(
            "dew_point_spread", Signal.NO_GO,
            reason_en=f"dew-point spread {s:.1f}°C — air too moist, solar energy lost to evaporation",
            reason_de=f"Taupunkt-Abstand {s:.1f}°C — Luft zu feucht, Sonnenenergie geht in Verdunstung",
            severity=Severity.SOFT,
        )
    if s < config.COMFORTABLE_DEW_POINT_SPREAD_C:
        return Verdict(
            "dew_point_spread", Signal.MAYBE,
            reason_en=f"dew-point spread {s:.1f}°C — marginal",
            reason_de=f"Taupunkt-Abstand {s:.1f}°C — grenzwertig",
        )
    return Verdict(
        "dew_point_spread", Signal.GO,
        reason_en=f"dew-point spread {s:.1f}°C — dry air",
        reason_de=f"Taupunkt-Abstand {s:.1f}°C — trockene Luft",
    )


def boundary_layer_height(meteo: MeteoSnapshot) -> Verdict:
    h = meteo.max_boundary_layer_height_m
    if h is None:
        # Replay against the historical-forecast API: IFS HRES doesn't
        # model BLH, so the field comes back as None. Treat as "no
        # signal" rather than fabricating a number.
        return Verdict(
            "boundary_layer_height", Signal.MAYBE,
            reason_en="boundary layer height unavailable — no signal",
            reason_de="Grenzschichthöhe nicht verfügbar — kein Signal",
        )
    if h < config.MIN_BOUNDARY_LAYER_HEIGHT_M:
        return Verdict(
            "boundary_layer_height", Signal.NO_GO,
            reason_en=f"boundary layer capped at {h:.0f} m — thermal can't develop depth",
            reason_de=f"Grenzschicht bei {h:.0f} m gedeckelt — Thermik bleibt flach",
            severity=Severity.SOFT,
        )
    if h < config.GOOD_BOUNDARY_LAYER_HEIGHT_M:
        return Verdict(
            "boundary_layer_height", Signal.MAYBE,
            reason_en=f"boundary layer {h:.0f} m — shallow mixing",
            reason_de=f"Grenzschicht {h:.0f} m — flache Durchmischung",
        )
    return Verdict(
        "boundary_layer_height", Signal.GO,
        reason_en=f"boundary layer {h:.0f} m — deep mixing",
        reason_de=f"Grenzschicht {h:.0f} m — tiefe Durchmischung",
    )


def post_rain_moisture(meteo: MeteoSnapshot) -> Verdict:
    # Soil moisture only — rained_yesterday was dropped as a veto (n=17,
    # 13 FP: post-frontal days at Walchensee fire fine once the sun is out;
    # genuinely washed-out days are caught by soil/cloud/solar instead).
    sm = meteo.soil_moisture_m3m3
    if sm is None:
        # Replay against the historical-forecast API: IFS HRES doesn't
        # model surface soil moisture. Treat as "no signal" — the rule
        # has no way to know whether the ground is wet, and the
        # aggregator will accumulate SOFT NO_GOs from other rules if
        # there are real concerns.
        return Verdict(
            "post_rain_moisture", Signal.MAYBE,
            reason_en="soil moisture unavailable — no signal",
            reason_de="Bodenfeuchte nicht verfügbar — kein Signal",
        )
    if sm > config.WET_SOIL_MOISTURE_M3M3:
        return Verdict(
            "post_rain_moisture", Signal.NO_GO,
            reason_en=f"soil moisture {sm:.2f} m³/m³ — ground still wet",
            reason_de=f"Bodenfeuchte {sm:.2f} m³/m³ — Boden noch zu nass",
            severity=Severity.SOFT,
        )
    return Verdict(
        "post_rain_moisture", Signal.GO,
        reason_en=f"dry ground (soil moisture {sm:.2f} m³/m³)",
        reason_de=f"trockener Boden (Bodenfeuchte {sm:.2f} m³/m³)",
    )


def is_storm_risk(min_lifted_index: float) -> bool:
    """True when convective instability is high enough to flag thunderstorm risk.

    Single source of truth for the thunderstorm **advisory** — the dashboard's
    Caution box and its yellow storm border. As of the LI-decouple experiment it
    no longer drives a verdict veto: a storm day is usually a strong thermal day
    right up until the gust front arrives, so the thermal is scored on its merits
    and the storm shows as a separate safety overlay (see `atmospheric_stability`
    below). Keyed on the lifted index (≤ MIN_LIFTED_INDEX) — the project's
    existing thunderstorm signal. CAPE and target-day precipitation are captured
    but not yet folded in (see config.py); tighten here when calibrated, and the
    advisory's consumers move together.
    """
    return min_lifted_index <= config.MIN_LIFTED_INDEX


def atmospheric_stability(meteo: MeteoSnapshot) -> Verdict:
    lo, hi = meteo.min_lifted_index, meteo.max_lifted_index
    if lo is None or hi is None:
        # Replay against historical-forecast API: IFS HRES doesn't expose
        # lifted_index for older years. Without LI we can't see if the
        # atmosphere is too stable (high LI) or storm-prone (low LI).
        return Verdict(
            "atmospheric_stability", Signal.MAYBE,
            reason_en="lifted index unavailable — no signal",
            reason_de="Lifted Index nicht verfügbar — kein Signal",
        )
    if hi >= config.MAX_LIFTED_INDEX:
        return Verdict(
            "atmospheric_stability", Signal.NO_GO,
            reason_en=f"LI {hi:.1f} — atmosphere too stable, thermal capped",
            reason_de=f"LI {hi:.1f} — Atmosphäre zu stabil, Thermik gedeckelt",
            severity=Severity.SOFT,
        )
    if storm_classifier.storm_advisory_from_snapshot(meteo):
        # Thunderstorm risk no longer vetoes the thermal verdict. The day-ahead
        # ground truth (n=68 storm days in the replay corpus) shows the thermal
        # still fired on 45 GO / 21 MAYBE / 2 NO_GO — a storm day is a strong
        # thermal day until the gust front arrives. The danger is surfaced as a
        # separate safety advisory (Caution box + storm border) driven by the
        # calibrated storm classifier (afternoon convective features, falling
        # back to LI ≤ −2); here the thermal is scored on its merits, so this
        # rule stays GREEN and lets the other pillars speak.
        return Verdict(
            "atmospheric_stability", Signal.GO,
            reason_en=f"LI {lo:.1f} — convective: thunderstorm risk flagged separately, thermal not vetoed",
            reason_de=f"LI {lo:.1f} — labil: Gewittergefahr separat ausgewiesen, Thermik nicht abgewertet",
        )
    return Verdict(
        "atmospheric_stability", Signal.GO,
        reason_en=f"LI {lo:.1f}…{hi:.1f} — stability in normal range",
        reason_de=f"LI {lo:.1f}…{hi:.1f} — Stabilität im Normbereich",
    )


def daytime_clouds(meteo: MeteoSnapshot) -> Verdict:
    pct = meteo.max_daytime_low_cloud_pct
    if pct > config.MAX_DAYTIME_LOW_CLOUD_PCT:
        return Verdict(
            "daytime_clouds", Signal.NO_GO,
            reason_en=f"{pct:.0f}% low cloud during the day — slopes shaded",
            reason_de=f"{pct:.0f}% tiefe Bewölkung tagsüber — beschattet die Hänge",
            severity=Severity.SOFT,
        )
    if pct < config.GOOD_DAYTIME_LOW_CLOUD_PCT:
        return Verdict(
            "daytime_clouds", Signal.GO,
            reason_en=f"{pct:.0f}% low cloud — slopes in full sun",
            reason_de=f"{pct:.0f}% tiefe Bewölkung — Hänge bekommen Sonne",
        )
    return Verdict(
        "daytime_clouds", Signal.MAYBE,
        reason_en=f"{pct:.0f}% low cloud — borderline",
        reason_de=f"{pct:.0f}% tiefe Bewölkung — grenzwertig",
    )


def no_insolation(meteo: MeteoSnapshot) -> Verdict:
    """Heavy daytime cloud *and* low morning solar together → no thermal.

    A thermal is solar-driven; with the slopes shaded all day (high low-cloud)
    *and* little morning insolation reaching the ground, there is no surface
    heating to build one. Either signal alone is only a SOFT hint (`daytime_clouds`,
    `solar_radiation`); their *combination* is decisive, so this is a HARD veto.
    It is the structural source of NO_GO the soft-veto aggregator otherwise
    lacks — without it the rules over-forecast GO on cloudy days. Thresholds are
    holdout-fit; see docs/findings/structural-insolation-veto.md.
    """
    cloud = meteo.max_daytime_low_cloud_pct
    solar = meteo.morning_solar_radiation_wm2
    if cloud >= config.NO_INSOLATION_CLOUD_PCT and solar <= config.NO_INSOLATION_SOLAR_WM2:
        return Verdict(
            "no_insolation", Signal.NO_GO,
            reason_en=f"{cloud:.0f}% daytime cloud + only {solar:.0f} W/m² solar — no insolation to drive a thermal",
            reason_de=f"{cloud:.0f}% Bewölkung tagsüber + nur {solar:.0f} W/m² Strahlung — keine Einstrahlung für Thermik",
            severity=Severity.HARD,
        )
    return Verdict(
        "no_insolation", Signal.GO,
        reason_en=f"{cloud:.0f}% daytime cloud / {solar:.0f} W/m² solar — enough insolation for a thermal",
        reason_de=f"{cloud:.0f}% Bewölkung / {solar:.0f} W/m² Strahlung — ausreichend Einstrahlung für Thermik",
    )


def upper_level_wind(meteo: MeteoSnapshot) -> Verdict:
    direction = meteo.wind_850_direction_at_peak_deg
    speed_850 = meteo.synoptic_wind_knots
    crossflow = meteo.max_wind_700_knots
    if direction is None or speed_850 is None or crossflow is None:
        # Replay against historical-forecast API: IFS HRES doesn't expose
        # pressure-level vars for the pre-2021 era. Without the 850/700 hPa
        # data we can't tell if upper flow opposes the thermal.
        return Verdict(
            "upper_level_wind", Signal.MAYBE,
            reason_en="upper-level wind data unavailable — no signal",
            reason_de="Höhenwind-Daten nicht verfügbar — kein Signal",
        )
    lo, hi = config.SYNOPTIC_OPPOSING_DEG
    if lo <= direction <= hi and speed_850 >= config.SYNOPTIC_OPPOSING_MIN_KNOTS:
        return Verdict(
            "upper_level_wind", Signal.NO_GO,
            reason_en=f"850 hPa {speed_850:.0f} kt from {direction:.0f}° (SSE) — counters the N-thermal",
            reason_de=f"850 hPa {speed_850:.0f} kt aus {direction:.0f}° (SSE) — Gegenströmung zur N-Thermik",
            severity=Severity.HARD,
        )
    if crossflow > config.MAX_UPPER_CROSSFLOW_KNOTS:
        return Verdict(
            "upper_level_wind", Signal.NO_GO,
            reason_en=f"700 hPa crossflow {crossflow:.0f} kt — valley decoupled",
            reason_de=f"700 hPa Querströmung {crossflow:.0f} kt — Tal entkoppelt",
            severity=Severity.HARD,
        )
    return Verdict(
        "upper_level_wind", Signal.GO,
        reason_en=f"upper wind from {direction:.0f}° @ 700 hPa {crossflow:.0f} kt — neutral",
        reason_de=f"Höhenwind aus {direction:.0f}° @ 700 hPa {crossflow:.0f} kt — neutral",
    )


def synoptic_override(meteo: MeteoSnapshot) -> Verdict:
    speed = meteo.synoptic_wind_knots
    if speed is None:
        # Replay against historical-forecast API for pre-2021 days: 850 hPa
        # wind isn't in the archive. Without it we can't rule out a
        # synoptic override; let the upper_level_wind rule carry the
        # NO_GO if it has data, otherwise treat as no signal.
        return Verdict(
            "synoptic_override", Signal.MAYBE,
            reason_en="synoptic wind data unavailable — no signal",
            reason_de="Höhenwind-Daten nicht verfügbar — kein Signal",
        )
    if speed >= config.SYNOPTIC_OVERRIDE_KNOTS:
        return Verdict(
            "synoptic_override", Signal.NO_GO,
            reason_en=f"synoptic wind {speed:.0f} kt will destroy the thermal cell",
            reason_de=f"Höhenwind {speed:.0f} kt zerstört die Thermikzelle",
            severity=Severity.HARD,
        )
    return Verdict(
        "synoptic_override", Signal.GO,
        reason_en="no overwhelming synoptic flow",
        reason_de="keine dominierende Höhenströmung",
    )


def thermal_ignition(readings: list[WindReading]) -> Verdict:
    ignited = [r for r in readings if r.avg_knots >= config.IGNITION_WIND_KNOTS]
    if ignited:
        names = ", ".join(r.station for r in ignited)
        return Verdict(
            "thermal_ignition", Signal.GO,
            reason_en=f"ignited at {names}",
            reason_de=f"gezündet an {names}",
        )
    return Verdict(
        "thermal_ignition", Signal.MAYBE,
        reason_en="no station has ignited yet",
        reason_de="noch keine Station gezündet",
    )


def air_lake_delta(
    lake_temp: LakeTempSnapshot | None,
    meteo: MeteoSnapshot,
    *,
    now: datetime | None = None,
) -> Verdict:
    """Air minus water temperature. NOTE: the rule is effectively disabled
    (COLD_LAKE_DELTA_C = 999) because its original premise was *contradicted* by
    the n=3,314 replay data — see config. The physical story below is the
    hypothesis that didn't hold; kept for context pending a structural revisit.

    Hypothesised premise (not borne out): Walchensee is 192 m deep — surface
    temperature lags air by weeks. In spring the lake sits 6–10 °C below the
    warming air, creating a cold-surface dome that was expected to oppose the
    thermal. In the data, warm-lake days actually fired *more*, so the rule was
    disabled rather than shipped with an inverted sign.

    Sign convention: `air − water` is positive when the lake is colder than the
    air. Above `COLD_LAKE_DELTA_C` we fire a SOFT NO_GO; below `-COLD_LAKE_DELTA_C`
    (warm lake) we boost with a plain GO. In between is a neutral band — the
    lake neither helps nor hurts.

    Tolerant on missing data: if the buoy is down (`lake_temp is None`) or its
    reading is older than `MAX_LAKE_TEMP_AGE_HOURS`, we return MAYBE rather than
    guessing. The forecast air-temp field is None for records written before
    this rule shipped; same treatment.
    """
    now = now or datetime.now()

    if lake_temp is None or lake_temp.surface_temp_c is None:
        return Verdict(
            "air_lake_delta", Signal.MAYBE,
            reason_en="no lake-temp reading from the buoy",
            reason_de="keine Wassertemperatur von der Boje",
        )
    if lake_temp.measured_at is None:
        return Verdict(
            "air_lake_delta", Signal.MAYBE,
            reason_en="lake-temp reading missing a timestamp",
            reason_de="Wassertemperatur ohne Zeitstempel",
        )
    age_h = (now - lake_temp.measured_at).total_seconds() / 3600.0
    if age_h > config.MAX_LAKE_TEMP_AGE_HOURS:
        return Verdict(
            "air_lake_delta", Signal.MAYBE,
            reason_en=f"buoy water-temp reading is {age_h:.0f} h old",
            reason_de=f"Bojen-Wassertemperatur ist {age_h:.0f} h alt",
        )
    if meteo.morning_air_temp_c is None:
        return Verdict(
            "air_lake_delta", Signal.MAYBE,
            reason_en="no forecast air-temp reading",
            reason_de="keine Lufttemperatur-Prognose",
        )

    delta = meteo.morning_air_temp_c - lake_temp.surface_temp_c
    if delta > config.COLD_LAKE_DELTA_C:
        return Verdict(
            "air_lake_delta", Signal.NO_GO,
            reason_en=f"air−water Δ={delta:+.1f}°C — cold lake opposes the thermal",
            reason_de=f"Luft−Wasser Δ={delta:+.1f}°C — kalter See bremst die Thermik",
            severity=Severity.SOFT,
        )
    if delta < -config.COLD_LAKE_DELTA_C:
        return Verdict(
            "air_lake_delta", Signal.GO,
            reason_en=f"air−water Δ={delta:+.1f}°C — warm lake aids the thermal",
            reason_de=f"Luft−Wasser Δ={delta:+.1f}°C — warmer See unterstützt die Thermik",
        )
    return Verdict(
        "air_lake_delta", Signal.GO,
        reason_en=f"air−water Δ={delta:+.1f}°C — lake-air delta in neutral band",
        reason_de=f"Luft−Wasser Δ={delta:+.1f}°C — See-Luft im neutralen Bereich",
    )
