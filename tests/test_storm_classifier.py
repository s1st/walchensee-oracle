"""Tests for the thunderstorm-advisory classifier (storm_classifier.py).

Guards three contracts:
  1. Golden vector — pins the frozen coefficients (export rewrites them; this flags
     the change, like tests/test_ml_classifier.py).
  2. Fallback — missing afternoon features fall back to the LI ≤ −2 rule.
  3. Shadow-invariant — the storm advisory NEVER changes the verdict (the LI veto
     was decoupled; atmospheric_stability stays GREEN regardless).
"""
from __future__ import annotations

import datetime as dt

from oracle import storm_classifier as SC
from oracle.knowledge.rules import Severity, Signal, atmospheric_stability
from oracle.pillars.meteo import MeteoSnapshot

# A representative convective afternoon (canonical raw feature dict).
_STORM_RAW = {"cape": 1200.0, "li": -4.5, "cin": -60.0, "precip": 8.5, "shear": 18.0, "low_cloud": 80.0}
_CALM_RAW = {"cape": 50.0, "li": 1.0, "cin": -2.0, "precip": 0.0, "shear": 8.0, "low_cloud": 30.0}


def test_golden_vector_pins_coefficients():
    # Flags any change to knowledge/storm_coeffs.py. Regenerate + update on retrain.
    p = SC.storm_probability(_STORM_RAW)
    assert p is not None
    assert abs(p - 0.8462720479595617) < 1e-9


def test_storm_vs_calm_advisory():
    assert SC.storm_advisory(_STORM_RAW, -4.5) is True
    assert SC.storm_advisory(_CALM_RAW, 1.0) is False


def test_missing_features_fall_back_to_li_rule():
    # No afternoon features at all → LI ≤ −2 fallback on the morning LI.
    assert SC.storm_probability({}) is None
    assert SC.storm_advisory({}, -3.0) is True     # LI −3 ≤ −2
    assert SC.storm_advisory({}, 0.0) is False
    assert SC.storm_advisory({}, None) is False     # no signal at all


def test_partial_features_fall_back():
    # One required raw feature missing → still falls back (no silent imputation).
    raw = dict(_STORM_RAW)
    raw["shear"] = None
    assert SC.storm_probability(raw) is None
    assert SC.storm_advisory(raw, -3.0) is True     # via LI fallback


def test_operating_point_comes_from_config_knob(monkeypatch):
    # The advisory threshold is the product knob in config, not the export's
    # storm_coeffs.THRESHOLD (which a retrain would overwrite). p(_STORM_RAW)≈0.85.
    from oracle import config
    monkeypatch.setattr(config, "STORM_ADVISORY_THRESHOLD", 0.99)
    assert SC.storm_advisory(_STORM_RAW, -4.5) is False   # 0.85 < 0.99
    monkeypatch.setattr(config, "STORM_ADVISORY_THRESHOLD", 0.10)
    assert SC.storm_advisory(_STORM_RAW, -4.5) is True     # 0.85 ≥ 0.10


def _meteo(**kw) -> MeteoSnapshot:
    base = dict(
        day=dt.date(2022, 6, 24), overnight_cloud_cover_pct=10.0, morning_solar_radiation_wm2=500.0,
        synoptic_wind_knots=5.0, min_dew_point_spread_c=3.0, max_boundary_layer_height_m=1500.0,
        soil_moisture_m3m3=0.2, rained_yesterday=False, yesterday_precipitation_mm=0.0,
        max_lifted_index=2.0, min_lifted_index=-3.0, max_cape_j_kg=800.0,
        max_daytime_low_cloud_pct=40.0, wind_850_direction_at_peak_deg=270.0,
        max_wind_700_knots=20.0, morning_air_temp_c=18.0,
    )
    base.update(kw)
    return MeteoSnapshot(**base)


def test_shadow_invariant_storm_never_vetoes_verdict():
    # A strongly convective afternoon: the classifier fires, but the verdict rule
    # stays GREEN (decoupled) — the advisory must never reach the aggregator.
    storm = _meteo(
        afternoon_cape_max_j_kg=1200.0, afternoon_li_min=-4.5, afternoon_cin_min_j_kg=-60.0,
        afternoon_precip_mm=8.5, afternoon_shear_kn=18.0, afternoon_low_cloud_max_pct=80.0,
    )
    assert SC.storm_advisory_from_snapshot(storm) is True
    v = atmospheric_stability(storm)
    assert v.signal is Signal.GO
    assert v.severity is not Severity.HARD
