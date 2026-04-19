"""Heuristic knowledge base.

Each rule consumes whatever pillar data it needs and returns a `Verdict`. The
engine combines verdicts into an overall forecast. Rules encode local experience
that global weather models miss — keep them short, named, and individually
testable.
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


@dataclass
class Verdict:
    rule: str
    signal: Signal
    reason: str


def alpenpumpe_threshold(snapshot: PressureSnapshot) -> Verdict:
    delta = snapshot.alpenpumpe_delta_hpa
    if delta >= config.MIN_ALPENPUMPE_DELTA_HPA:
        return Verdict("alpenpumpe_threshold", Signal.GO, f"Δ={delta:.1f} hPa ≥ threshold")
    return Verdict("alpenpumpe_threshold", Signal.NO_GO, f"Δ={delta:.1f} hPa below threshold")


def foehn_override(snapshot: PressureSnapshot) -> Verdict:
    delta = snapshot.foehn_delta_hpa
    if delta >= config.FOEHN_TRIGGER_DELTA_HPA:
        return Verdict(
            "foehn_override",
            Signal.NO_GO,
            f"Bolzano−Innsbruck Δ={delta:.1f} hPa indicates Föhn — thermal suppressed",
        )
    return Verdict("foehn_override", Signal.GO, f"no Föhn pressure signature (Δ={delta:.1f} hPa)")


def overnight_cooling(meteo: MeteoSnapshot) -> Verdict:
    pct = meteo.overnight_cloud_cover_pct
    if pct <= config.MAX_OVERNIGHT_CLOUD_COVER_PCT:
        return Verdict("overnight_cooling", Signal.GO, f"clear night: {pct:.0f}% cloud cover")
    return Verdict(
        "overnight_cooling",
        Signal.NO_GO,
        f"{pct:.0f}% overnight cloud cover — weak inversion",
    )


def solar_radiation(meteo: MeteoSnapshot) -> Verdict:
    wm2 = meteo.morning_solar_radiation_wm2
    if wm2 >= config.MIN_MORNING_SOLAR_WM2:
        return Verdict("solar_radiation", Signal.GO, f"peak radiation {wm2:.0f} W/m² ≥ threshold")
    return Verdict(
        "solar_radiation",
        Signal.NO_GO,
        f"peak radiation {wm2:.0f} W/m² below {config.MIN_MORNING_SOLAR_WM2:.0f}",
    )


def synoptic_override(meteo: MeteoSnapshot) -> Verdict:
    if meteo.synoptic_wind_knots >= config.SYNOPTIC_OVERRIDE_KNOTS:
        return Verdict(
            "synoptic_override",
            Signal.NO_GO,
            f"synoptic wind {meteo.synoptic_wind_knots:.0f} kt will destroy the thermal cell",
        )
    return Verdict("synoptic_override", Signal.GO, "no overwhelming synoptic flow")


def thermal_ignition(readings: list[WindReading]) -> Verdict:
    ignited = [r for r in readings if r.avg_knots >= config.IGNITION_WIND_KNOTS]
    if ignited:
        names = ", ".join(r.station for r in ignited)
        return Verdict("thermal_ignition", Signal.GO, f"ignited at {names}")
    return Verdict("thermal_ignition", Signal.MAYBE, "no station has ignited yet")
