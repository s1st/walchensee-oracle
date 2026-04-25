"""Heuristic knowledge base.

Each rule consumes whatever pillar data it needs and returns a `Verdict` with
both a German and an English reason string. The engine combines verdicts into
an overall forecast; the dashboard picks the language per visitor. Rules
encode local experience that global weather models miss — keep them short,
named, and individually testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from oracle import config
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureSnapshot


class Signal(str, Enum):
    GO = "go"
    MAYBE = "maybe"
    NO_GO = "no_go"


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
            reason_en=f"Δ={delta:.1f} hPa ≥ threshold",
            reason_de=f"Δ={delta:.1f} hPa ≥ Schwellwert",
        )
    return Verdict(
        "thermik", Signal.NO_GO,
        reason_en=f"Δ={delta:.1f} hPa below threshold",
        reason_de=f"Δ={delta:.1f} hPa unter Schwellwert",
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
            reason_en=f"clear night: {pct:.0f}% cloud cover",
            reason_de=f"klare Nacht: {pct:.0f}% Bewölkung",
        )
    return Verdict(
        "overnight_cooling", Signal.NO_GO,
        reason_en=f"{pct:.0f}% overnight cloud cover — weak inversion",
        reason_de=f"{pct:.0f}% Bewölkung nachts — schwache Inversion",
        severity=Severity.SOFT,
    )


def solar_radiation(meteo: MeteoSnapshot) -> Verdict:
    wm2 = meteo.morning_solar_radiation_wm2
    if wm2 >= config.MIN_MORNING_SOLAR_WM2:
        return Verdict(
            "solar_radiation", Signal.GO,
            reason_en=f"peak radiation {wm2:.0f} W/m² ≥ threshold",
            reason_de=f"Strahlung {wm2:.0f} W/m² ≥ Schwellwert",
        )
    return Verdict(
        "solar_radiation", Signal.NO_GO,
        reason_en=f"peak radiation {wm2:.0f} W/m² below {config.MIN_MORNING_SOLAR_WM2:.0f}",
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
    if meteo.rained_yesterday:
        mm = meteo.yesterday_precipitation_mm
        return Verdict(
            "post_rain_moisture", Signal.NO_GO,
            reason_en=f"{mm:.1f} mm rain yesterday — solar energy lost to evaporation",
            reason_de=f"gestern {mm:.1f} mm Regen — Sonnenenergie geht in Verdunstung",
            severity=Severity.SOFT,
        )
    if meteo.soil_moisture_m3m3 > config.WET_SOIL_MOISTURE_M3M3:
        sm = meteo.soil_moisture_m3m3
        return Verdict(
            "post_rain_moisture", Signal.NO_GO,
            reason_en=f"soil moisture {sm:.2f} m³/m³ — ground still wet",
            reason_de=f"Bodenfeuchte {sm:.2f} m³/m³ — Boden noch zu nass",
            severity=Severity.SOFT,
        )
    sm = meteo.soil_moisture_m3m3
    return Verdict(
        "post_rain_moisture", Signal.GO,
        reason_en=f"dry ground (soil moisture {sm:.2f} m³/m³)",
        reason_de=f"trockener Boden (Bodenfeuchte {sm:.2f} m³/m³)",
    )


def atmospheric_stability(meteo: MeteoSnapshot) -> Verdict:
    if meteo.max_lifted_index >= config.MAX_LIFTED_INDEX:
        li = meteo.max_lifted_index
        return Verdict(
            "atmospheric_stability", Signal.NO_GO,
            reason_en=f"LI {li:.1f} — atmosphere too stable, thermal capped",
            reason_de=f"LI {li:.1f} — Atmosphäre zu stabil, Thermik gedeckelt",
            severity=Severity.SOFT,
        )
    if meteo.min_lifted_index <= config.MIN_LIFTED_INDEX:
        li = meteo.min_lifted_index
        return Verdict(
            "atmospheric_stability", Signal.NO_GO,
            reason_en=f"LI {li:.1f} — thunderstorm risk destroys the thermal",
            reason_de=f"LI {li:.1f} — Gewittergefahr zerstört die Thermik",
            severity=Severity.HARD,
        )
    lo, hi = meteo.min_lifted_index, meteo.max_lifted_index
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


def upper_level_wind(meteo: MeteoSnapshot) -> Verdict:
    direction = meteo.wind_850_direction_at_peak_deg
    crossflow = meteo.max_wind_700_knots
    lo, hi = config.SYNOPTIC_OPPOSING_DEG
    if lo <= direction <= hi:
        return Verdict(
            "upper_level_wind", Signal.NO_GO,
            reason_en=f"850 hPa from {direction:.0f}° (SSE) — counters the N-thermal",
            reason_de=f"850 hPa aus {direction:.0f}° (SSE) — Gegenströmung zur N-Thermik",
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
    if speed >= config.SYNOPTIC_OVERRIDE_KNOTS:
        return Verdict(
            "synoptic_override", Signal.NO_GO,
            reason_en=f"synoptic wind {speed:.0f} kt will destroy the thermal cell",
            reason_de=f"Synoptikwind {speed:.0f} kt zerstört die Thermikzelle",
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
