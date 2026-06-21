"""Ignition-timing band (Stage 0 — research, NO-BUILD).

Conclusion (2026-06-21, see docs/findings/ignition-timing-2026-06-21.md): onset
time is not predictable from day-ahead meteo at useful precision — this band
scores a weak Spearman ~0.17 vs the real onset on 10 y of labels, and the
follow-up Stage-1 intraday spike confirmed the ceiling is too low to ship. Kept
re-runnable, never wired into prod.

The 14-rule forecaster answers *whether* the thermal fires; it says nothing
about *when*. Riders pay for that gap — waiting out a dead morning on a day
that only ignites mid-afternoon (e.g. 2026-06-21: counter-gradient + convective
instability, ignition ~15:00).

This module estimates an **early / midday / late** band from the same pressure
and meteo snapshot the rules already consume. It is a transparent, signed
"lateness" score — no fitted model — so every band carries a one-line reason in
DE and EN, same as a rule Verdict.

Physics, in order of signal strength:

* **Pressure gradient** (`thermik_delta_hpa`) is the dominant knob. A favourable
  (positive) gradient assists the up-valley flow → the engine spins up early.
  A counter-gradient (negative) means the thermal has to overcome opposing flow
  and only breaks through near peak heating → late.
* **Convective instability** (`min_lifted_index < 0`) delays a clean onset: the
  air wants to go vertical, the surface thermal builds and then bursts late.
* **Morning low cloud** shades the slopes and pushes ignition later.
* **Morning solar / boundary-layer depth** are weak *earlier* pulls — a strong,
  already-deep mixed layer by midday means the engine is hot sooner.

NOTE: this never touches `overall`. Cutoffs in `_EARLY_MAX` / `_LATE_MIN` are
first-pass; `scripts/validate_ignition_timing.py` scores the band against the
sustained daytime onset (not `first_ignition_at`, which catches night wind).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureSnapshot


class Band(str, Enum):
    EARLY = "early"
    MIDDAY = "midday"
    LATE = "late"


# Human-readable expected window per band (Europe/Berlin clock), for the
# dashboard hint. Deliberately a *band*, not a clock time — a wrong concrete
# time is worse than none (it sends riders at the wrong hour).
_WINDOW = {
    Band.EARLY: ("~10–12 Uhr", "~10:00–12:00"),
    Band.MIDDAY: ("~12–14 Uhr", "~12:00–14:00"),
    Band.LATE: ("ab ~14 Uhr / konvektiv spät", "from ~14:00 / convective-late"),
}

# Signed weights — each term contributes "hours of lateness" (positive = later).
# First-pass, tuned for sign and rough scale, validated against ground truth.
_GRAD_W = 0.55      # per hPa of (counter-)gradient
_LI_W = 0.40        # per unit of negative lifted index
_CLOUD_W = 1.6      # per unit fraction of midday low cloud
_SOLAR_W = 0.8      # earlier pull when midday sun is strong
_BLH_W = 0.6        # earlier pull when the mixed layer is already deep
_REF_SOLAR_WM2 = 700.0
_REF_BLH_M = 1500.0

# Band cutoffs on the lateness score.
_EARLY_MAX = -0.3
_LATE_MIN = 1.2


@dataclass
class IgnitionTiming:
    band: Band
    score: float                 # signed lateness; higher = later
    reason_en: str
    reason_de: str

    @property
    def window_de(self) -> str:
        return _WINDOW[self.band][0]

    @property
    def window_en(self) -> str:
        return _WINDOW[self.band][1]


@dataclass
class _Term:
    """One signed contribution to the lateness score, with a bilingual label."""
    value: float
    label_en: str
    label_de: str


def _terms(
    *,
    thermik_delta_hpa: float | None,
    min_lifted_index: float | None,
    max_daytime_low_cloud_pct: float | None,
    morning_solar_radiation_wm2: float | None,
    max_boundary_layer_height_m: float | None,
) -> list[_Term]:
    """Build the signed term list from raw values. Shared by the live-snapshot
    adapter and the validation script (stored `inputs.*` dicts) so train/serve
    can't drift — same contract as the ML feature reader."""
    terms: list[_Term] = []

    if thermik_delta_hpa is not None:
        v = -_GRAD_W * thermik_delta_hpa
        if thermik_delta_hpa < 0:
            terms.append(_Term(v, "counter-gradient delays onset", "Gegengradient verzögert Zündung"))
        else:
            terms.append(_Term(v, "favourable gradient — early spin-up", "günstiger Gradient — frühe Zündung"))

    if min_lifted_index is not None and min_lifted_index < 0:
        terms.append(_Term(_LI_W * (-min_lifted_index),
                           "convective instability — late burst",
                           "Labilität — späte konvektive Zündung"))

    if max_daytime_low_cloud_pct is not None and max_daytime_low_cloud_pct > 0:
        terms.append(_Term(_CLOUD_W * (max_daytime_low_cloud_pct / 100.0),
                           "midday low cloud shades the slopes",
                           "tiefe Wolken beschatten die Hänge"))

    if morning_solar_radiation_wm2 is not None:
        v = _SOLAR_W * (1.0 - morning_solar_radiation_wm2 / _REF_SOLAR_WM2)
        if v < 0:
            terms.append(_Term(v, "strong midday sun — earlier", "starke Einstrahlung — früher"))

    if max_boundary_layer_height_m is not None:
        v = _BLH_W * (1.0 - max_boundary_layer_height_m / _REF_BLH_M)
        if v < 0:
            terms.append(_Term(v, "deep mixed layer by midday — earlier",
                               "tiefe Durchmischung mittags — früher"))

    return terms


def _band_for(score: float) -> Band:
    if score <= _EARLY_MAX:
        return Band.EARLY
    if score >= _LATE_MIN:
        return Band.LATE
    return Band.MIDDAY


def _assemble(terms: list[_Term]) -> IgnitionTiming:
    score = sum(t.value for t in terms)
    band = _band_for(score)
    # Reason names the single dominant driver (largest |contribution|).
    driver = max(terms, key=lambda t: abs(t.value)) if terms else None
    window_de, window_en = _WINDOW[band]
    if driver is None:
        return IgnitionTiming(band, score,
                              reason_en=f"ignition {window_en} (no strong timing signal)",
                              reason_de=f"Zündung {window_de} (kein starkes Timing-Signal)")
    return IgnitionTiming(
        band, score,
        reason_en=f"ignition {window_en} — {driver.label_en}",
        reason_de=f"Zündung {window_de} — {driver.label_de}",
    )


def estimate(pressure: PressureSnapshot, meteo: MeteoSnapshot) -> IgnitionTiming:
    """Live-snapshot entry point: estimate the ignition band for the day."""
    return _assemble(_terms(
        thermik_delta_hpa=pressure.thermik_delta_hpa,
        min_lifted_index=meteo.min_lifted_index,
        max_daytime_low_cloud_pct=meteo.max_daytime_low_cloud_pct,
        morning_solar_radiation_wm2=meteo.morning_solar_radiation_wm2,
        max_boundary_layer_height_m=meteo.max_boundary_layer_height_m,
    ))


def estimate_from_inputs(pressure: dict, meteo: dict) -> IgnitionTiming:
    """Stored-record entry point: estimate from the `inputs.pressure` /
    `inputs.meteo` dicts a run JSON carries. Same keys the live snapshots
    serialise to, so the validation harness scores exactly what would ship."""
    return _assemble(_terms(
        thermik_delta_hpa=pressure.get("thermik_delta_hpa"),
        min_lifted_index=meteo.get("min_lifted_index"),
        max_daytime_low_cloud_pct=meteo.get("max_daytime_low_cloud_pct"),
        morning_solar_radiation_wm2=meteo.get("morning_solar_radiation_wm2"),
        max_boundary_layer_height_m=meteo.get("max_boundary_layer_height_m"),
    ))
