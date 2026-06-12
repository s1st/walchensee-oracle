from datetime import datetime

from oracle.config import StationRole
from oracle.engine import apply_rules
from oracle.knowledge.rules import (
    Severity,
    Signal,
    air_lake_delta,
    atmospheric_stability,
    boundary_layer_height,
    daytime_clouds,
    dew_point_spread,
    foehn_override,
    is_storm_risk,
    overnight_cooling,
    post_rain_moisture,
    solar_radiation,
    synoptic_override,
    thermal_ignition,
    thermik,
    upper_level_wind,
)
from oracle.pillars.measurements import LakeTempSnapshot, WindReading
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
    assert thermik(_snapshot(-2.0)).signal is Signal.NO_GO


def test_foehn_override_flags_southerly_pressure():
    # 12 hPa is well above the data-fitted 10 hPa threshold (essentially
    # "rule never fires in 3,331 days, kept as a safety net").
    assert foehn_override(_snapshot(3.0, foehn_delta=12.0)).signal is Signal.NO_GO


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
    air_temp: float | None = 15.0,
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
        morning_air_temp_c=air_temp,
    )


def test_overnight_cooling_clear_night_go():
    assert overnight_cooling(_meteo(cloud=15)).signal is Signal.GO


def test_overnight_cooling_cloudy_night_no_go():
    assert overnight_cooling(_meteo(cloud=97)).signal is Signal.NO_GO


def test_solar_radiation_bright_morning_go():
    assert solar_radiation(_meteo(solar=750)).signal is Signal.GO


def test_solar_radiation_dim_morning_no_go():
    # 300 W/m² is well below the data-fitted 380 W/m² threshold.
    assert solar_radiation(_meteo(solar=300)).signal is Signal.NO_GO


def test_synoptic_override_kills_thermal():
    # 30 kt is well above the data-fitted 25 kt threshold.
    assert synoptic_override(_meteo(synoptic=30)).signal is Signal.NO_GO


def test_synoptic_override_calm_go():
    assert synoptic_override(_meteo(synoptic=8)).signal is Signal.GO


def test_dew_point_spread_dry_air_go():
    assert dew_point_spread(_meteo(dew_spread=10.0)).signal is Signal.GO


def test_dew_point_spread_humid_no_go():
    assert dew_point_spread(_meteo(dew_spread=2.0)).signal is Signal.NO_GO


def test_dew_point_spread_marginal_maybe():
    assert dew_point_spread(_meteo(dew_spread=6.0)).signal is Signal.MAYBE


def test_boundary_layer_deep_go():
    assert boundary_layer_height(_meteo(blh=1500.0)).signal is Signal.GO


def test_boundary_layer_shallow_maybe():
    assert boundary_layer_height(_meteo(blh=800.0)).signal is Signal.MAYBE


def test_boundary_layer_capped_no_go():
    # 300 m is well below the data-fitted 400 m threshold.
    assert boundary_layer_height(_meteo(blh=300.0)).signal is Signal.NO_GO


def test_post_rain_yesterday_alone_does_not_block():
    # rained_yesterday was dropped as a veto (13/17 FP on calibration data);
    # only wet soil blocks now.
    v = post_rain_moisture(_meteo(rained_yesterday=True, yesterday_mm=5.2, soil=0.18))
    assert v.signal is Signal.GO


def test_post_rain_wet_soil_blocks():
    assert post_rain_moisture(_meteo(soil=0.40)).signal is Signal.NO_GO


def test_post_rain_dry_ground_go():
    assert post_rain_moisture(_meteo(soil=0.18)).signal is Signal.GO


def test_atmospheric_stability_too_stable_no_go():
    assert atmospheric_stability(_meteo(max_li=11.0, min_li=5.0)).signal is Signal.NO_GO


def test_atmospheric_stability_storm_risk_no_go():
    assert atmospheric_stability(_meteo(max_li=0.0, min_li=-3.0)).signal is Signal.NO_GO


def test_atmospheric_stability_normal_go():
    assert atmospheric_stability(_meteo(max_li=3.0, min_li=1.0)).signal is Signal.GO


def test_is_storm_risk_matches_atmospheric_stability_hard_veto():
    # The predicate must agree with the rule's HARD branch at the threshold:
    # LI ≤ MIN_LIFTED_INDEX (−2) is a storm; just above it is not.
    assert is_storm_risk(-3.0) is True
    assert is_storm_risk(-2.0) is True
    assert is_storm_risk(-1.9) is False
    assert (
        atmospheric_stability(_meteo(max_li=0.0, min_li=-3.0)).severity is Severity.HARD
    ) == is_storm_risk(-3.0)


def test_daytime_clouds_clear_go():
    assert daytime_clouds(_meteo(daytime_low_cloud=15)).signal is Signal.GO


def test_daytime_clouds_overcast_no_go():
    assert daytime_clouds(_meteo(daytime_low_cloud=80)).signal is Signal.NO_GO


def test_daytime_clouds_mixed_maybe():
    assert daytime_clouds(_meteo(daytime_low_cloud=45)).signal is Signal.MAYBE


def test_upper_level_wind_opposing_direction_no_go():
    v = upper_level_wind(_meteo(wind_850_dir=180, synoptic=15))  # pure S, real speed
    assert v.signal is Signal.NO_GO
    assert "SSE" in v.reason or "180" in v.reason


def test_upper_level_wind_light_sse_drift_not_vetoed():
    # SSE direction alone isn't opposition: n=4 calibration days with 850 hPa
    # SSE drift at 2.8-10.3 kt all fired. The veto needs meaningful speed.
    assert upper_level_wind(_meteo(wind_850_dir=180, synoptic=5)).signal is Signal.GO


def test_upper_level_wind_crossflow_too_strong_no_go():
    v = upper_level_wind(_meteo(wind_850_dir=30, wind_700=30))
    assert v.signal is Signal.NO_GO
    # English reason mentions the 700 hPa crossflow; the German reason carries
    # the "Querströmung" wording. Check each against its own language so neither
    # clause is silently dead (`.reason` is English-only).
    assert "700" in v.reason_en
    assert "Querströmung" in v.reason_de


def test_upper_level_wind_neutral_go():
    assert upper_level_wind(_meteo(wind_850_dir=10, wind_700=8)).signal is Signal.GO


def test_thermal_ignition_detects_ignited_station():
    winds = [
        WindReading("Urfeld", StationRole.SHORE, 12.0, 18.0, 90.0, datetime.now()),
    ]
    assert thermal_ignition(winds).signal is Signal.GO


def test_thermal_ignition_below_threshold_maybe():
    # A reading below the ignition threshold doesn't count as ignited.
    winds = [WindReading("Urfeld", StationRole.SHORE, 5.0, 9.0, None, datetime.now())]
    assert thermal_ignition(winds).signal is Signal.MAYBE


def test_thermal_ignition_no_readings_maybe():
    # Urfeld is flaky — no readings at all is MAYBE (not-yet-ignited), not a veto.
    assert thermal_ignition([]).signal is Signal.MAYBE


# --- Severity tagging on NO_GO verdicts ----------------------------------
# Hard vetos are the rules where a NO_GO physically destroys the thermal or
# makes the lake unsafe. Soft vetos only attenuate; the new aggregator only
# blocks on hard ones, so the right severity here is what stops a single
# soft rule over-vetoing the consensus.


def test_thermik_no_go_is_soft():
    assert thermik(_snapshot(-2.0)).severity is Severity.SOFT


def test_foehn_override_is_hard():
    assert foehn_override(_snapshot(3.0, foehn_delta=12.0)).severity is Severity.HARD


def test_overnight_cooling_no_go_is_soft():
    assert overnight_cooling(_meteo(cloud=97)).severity is Severity.SOFT


def test_solar_radiation_no_go_is_soft():
    assert solar_radiation(_meteo(solar=300)).severity is Severity.SOFT


def test_dew_point_spread_no_go_is_soft():
    assert dew_point_spread(_meteo(dew_spread=2.0)).severity is Severity.SOFT


def test_boundary_layer_no_go_is_soft():
    assert boundary_layer_height(_meteo(blh=300.0)).severity is Severity.SOFT


def test_post_rain_no_go_is_soft():
    assert post_rain_moisture(_meteo(soil=0.40)).severity is Severity.SOFT


def test_atmospheric_stability_capped_is_soft():
    # LI ≥ +6 means atmosphere is too stable / capped — advisory.
    assert atmospheric_stability(_meteo(max_li=11.0, min_li=5.0)).severity is Severity.SOFT


def test_atmospheric_stability_storm_is_hard():
    # LI ≤ −2 means thunderstorm risk — unsafe regardless of thermal viability.
    assert atmospheric_stability(_meteo(max_li=0.0, min_li=-3.0)).severity is Severity.HARD


def test_daytime_clouds_no_go_is_soft():
    assert daytime_clouds(_meteo(daytime_low_cloud=80)).severity is Severity.SOFT


def test_upper_level_wind_opposing_is_hard():
    assert upper_level_wind(_meteo(wind_850_dir=180, synoptic=15)).severity is Severity.HARD


def test_upper_level_wind_crossflow_is_hard():
    assert upper_level_wind(_meteo(wind_850_dir=30, wind_700=30)).severity is Severity.HARD


def test_synoptic_override_is_hard():
    assert synoptic_override(_meteo(synoptic=30)).severity is Severity.HARD


def test_go_verdicts_have_no_severity():
    # Every GO/MAYBE Verdict should default to Severity.NONE so it can't be
    # accidentally counted as a veto.
    assert thermik(_snapshot(5.0)).severity is Severity.NONE
    assert foehn_override(_snapshot(3.0, foehn_delta=0.5)).severity is Severity.NONE
    assert dew_point_spread(_meteo(dew_spread=6.0)).severity is Severity.NONE  # MAYBE band
    assert boundary_layer_height(_meteo(blh=800.0)).severity is Severity.NONE  # MAYBE band


# --- Bilingual reasons ----------------------------------------------------
# Both reason_en and reason_de are emitted at evaluation time so the dashboard
# can pick a language per visitor without post-hoc translation. Guard that no
# rule ships an empty reason on any branch — a regression that's otherwise
# invisible (the CLI/JSON only read the English one).


def test_every_rule_emits_non_empty_reasons_on_all_branches():
    pressure_cases = [
        _snapshot(5.0, foehn_delta=0.5),    # thermik GO, no Föhn
        _snapshot(-2.0, foehn_delta=5.0),   # thermik NO_GO, Föhn veto
    ]
    meteo_cases = [
        _meteo(),  # broadly favourable → GO / MAYBE branches
        _meteo(  # everything vetoes → exercises every NO_GO branch
            cloud=97, solar=400, synoptic=20, dew_spread=2.0, blh=400.0,
            soil=0.40, max_li=11.0, min_li=-3.0,
            daytime_low_cloud=80, wind_850_dir=180, wind_700=30,
        ),
    ]
    winds_cases = [
        [],  # thermal_ignition MAYBE
        [WindReading("Urfeld", StationRole.SHORE, 12.0, 18.0, None, datetime.now())],  # GO
    ]
    for p in pressure_cases:
        for m in meteo_cases:
            for w in winds_cases:
                for v in apply_rules(p, m, w, lake_temp=None):
                    assert v.reason_en.strip(), f"{v.rule} emitted an empty reason_en"
                    assert v.reason_de.strip(), f"{v.rule} emitted an empty reason_de"


# --- air_lake_delta ------------------------------------------------------


def _lake(surface_temp_c: float, age_hours: float = 0.0) -> LakeTempSnapshot:
    from datetime import timedelta
    return LakeTempSnapshot(
        surface_temp_c=surface_temp_c,
        measured_at=datetime.now() - timedelta(hours=age_hours),
        source_station="Urfeld",
    )


def test_air_lake_delta_cold_lake_fires_soft_no_go():
    """Air warmer than lake by more than the threshold → cold-lake veto."""
    v = air_lake_delta(_lake(surface_temp_c=8.0), _meteo(air_temp=20.0))
    assert v.signal is Signal.NO_GO
    assert v.severity is Severity.SOFT
    assert "+12.0" in v.reason_en  # delta displayed with sign


def test_air_lake_delta_warm_lake_is_go():
    """Lake warmer than air → no veto, plain GO."""
    v = air_lake_delta(_lake(surface_temp_c=22.0), _meteo(air_temp=15.0))
    assert v.signal is Signal.GO
    assert v.severity is Severity.NONE
    assert "-7.0" in v.reason_en


def test_air_lake_delta_neutral_band_is_go():
    """Within the warm/cold band, no veto — plain GO with neutral reason."""
    v = air_lake_delta(_lake(surface_temp_c=18.0), _meteo(air_temp=20.0))
    assert v.signal is Signal.GO
    assert v.severity is Severity.NONE


def test_air_lake_delta_missing_lake_temp_is_maybe():
    """Buoy down or no wtemp on latest row → MAYBE, not a guess."""
    v = air_lake_delta(lake_temp=None, meteo=_meteo(air_temp=20.0))
    assert v.signal is Signal.MAYBE
    assert v.severity is Severity.NONE


def test_air_lake_delta_stale_buoy_reading_is_maybe():
    """Reading older than the max-age gate → MAYBE, not a stale veto."""
    v = air_lake_delta(_lake(surface_temp_c=8.0, age_hours=200.0), _meteo(air_temp=20.0))
    assert v.signal is Signal.MAYBE
    assert "200" in v.reason_en or "stale" in v.reason_en.lower() or "old" in v.reason_en.lower()


def test_air_lake_delta_missing_forecast_air_temp_is_maybe():
    """Old record without morning_air_temp_c → MAYBE."""
    v = air_lake_delta(_lake(surface_temp_c=12.0), _meteo(air_temp=None))
    assert v.signal is Signal.MAYBE


def test_air_lake_delta_bilingual_reasons_non_empty():
    """Sanity: every branch emits a non-empty reason in both languages."""
    cases = [
        (_lake(8.0), _meteo(air_temp=20.0)),   # cold
        (_lake(22.0), _meteo(air_temp=15.0)),  # warm
        (_lake(18.0), _meteo(air_temp=20.0)),  # neutral
        (None, _meteo(air_temp=20.0)),         # missing
        (_lake(8.0, age_hours=200.0), _meteo(air_temp=20.0)),  # stale
        (_lake(12.0), _meteo(air_temp=None)),  # missing air
    ]
    for lake, meteo in cases:
        v = air_lake_delta(lake, meteo)
        assert v.reason_en.strip(), f"empty reason_en for {v.signal}"
        assert v.reason_de.strip(), f"empty reason_de for {v.signal}"


def test_apply_rules_includes_air_lake_delta_verdict():
    """apply_rules emits 13 verdicts and air_lake_delta is in the list."""
    verdicts = apply_rules(
        _snapshot(3.0),
        _meteo(air_temp=20.0),
        [],
        lake_temp=_lake(surface_temp_c=8.0),
    )
    rules = {v.rule for v in verdicts}
    assert "air_lake_delta" in rules
    assert len(verdicts) == 13
