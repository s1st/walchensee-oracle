from datetime import datetime

from oracle.config import StationRole
from oracle.knowledge.rules import (
    Signal,
    alpenpumpe_threshold,
    foehn_override,
    overnight_cooling,
    solar_radiation,
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


def test_foehn_override_flags_southerly_pressure():
    assert foehn_override(_snapshot(3.0, foehn_delta=5.0)).signal is Signal.NO_GO


def test_foehn_override_clear_when_pressure_balanced():
    assert foehn_override(_snapshot(3.0, foehn_delta=0.5)).signal is Signal.GO


def _meteo(*, cloud: float = 10, solar: float = 800, synoptic: float = 5) -> MeteoSnapshot:
    return MeteoSnapshot(
        day=datetime.now().date(),
        overnight_cloud_cover_pct=cloud,
        morning_solar_radiation_wm2=solar,
        synoptic_wind_knots=synoptic,
    )


def test_overnight_cooling_clear_night_go():
    assert overnight_cooling(_meteo(cloud=15)).signal is Signal.GO


def test_overnight_cooling_cloudy_night_no_go():
    assert overnight_cooling(_meteo(cloud=60)).signal is Signal.NO_GO


def test_solar_radiation_bright_morning_go():
    assert solar_radiation(_meteo(solar=750)).signal is Signal.GO


def test_solar_radiation_dim_morning_no_go():
    assert solar_radiation(_meteo(solar=400)).signal is Signal.NO_GO


def test_synoptic_override_kills_thermal():
    assert synoptic_override(_meteo(synoptic=20)).signal is Signal.NO_GO


def test_thermal_ignition_detects_ignited_station():
    winds = [
        WindReading("Urfeld", StationRole.SHORE, 12.0, 18.0, 90.0, datetime.now()),
    ]
    assert thermal_ignition(winds).signal is Signal.GO
