from datetime import datetime

from oracle.knowledge.rules import Signal, pressure_threshold, synoptic_override, thermal_ignition
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureGradient, PressureReading


def _gradient(delta: float) -> PressureGradient:
    now = datetime.now()
    return PressureGradient(
        north=PressureReading("Munich", 1020.0, now),
        south=PressureReading("Innsbruck", 1020.0 - delta, now),
    )


def test_pressure_threshold_go():
    assert pressure_threshold(_gradient(5.0)).signal is Signal.GO


def test_pressure_threshold_no_go():
    assert pressure_threshold(_gradient(1.0)).signal is Signal.NO_GO


def test_synoptic_override_kills_thermal():
    snap = MeteoSnapshot(
        day=datetime.now().date(),
        overnight_cloud_cover_pct=10,
        morning_solar_radiation_wm2=800,
        synoptic_wind_knots=25,
    )
    assert synoptic_override(snap).signal is Signal.NO_GO


def test_thermal_ignition_detects_ignited_station():
    winds = [WindReading("Urfeld", 12.0, 18.0, 90.0, datetime.now())]
    assert thermal_ignition(winds).signal is Signal.GO
