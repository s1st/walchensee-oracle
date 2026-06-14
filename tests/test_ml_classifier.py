"""Unit tests for the shadow ML classifier.

The pure-Python scorer was verified once to reproduce sklearn's `predict`
exactly on all 1912 training rows; these tests freeze that behaviour and
guard the *shadow invariant* — the classifier must never alter `overall`.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from oracle.engine import Forecast
from oracle.config import StationRole
from oracle.knowledge.rules import Signal, Verdict
from oracle.logger import forecast_to_dict
from oracle.ml_classifier import classify
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureReading, PressureSnapshot

# A real replay day (2026-06-12) whose sklearn output is known-good.
_PRESSURE = {
    "munich_hpa": 1023.9, "innsbruck_hpa": 1026.1, "bolzano_hpa": 1024.8,
    "thermik_delta_hpa": -2.2, "foehn_delta_hpa": -1.3,
}
_METEO = {
    "overnight_cloud_cover_pct": 91.0, "morning_solar_radiation_wm2": 112.0,
    "min_dew_point_spread_c": 0.4, "rained_yesterday": True,
    "yesterday_precipitation_mm": 2.1, "max_daytime_low_cloud_pct": 100.0,
}


def test_classify_golden_vector():
    """Frozen against the sklearn-verified output for this input."""
    ml = classify(_PRESSURE, _METEO)
    assert ml is not None
    assert ml.verdict == "maybe"
    assert ml.probabilities["go"] == pytest.approx(0.195, abs=1e-3)
    assert ml.probabilities["maybe"] == pytest.approx(0.540, abs=1e-3)
    assert ml.probabilities["no_go"] == pytest.approx(0.265, abs=1e-3)


def test_probabilities_sum_to_one_and_verdict_is_argmax():
    ml = classify(_PRESSURE, _METEO)
    assert sum(ml.probabilities.values()) == pytest.approx(1.0)
    assert ml.verdict == max(ml.probabilities, key=ml.probabilities.__getitem__)
    assert len(ml.contributions) == 3


def test_missing_feature_is_median_imputed_not_crash():
    """A missing feature falls back to the training median — no KeyError."""
    sparse_meteo = {k: v for k, v in _METEO.items() if k != "morning_solar_radiation_wm2"}
    ml = classify(_PRESSURE, sparse_meteo)
    assert ml is not None
    assert sum(ml.probabilities.values()) == pytest.approx(1.0)


def test_missing_pillar_returns_none():
    assert classify(None, _METEO) is None
    assert classify(_PRESSURE, None) is None
    assert classify({}, _METEO) is None


def test_rained_yesterday_bool_is_accepted():
    """The bool feature must not blow up the float maths."""
    assert classify(_PRESSURE, {**_METEO, "rained_yesterday": False}) is not None


def test_reasons_are_bilingual():
    ml = classify(_PRESSURE, _METEO)
    assert "Learned model" in ml.reason_en and "MAYBE" in ml.reason_en
    assert "Gelerntes Modell" in ml.reason_de


# --- shadow invariant ------------------------------------------------------

def _forecast(overall: Signal) -> Forecast:
    now = datetime(2026, 6, 12, 9, 0)
    return Forecast(
        overall=overall,
        verdicts=[Verdict("thermik", overall, reason_en="x", reason_de="x")],
        pressure=PressureSnapshot(
            thermik_north=PressureReading("Munich", 1023.9, now),
            thermik_south=PressureReading("Innsbruck", 1026.1, now),
            foehn_south=PressureReading("Bolzano", 1024.8, now),
        ),
        meteo=MeteoSnapshot(
            day=date(2026, 6, 12), overnight_cloud_cover_pct=91.0,
            morning_solar_radiation_wm2=112.0, synoptic_wind_knots=8.0,
            min_dew_point_spread_c=0.4, max_boundary_layer_height_m=1200.0,
            soil_moisture_m3m3=0.22, rained_yesterday=True,
            yesterday_precipitation_mm=2.1, max_lifted_index=3.0,
            min_lifted_index=1.0, max_cape_j_kg=0.0, max_daytime_low_cloud_pct=100.0,
            wind_850_direction_at_peak_deg=30.0, max_wind_700_knots=10.0,
        ),
        winds=[WindReading("Urfeld", StationRole.SHORE, 2.1, 4.5, None, now)],
        lake_temp=None,
    )


def test_serialised_block_is_present_and_well_formed():
    d = forecast_to_dict(_forecast(Signal.NO_GO), date(2026, 6, 12))
    assert "ml_classifier" in d
    block = d["ml_classifier"]
    assert block["verdict"] in {"go", "maybe", "no_go"}
    assert set(block["probabilities"]) == {"go", "maybe", "no_go"}
    assert "model" in block


def test_shadow_invariant_ml_does_not_change_overall():
    """The official `overall` equals the rule verdict regardless of what the
    ML classifier says — even when they disagree."""
    for sig in (Signal.GO, Signal.MAYBE, Signal.NO_GO):
        d = forecast_to_dict(_forecast(sig), date(2026, 6, 12))
        assert d["overall"] == sig.value          # unchanged by the ML block
        # the ML verdict on this (cloudy, low-solar) day is "maybe" — so for
        # GO/NO_GO it genuinely disagrees, proving non-interference.
        assert d["ml_classifier"]["verdict"] == "maybe"


def test_dashboard_renders_ml_card(tmp_path, monkeypatch):
    """The experimental ML card appears (both languages) when the record
    carries an `ml_classifier` block."""
    from starlette.testclient import TestClient

    import oracle.dashboard.main as dash
    from oracle.logger import LocalRunStore, write_run

    store = LocalRunStore(tmp_path)
    write_run(_forecast(Signal.NO_GO), date(2026, 6, 12), store=store)
    monkeypatch.setattr(dash, "_store", lambda: store)
    # Keep the test hermetic/fast: stub the route's live external fetches.
    async def _no_live():
        return {}
    async def _no_views():
        return None
    async def _no_stats():
        return None
    monkeypatch.setattr(dash, "_fetch_urfeld_live", _no_live)
    monkeypatch.setattr(dash, "_fetch_page_views", _no_views)
    monkeypatch.setattr(dash, "_forecast_stats", _no_stats)
    client = TestClient(dash.app)

    en = client.get("/?day=2026-06-12&lang=en").text
    assert "ML Classifier" in en and "experimental" in en
    assert "Learned model" in en and "maybe 54%" in en
    assert "ML classifier (exp.)" in en  # 30-day strip row
    de = client.get("/?day=2026-06-12&lang=de").text
    assert "Gelerntes Modell" in de
    assert "ML-Klassifikator (exp.)" in de  # 30-day strip row
