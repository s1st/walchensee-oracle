from datetime import datetime

from oracle.config import StationRole
from oracle.knowledge.rules import (
    Signal,
    alpenpumpe_threshold,
    synoptic_override,
    thermal_ignition,
)
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureReading, PressureSnapshot


def _snapshot(alpenpumpe_delta: float, foehn_delta: float = 0.0) -> PressureSnapshot:
    now = datetime.now()
    innsbruck_hpa = 1018.0
    return PressureSnapshot(
        alpenpumpe_north=PressureReading("Munich", innsbruck_hpa + alpenpumpe_delta, now),
        alpenpumpe_south=PressureReading("Innsbruck", innsbruck_hpa, now),
        foehn_south=PressureReading("Bolzano", innsbruck_hpa + foehn_delta, now),
    )


def test_alpenpumpe_threshold_go():
    assert alpenpumpe_threshold(_snapshot(5.0)).signal is Signal.GO


def test_alpenpumpe_threshold_no_go():
    assert alpenpumpe_threshold(_snapshot(1.0)).signal is Signal.NO_GO


def test_synoptic_override_kills_thermal():
    snap = MeteoSnapshot(
        day=datetime.now().date(),
        overnight_cloud_cover_pct=10,
        morning_solar_radiation_wm2=800,
        synoptic_wind_knots=20,
    )
    assert synoptic_override(snap).signal is Signal.NO_GO


def test_thermal_ignition_detects_ignited_station():
    winds = [
        WindReading("Urfeld", StationRole.SHORE, 12.0, 18.0, 90.0, datetime.now()),
    ]
    assert thermal_ignition(winds).signal is Signal.GO
