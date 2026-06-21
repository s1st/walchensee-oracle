"""Stage-1 ignition-timing features — intraday, not morning-aggregate.

The Stage-0 heuristic (`oracle.knowledge.ignition_timing`) scored a weak
Spearman ~0.17 against onset on 10 years of clean labels: a thermal's *onset
time* doesn't live in the 10:30–15:00 morning maxima. It should live in the
**intraday shape** — when solar crosses an ignition-capable level, when the
low-cloud deck clears, how fast the surface heats. `meteo.py` already fetches
the hourly arrays and throws that shape away.

This module extracts timing features from the raw Open-Meteo hourly payload
over an extended daytime window. It is research-only (driven by
`scripts/intraday_timing_spike.py`); if the spike shows real signal we wire a
distilled version into the production snapshot.

Available intraday across the historical-forecast archive back to 2021:
shortwave_radiation, cloud_cover_low, temperature_2m, lifted_index. NOT
boundary_layer_height (null before ~2025) — deliberately excluded so the
feature set is uniform across the validation years.
"""
from __future__ import annotations

from datetime import date, datetime, time

# Extended daytime scan window — past the morning box, into the afternoon when
# late thermals actually fire.
_DAY_START = time(7, 0)
_DAY_END = time(18, 0)

# A sentinel "never reached" minute for crossing features (after the window).
_NEVER = _DAY_END.hour * 60 + _DAY_END.minute + 60

# Solar thresholds (W/m²) whose first-crossing time we record — a coarse
# "how far up the heating ramp, and when" descriptor.
_SOLAR_THRESHOLDS = (200.0, 400.0, 600.0)
_CLOUD_CLEAR_PCT = 30.0


def _minute(t: datetime) -> int:
    return t.hour * 60 + t.minute


def _daytime(times: list[datetime], values: list, day: date) -> list[tuple[int, float]]:
    """(minute-of-day, value) pairs inside [day 07:00, day 18:00], non-null."""
    start = datetime.combine(day, _DAY_START)
    end = datetime.combine(day, _DAY_END)
    return [
        (_minute(t), float(v))
        for t, v in zip(times, values, strict=True)
        if v is not None and start <= t <= end
    ]


def _first_cross_up(series: list[tuple[int, float]], thresh: float) -> int:
    for minute, v in series:
        if v >= thresh:
            return minute
    return _NEVER


def _first_cross_down(series: list[tuple[int, float]], thresh: float) -> int:
    for minute, v in series:
        if v <= thresh:
            return minute
    return _NEVER


def _value_near(series: list[tuple[int, float]], target_min: int) -> float | None:
    """Value at the sample closest to `target_min` (within the window)."""
    if not series:
        return None
    return min(series, key=lambda mv: abs(mv[0] - target_min))[1]


def _integral_before(series: list[tuple[int, float]], cutoff_min: int) -> float:
    return sum(v for m, v in series if m <= cutoff_min)


def intraday_features(hourly: dict, times: list[datetime], day: date) -> dict | None:
    """Extract Stage-1 timing features for `day` from a raw hourly payload.

    Returns None if the daytime solar window isn't covered (can't assess).
    Crossing features default to `_NEVER` when the level is never reached, so
    "the deck never cleared" / "solar never hit 600" is itself a (large) signal.
    """
    solar = _daytime(times, hourly["shortwave_radiation"], day)
    if not solar:
        return None
    low_cloud = _daytime(times, hourly.get("cloud_cover_low") or [], day)
    temp = _daytime(times, hourly.get("temperature_2m") or [], day)
    li = _daytime(times, hourly.get("lifted_index") or [], day)

    feats: dict[str, float] = {}
    for thr in _SOLAR_THRESHOLDS:
        feats[f"solar_cross_{int(thr)}_min"] = float(_first_cross_up(solar, thr))
    feats["solar_morning_integral"] = _integral_before(solar, 12 * 60)
    feats["solar_peak_min"] = float(max(solar, key=lambda mv: mv[1])[0])

    if low_cloud:
        feats["low_cloud_clear_min"] = float(_first_cross_down(low_cloud, _CLOUD_CLEAR_PCT))
        feats["low_cloud_morning_mean"] = (
            sum(v for m, v in low_cloud if m <= 12 * 60)
            / max(1, sum(1 for m, _ in low_cloud if m <= 12 * 60))
        )
    if temp:
        t8 = _value_near(temp, 8 * 60)
        t12 = _value_near(temp, 12 * 60)
        if t8 is not None and t12 is not None:
            feats["temp_rise_8_to_12_c"] = t12 - t8
    if li:
        feats["li_min_daytime"] = min(v for _, v in li)
        feats["li_cross0_min"] = float(_first_cross_down(li, 0.0))

    return feats
