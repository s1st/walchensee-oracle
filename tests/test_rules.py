from datetime import datetime

from oracle.config import StationRole
from oracle.knowledge.rules import (
    Signal,
    atmospheric_stability,
    boundary_layer_height,
    daytime_clouds,
    dew_point_spread,
    foehn_override,
    overnight_cooling,
    post_rain_moisture,
    solar_radiation,
    synoptic_override,
    thermal_ignition,
    thermik,
    upper_level_wind,
)
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureReading, PressureSnapshot


def _snapshot(thermik_delta: float, foehn_delta: float = 0.0) -> PressureSnapshot:
    now = datetime.now()
    innsbruck_hpa = 1018.0
    return PressureSnapshot(
        thermik_north=PressureReading("Munich", innsbruck_hpa + thermik_delta, now),
        thermik_south=PressureReading("Innsbruck", innsbruck_hpa, now),
        foehn_south=PressureReading("Bolzano", innsbruck_hpa + foehn_delta, now),
    )


def test_thermik_go():
    assert thermik(_snapshot(5.0)).signal is Signal.GO


def test_thermik_no_go():
    assert thermik(_snapshot(1.0)).signal is Signal.NO_GO


def test_foehn_override_flags_southerly_pressure():
    assert foehn_override(_snapshot(3.0, foehn_delta=5.0)).signal is Signal.NO_GO


def test_foehn_override_clear_when_pressure_balanced():
    assert foehn_override(_snapshot(3.0, foehn_delta=0.5)).signal is Signal.GO


def _meteo(
    *,
    cloud: float = 10,
    solar: float = 800,
    synoptic: float = 5,
    dew_spread: float = 10.0,
    blh: float = 1500.0,
    soil: float = 0.20,
    rained_yesterday: bool = False,
    yesterday_mm: float = 0.0,
    max_li: float = 3.0,
    min_li: float = 1.0,
    cape: float = 0.0,
    daytime_low_cloud: float = 20.0,
    wind_850_dir: float = 30.0,
    wind_700: float = 10.0,
) -> MeteoSnapshot:
    return MeteoSnapshot(
        day=datetime.now().date(),
        overnight_cloud_cover_pct=cloud,
        morning_solar_radiation_wm2=solar,
        synoptic_wind_knots=synoptic,
        min_dew_point_spread_c=dew_spread,
        max_boundary_layer_height_m=blh,
        soil_moisture_m3m3=soil,
        rained_yesterday=rained_yesterday,
        yesterday_precipitation_mm=yesterday_mm,
        max_lifted_index=max_li,
        min_lifted_index=min_li,
        max_cape_j_kg=cape,
        max_daytime_low_cloud_pct=daytime_low_cloud,
        wind_850_direction_at_peak_deg=wind_850_dir,
        max_wind_700_knots=wind_700,
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


def test_dew_point_spread_dry_air_go():
    assert dew_point_spread(_meteo(dew_spread=10.0)).signal is Signal.GO


def test_dew_point_spread_humid_no_go():
    assert dew_point_spread(_meteo(dew_spread=3.0)).signal is Signal.NO_GO


def test_dew_point_spread_marginal_maybe():
    assert dew_point_spread(_meteo(dew_spread=6.0)).signal is Signal.MAYBE


def test_boundary_layer_deep_go():
    assert boundary_layer_height(_meteo(blh=1500.0)).signal is Signal.GO


def test_boundary_layer_shallow_maybe():
    assert boundary_layer_height(_meteo(blh=800.0)).signal is Signal.MAYBE


def test_boundary_layer_capped_no_go():
    assert boundary_layer_height(_meteo(blh=400.0)).signal is Signal.NO_GO


def test_post_rain_yesterday_blocks():
    v = post_rain_moisture(_meteo(rained_yesterday=True, yesterday_mm=5.2))
    assert v.signal is Signal.NO_GO
    assert "5.2" in v.reason


def test_post_rain_wet_soil_blocks():
    assert post_rain_moisture(_meteo(soil=0.40)).signal is Signal.NO_GO


def test_post_rain_dry_ground_go():
    assert post_rain_moisture(_meteo(soil=0.18)).signal is Signal.GO


def test_atmospheric_stability_too_stable_no_go():
    assert atmospheric_stability(_meteo(max_li=7.0, min_li=5.0)).signal is Signal.NO_GO


def test_atmospheric_stability_storm_risk_no_go():
    assert atmospheric_stability(_meteo(max_li=0.0, min_li=-3.0)).signal is Signal.NO_GO


def test_atmospheric_stability_normal_go():
    assert atmospheric_stability(_meteo(max_li=3.0, min_li=1.0)).signal is Signal.GO


def test_daytime_clouds_clear_go():
    assert daytime_clouds(_meteo(daytime_low_cloud=15)).signal is Signal.GO


def test_daytime_clouds_overcast_no_go():
    assert daytime_clouds(_meteo(daytime_low_cloud=80)).signal is Signal.NO_GO


def test_daytime_clouds_mixed_maybe():
    assert daytime_clouds(_meteo(daytime_low_cloud=45)).signal is Signal.MAYBE


def test_upper_level_wind_opposing_direction_no_go():
    v = upper_level_wind(_meteo(wind_850_dir=180))  # pure S
    assert v.signal is Signal.NO_GO
    assert "SSE" in v.reason or "180" in v.reason


def test_upper_level_wind_crossflow_too_strong_no_go():
    v = upper_level_wind(_meteo(wind_850_dir=30, wind_700=30))
    assert v.signal is Signal.NO_GO
    assert "Querströmung" in v.reason or "700" in v.reason


def test_upper_level_wind_neutral_go():
    assert upper_level_wind(_meteo(wind_850_dir=10, wind_700=8)).signal is Signal.GO


def test_thermal_ignition_detects_ignited_station():
    winds = [
        WindReading("Urfeld", StationRole.SHORE, 12.0, 18.0, 90.0, datetime.now()),
    ]
    assert thermal_ignition(winds).signal is Signal.GO
