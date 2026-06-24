"""Calibration: confusion matrix + per-rule offender stats."""
from pathlib import Path

import csv

from oracle.calibration import (
    actual_verdict,
    actual_verdict_duration,
    actual_verdict_thermal,
    compile_report,
    constant_baselines,
    export_csv,
    format_text_report,
    heidke_skill_score,
    mcnemar,
    mcnemar_keys,
    mean_cost,
    observed_storm,
    parse_months,
    peirce_skill_score,
    reports_by_era,
    reports_by_year,
    rescore_all,
    rescore_record,
    storm_suspected,
)
from oracle.calibration import _months_label, era_of
from oracle.logger import LocalRunStore


def _samples(*triples):
    # (hour, gust_kt, pressure_hpa) → buoy sample rows
    return [
        {"t": f"2022-06-24T{h:02d}:00:00", "gust_kt": g, "pressure_hpa": p}
        for h, g, p in triples
    ]


def test_observed_storm_gust_front_detected():
    # Afternoon gust ≥ 22 kt AND pressure jump ≥ 2 hPa → observed storm.
    m = {"samples": _samples((13, 12, 1010.0), (15, 25.0, 1013.0), (17, 14.0, 1011.0))}
    assert observed_storm(m) is True


def test_observed_storm_windy_but_no_pressure_jump_is_thermal():
    # Strong gust but flat pressure → a thermal, not a gust front.
    m = {"samples": _samples((13, 18, 1012.0), (15, 26.0, 1012.5), (17, 22.0, 1012.2))}
    assert observed_storm(m) is False


def test_observed_storm_none_without_enough_buoy_data():
    assert observed_storm({"samples": _samples((15, 25.0, 1013.0))}) is None  # <3 afternoon pts
    assert observed_storm({}) is None
    assert observed_storm(None) is None


def test_rescore_skips_non_date_blobs(tmp_path: Path):
    # The prod bucket carries `runs/_stats_cache.json`; the day-walk must skip it
    # rather than crash on date.fromisoformat('_stats_cache').
    store = LocalRunStore(tmp_path)
    store.write("2026-06-01", {
        "day": "2026-06-01", "overall": "no_go",
        "inputs": _full_inputs(day="2026-06-01", li_min=1.0),
    })
    (tmp_path / "_stats_cache.json").write_text("{}")  # non-date blob in runs/
    report = rescore_all(store=store)   # must not raise
    assert "2026-06-01" in report["rewritten"] or "2026-06-01" in report["unchanged"]


def test_actual_verdict_thresholds():
    assert actual_verdict(None) is None
    assert actual_verdict(7.9) == "no_go"
    assert actual_verdict(8.0) == "maybe"
    assert actual_verdict(11.9) == "maybe"
    assert actual_verdict(12.0) == "go"
    assert actual_verdict(20.0) == "go"


def _curve(*avgs: float) -> list[dict]:
    """Minimal sample curve — only avg_kt matters for the duration label."""
    return [{"t": f"2026-04-22T1{i}:00:00", "avg_kt": a, "gust_kt": a + 4} for i, a in enumerate(avgs)]


def test_actual_verdict_duration_go_at_11kt():
    # 6 samples (~1 h) of 11 kt average → GO under the lowered session bar,
    # even though none reach the old 12 kt threshold.
    machine = {"samples_above_8kt": 6, "samples": _curve(11, 11, 11, 11, 11, 11)}
    assert actual_verdict_duration(machine) == "go"


def test_actual_verdict_duration_maybe_below_session_bar():
    # An hour of ignition wind (≥ 8 kt) but only a few samples reach 11 kt → MAYBE.
    machine = {"samples_above_8kt": 8, "samples": _curve(9, 9, 9, 11, 11, 9, 8, 8)}
    assert actual_verdict_duration(machine) == "maybe"


def test_actual_verdict_duration_no_go_short_burst():
    machine = {"samples_above_8kt": 2, "samples": _curve(11, 11)}
    assert actual_verdict_duration(machine) == "no_go"


def test_actual_verdict_duration_legacy_fallback_without_samples():
    # Old record with no raw curve falls back to the stored 12 kt count.
    assert actual_verdict_duration({"samples_above_8kt": 9, "samples_above_12kt": 6}) == "go"
    assert actual_verdict_duration({"samples_above_8kt": 9, "samples_above_12kt": 1}) == "maybe"
    assert actual_verdict_duration(None) is None


def _confusion(rows: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Build a full 3×3 confusion dict (forecast → actual → count) from sparse rows."""
    keys = ("go", "maybe", "no_go")
    return {f: {a: rows.get(f, {}).get(a, 0) for a in keys} for f in keys}


def test_skill_scores_zero_for_constant_forecast():
    # Always-GO on a corpus that is the plurality-GO mix the review cites
    # (go 1648 / maybe 1302 / no_go 381). A constant forecast must score 0 skill
    # on both Peirce and Heidke, even though its raw accuracy (49.5%) is high.
    always_go = _confusion({"go": {"go": 1648, "maybe": 1302, "no_go": 381}})
    assert peirce_skill_score(always_go) == 0.0
    assert heidke_skill_score(always_go) == 0.0
    # raw accuracy is the GO base rate — the trap the skill score sidesteps
    assert abs(1648 / 3331 - 0.495) < 0.001


def test_skill_scores_perfect_forecast_is_one():
    perfect = _confusion({"go": {"go": 100}, "maybe": {"maybe": 50}, "no_go": {"no_go": 25}})
    assert abs(peirce_skill_score(perfect) - 1.0) < 1e-9
    assert abs(heidke_skill_score(perfect) - 1.0) < 1e-9


def test_skill_scores_positive_for_real_skill():
    # Mostly-diagonal with some confusion → positive but < 1.
    c = _confusion({
        "go": {"go": 80, "maybe": 20},
        "maybe": {"go": 10, "maybe": 60, "no_go": 10},
        "no_go": {"maybe": 5, "no_go": 40},
    })
    assert 0.0 < peirce_skill_score(c) < 1.0
    assert 0.0 < heidke_skill_score(c) < 1.0


def test_skill_scores_empty_confusion():
    empty = _confusion({})
    assert peirce_skill_score(empty) == 0.0
    assert heidke_skill_score(empty) == 0.0
    assert mean_cost(empty) == 0.0


def test_mean_cost_asymmetry_missed_session_costs_more():
    # A missed session (forecast no_go, actual go) must cost more than the
    # mirror wasted drive (forecast go, actual no_go) under the asymmetric matrix.
    missed = _confusion({"no_go": {"go": 1}})
    wasted = _confusion({"go": {"no_go": 1}})
    assert mean_cost(missed) > mean_cost(wasted)
    assert mean_cost(_confusion({"go": {"go": 1}})) == 0.0  # diagonal is free


def test_constant_baselines_track_marginals():
    c = _confusion({"go": {"go": 30, "maybe": 10}, "maybe": {"maybe": 40, "no_go": 20}})
    # actual marginals: go 30, maybe 50, no_go 20, total 100
    b = constant_baselines(c)
    assert abs(b["go"]["accuracy"] - 0.30) < 1e-9
    assert abs(b["maybe"]["accuracy"] - 0.50) < 1e-9
    assert abs(b["no_go"]["accuracy"] - 0.20) < 1e-9


def _timed_curve(start: str, avgs: list[float], gust_factor: float = 1.6) -> list[dict]:
    """Sample curve starting at `start` (HH:MM), one sample every 10 min."""
    h, m = map(int, start.split(":"))
    out = []
    for i, a in enumerate(avgs):
        total = h * 60 + m + 10 * i
        out.append({
            "t": f"2023-06-15T{total // 60:02d}:{total % 60:02d}:00",
            "avg_kt": a,
            "gust_kt": round(a * gust_factor, 2),
        })
    return out


def test_thermal_label_accepts_clean_midday_thermal():
    machine = {"samples_above_8kt": 6, "samples": _timed_curve("11:00", [12] * 6, gust_factor=1.6)}
    assert actual_verdict_duration(machine) == "go"   # qualifies on wind alone
    assert actual_verdict_thermal(machine) == "go"    # ...and looks thermal


def test_thermal_label_rejects_early_onset_foehn():
    # Same strong sustained wind, but it was already blowing at 06:00 → synoptic/foehn.
    machine = {"samples_above_8kt": 6, "samples": _timed_curve("06:00", [12] * 6, gust_factor=1.4)}
    assert actual_verdict_duration(machine) == "go"
    assert actual_verdict_thermal(machine) == "no_go"


def test_thermal_label_rejects_ragged_frontal_gusts():
    # Mid-day onset but a wild gust factor (3.0) → gust front / frontal squall.
    machine = {"samples_above_8kt": 6, "samples": _timed_curve("12:00", [12] * 6, gust_factor=3.0)}
    assert actual_verdict_duration(machine) == "go"
    assert actual_verdict_thermal(machine) == "no_go"


def test_thermal_label_ignores_lone_early_blip():
    # A single 8 kt blip at 06:00, then calm, then the real session at 11:00.
    curve = (
        _timed_curve("06:00", [8])
        + _timed_curve("06:30", [2, 2, 2])
        + _timed_curve("11:00", [12] * 6, gust_factor=1.6)
    )
    machine = {"samples_above_8kt": 7, "samples": curve}
    assert actual_verdict_thermal(machine) == "go"  # onset is the sustained 11:00 run


def test_thermal_label_passes_through_no_go_and_legacy():
    # NO_GO base stays NO_GO.
    assert actual_verdict_thermal({"samples_above_8kt": 2, "samples": _timed_curve("11:00", [12, 12])}) == "no_go"
    # Legacy record without a raw curve can't be character-judged → keeps duration verdict.
    assert actual_verdict_thermal({"samples_above_8kt": 9, "samples_above_12kt": 6}) == "go"
    assert actual_verdict_thermal(None) is None


def _record(*, day: str, overall: str, peak: float | None, verdicts: list[dict]) -> dict:
    machine = None if peak is None else {"peak_avg_knots": peak}
    return {
        "day": day,
        "overall": overall,
        "verdicts": verdicts,
        "ground_truth": {"machine": machine, "human": None},
    }


def _verdict(rule: str, signal: str, severity: str = "none") -> dict:
    return {"rule": rule, "signal": signal, "severity": severity, "reason": "", "reason_en": "", "reason_de": ""}


def test_compile_report_skips_days_without_ground_truth(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    store.write("2026-04-01", _record(
        day="2026-04-01", overall="go", peak=None,  # no ground truth → skip
        verdicts=[_verdict("thermik", "go")],
    ))
    store.write("2026-04-02", _record(
        day="2026-04-02", overall="go", peak=14.0,
        verdicts=[_verdict("thermik", "go")],
    ))

    report = compile_report(store=store)
    assert report.sample_size == 1
    assert report.days_with_ground_truth == ["2026-04-02"]


def test_confusion_matrix_counts(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    # Three days: forecast/actual = (go, go), (no_go, go), (maybe, no_go)
    store.write("2026-04-01", _record(
        day="2026-04-01", overall="go", peak=14.0,
        verdicts=[_verdict("thermik", "go")],
    ))
    store.write("2026-04-02", _record(
        day="2026-04-02", overall="no_go", peak=15.0,
        verdicts=[_verdict("foehn_override", "no_go", "hard")],
    ))
    store.write("2026-04-03", _record(
        day="2026-04-03", overall="maybe", peak=5.0,
        verdicts=[_verdict("dew_point_spread", "maybe")],
    ))

    report = compile_report(store=store)
    assert report.sample_size == 3
    assert report.confusion["go"]["go"] == 1
    assert report.confusion["no_go"]["go"] == 1
    assert report.confusion["maybe"]["no_go"] == 1


def test_false_positive_veto_attribution(tmp_path: Path):
    """A rule that says NO_GO on a day the lake fires is the worst-offender pattern."""
    store = LocalRunStore(tmp_path)
    # Day 1: post_rain says no_go but actual was 14 kt → false-positive veto.
    # Day 2: post_rain again says no_go on a 13 kt day → another false-positive.
    # Day 3: post_rain says no_go on a 5 kt day → correct (not a false positive).
    for i, peak in enumerate([14.0, 13.0, 5.0], start=1):
        store.write(f"2026-04-0{i}", _record(
            day=f"2026-04-0{i}",
            overall="no_go",
            peak=peak,
            verdicts=[_verdict("post_rain_moisture", "no_go", "soft")],
        ))

    report = compile_report(store=store)
    s = report.rule_stats["post_rain_moisture"]
    assert s.vetos == 3
    assert s.false_positive_vetos == 2
    # `worst_offenders` should rank this rule first.
    worst = report.worst_offenders()
    assert worst and worst[0].rule == "post_rain_moisture"


def test_parse_months_ranges_and_lists():
    assert parse_months("4-10") == frozenset({4, 5, 6, 7, 8, 9, 10})
    assert parse_months("4,5,9") == frozenset({4, 5, 9})
    assert parse_months("6") == frozenset({6})
    # "11-2" is an empty range (lo>hi) → no months → invalid, falls to bad set below.
    for bad in ("0-3", "13", "", "  ", "11-2"):
        try:
            parse_months(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_months_label_contiguous_vs_sparse():
    assert _months_label(frozenset({4, 5, 6, 7, 8, 9, 10})) == "Apr–Oct"
    assert _months_label(frozenset({4, 5, 9})) == "Apr,May,Sep"
    assert _months_label(frozenset({6})) == "Jun"


def test_compile_report_season_filter_drops_winter(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    # Two in-season days (Jun) and two winter days (Dec/Jan), all fired.
    for iso in ("2023-06-10", "2023-06-11", "2022-12-15", "2023-01-20"):
        store.write(iso, _record(
            day=iso, overall="go", peak=14.0, verdicts=[_verdict("thermik", "go")],
        ))

    all_year = compile_report(store=store)  # default months=None here (library default)
    assert all_year.sample_size == 4
    assert all_year.months is None

    in_season = compile_report(store=store, months={4, 5, 6, 7, 8, 9, 10})
    assert in_season.sample_size == 2
    assert sorted(in_season.days_with_ground_truth) == ["2023-06-10", "2023-06-11"]
    assert in_season.months == frozenset({4, 5, 6, 7, 8, 9, 10})


def test_mcnemar_no_discordant_pairs_is_not_significant():
    # Identical forecasts → no discordant days → p = 1.
    r = mcnemar([True, False, True], [True, False, True])
    assert r.b == 0 and r.c == 0
    assert r.p_value == 1.0


def test_mcnemar_strong_improvement_is_significant():
    # 30 days fixed, 2 broken → large, lopsided discordance → significant.
    old = [False] * 30 + [True] * 2 + [True] * 50
    new = [True] * 30 + [False] * 2 + [True] * 50
    r = mcnemar(old, new)
    assert r.b == 30 and r.c == 2
    assert r.net == 28
    assert not r.exact          # 32 discordant ≥ 25 → χ² path
    assert r.p_value < 0.05


def test_mcnemar_balanced_discordance_is_noise():
    # The review's case: many discordant days but ~evenly split → not significant
    # even though the raw n is large. (89 newly-right vs 81 newly-wrong.)
    old = [False] * 89 + [True] * 81
    new = [True] * 89 + [False] * 81
    r = mcnemar(old, new)
    assert r.b == 89 and r.c == 81
    assert r.p_value > 0.05     # net +8 of 170 is noise


def test_mcnemar_small_sample_uses_exact():
    r = mcnemar([False] * 3 + [True] * 2, [True] * 3 + [False] * 2)
    assert r.exact              # 5 discordant < 25 → exact binomial
    assert r.b == 3 and r.c == 2


def test_era_of_boundary():
    assert era_of("2022-11-23") == "ifs"
    assert era_of("2022-11-24") == "icon"
    assert era_of("2026-06-13") == "icon"


def test_reports_by_year_and_era_partition(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    # Two IFS-era days (2021) and two ICON-era days (2023), all in-season, fired.
    for iso in ("2021-06-10", "2021-07-11", "2023-06-10", "2023-07-11"):
        store.write(iso, _record(
            day=iso, overall="go", peak=14.0, verdicts=[_verdict("thermik", "go")],
        ))
    by_year = reports_by_year(store=store, label="peak")
    assert set(by_year) == {2021, 2023}
    assert by_year[2021].sample_size == 2 and by_year[2023].sample_size == 2

    by_era = reports_by_era(store=store, label="peak")
    assert by_era["ifs"].sample_size == 2   # 2021
    assert by_era["icon"].sample_size == 2  # 2023


def test_mcnemar_keys_compares_overall_to_resimulated(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    # Day 1: old wrong (no_go), new right (go), actual go → fixes.
    # Day 2: old right (go), new wrong (no_go), actual go → breaks.
    # Day 3: both right.
    rec1 = _record(day="2023-06-10", overall="no_go", peak=14.0, verdicts=[])
    rec1["overall_resimulated"] = "go"
    rec2 = _record(day="2023-06-11", overall="go", peak=14.0, verdicts=[])
    rec2["overall_resimulated"] = "no_go"
    rec3 = _record(day="2023-06-12", overall="go", peak=14.0, verdicts=[])
    rec3["overall_resimulated"] = "go"
    for r in (rec1, rec2, rec3):
        store.write(r["day"], r)
    result = mcnemar_keys(store=store, label="peak")
    assert result.b == 1 and result.c == 1  # one fixed, one broken


def test_storm_suspected_reads_lifted_index():
    assert storm_suspected({"inputs": _full_inputs(day="2026-06-01", li_min=-3.0)}) is True
    assert storm_suspected({"inputs": _full_inputs(day="2026-06-01", li_min=1.0)}) is False
    # Legacy record with no meteo inputs → can't tell → not a storm (keep the day).
    assert storm_suspected({"day": "2026-06-01"}) is False


def test_compile_report_scores_storm_days_on_thermal_merit(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    # LI-decouple: a storm day is no longer quarantined. The thermal usually
    # still fires before the front, so the day is scored on thermal merit. Here
    # atmospheric_stability stays GREEN (decoupled) and the Urfeld peak of 16 kt
    # is a GO; the day is tallied as a storm day but counted in the matrix.
    store.write("2026-06-01", {
        "day": "2026-06-01",
        "overall": "go",
        "verdicts": [_verdict("atmospheric_stability", "go", "soft")],
        "inputs": _full_inputs(day="2026-06-01", li_min=-3.0),
        "ground_truth": {"machine": {"peak_avg_knots": 16.0}, "human": None},
    })
    # Clear-air day: a genuine thermal that post_rain_moisture over-vetoed.
    store.write("2026-06-02", {
        "day": "2026-06-02",
        "overall": "no_go",
        "verdicts": [_verdict("post_rain_moisture", "no_go", "soft")],
        "inputs": _full_inputs(day="2026-06-02", li_min=1.0),
        "ground_truth": {"machine": {"peak_avg_knots": 14.0}, "human": None},
    })

    report = compile_report(store=store)
    # Storm day is tallied but NOT excluded — both days are scored.
    assert report.storm_days == ["2026-06-01"]
    assert report.sample_size == 2
    assert set(report.days_with_ground_truth) == {"2026-06-01", "2026-06-02"}
    # The storm day's correct GO contributes to the matrix (forecast go, actual go).
    assert report.confusion["go"]["go"] == 1
    # The genuine over-veto on the clear-air day still surfaces.
    assert report.rule_stats["post_rain_moisture"].false_positive_vetos == 1
    assert "scored on thermal merit" in format_text_report(report)


def test_format_text_report_handles_empty(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    report = compile_report(store=store)
    text = format_text_report(report)
    assert "No days with ground truth" in text


# --- rescore -------------------------------------------------------------


def _full_inputs(*, day: str, thermik_delta: float = 5.0, foehn_delta: float = 0.0,
                 li_max: float = 3.0, li_min: float = 1.0,
                 overnight_cloud_pct: float = 10.0,
                 daytime_low_cloud_pct: float = 20.0,
                 dew_spread: float = 8.0,
                 soil: float = 0.20) -> dict:
    """Synthesise an inputs block matching what logger writes."""
    return {
        "pressure": {
            "munich_hpa": 1018.0 + thermik_delta,
            "innsbruck_hpa": 1018.0,
            "bolzano_hpa": 1018.0 + foehn_delta,
            "thermik_delta_hpa": thermik_delta,
            "foehn_delta_hpa": foehn_delta,
            "measured_at": f"{day}T08:00:00",
        },
        "meteo": {
            "day": day,
            "overnight_cloud_cover_pct": overnight_cloud_pct,
            "morning_solar_radiation_wm2": 800.0,
            "synoptic_wind_knots": 5.0,
            "min_dew_point_spread_c": dew_spread,
            "max_boundary_layer_height_m": 1500.0,
            "soil_moisture_m3m3": soil,
            "rained_yesterday": False,
            "yesterday_precipitation_mm": 0.0,
            "max_lifted_index": li_max,
            "min_lifted_index": li_min,
            "max_cape_j_kg": 0.0,
            "max_daytime_low_cloud_pct": daytime_low_cloud_pct,
            "wind_850_direction_at_peak_deg": 30.0,
            "max_wind_700_knots": 10.0,
        },
        "measurements": [],
    }


def test_rescore_record_returns_overall_and_verdicts():
    record = {"inputs": _full_inputs(day="2026-04-22")}
    result = rescore_record(record)
    assert result is not None
    overall, verdicts = result
    # All inputs are favourable → only thermal_ignition (no winds → MAYBE) keeps overall off GO.
    assert overall in ("go", "maybe")
    rules = {v.rule for v in verdicts}
    assert "thermik" in rules and "foehn_override" in rules


def test_rescore_record_skips_incomplete_inputs():
    # Pre-severity log shape with a missing meteo field can't be reconstructed.
    record = {"inputs": {"pressure": {"munich_hpa": 1020}, "meteo": {"day": "2026-01-01"}}}
    assert rescore_record(record) is None


def test_rescore_record_five_soft_vetos_yields_maybe():
    # Five soft vetos downgrade to maybe under the consensus aggregator
    # (2026-06-12 SOFT_VETO_BAR=5, was 2). The record has LI too stable
    # + overnight cloud > threshold + daytime cloud + dew-point spread
    # + soil moisture — five soft vetos. A single soft veto on its
    # own would still produce GO; the aggregator only downgrades when
    # negative signals converge at the 5-veto threshold.
    record = {
        "inputs": _full_inputs(
            day="2026-04-22",
            li_max=11.0,
            overnight_cloud_pct=97.0,
            daytime_low_cloud_pct=80.0,
            dew_spread=2.0,
            soil=0.40,
        ),
    }
    result = rescore_record(record)
    assert result is not None
    overall, _ = result
    assert overall == "maybe"


def test_rescore_all_writes_resimulated_field(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    store.write("2026-04-22", {
        "day": "2026-04-22",
        "overall": "no_go",  # what old aggregator said
        "verdicts": [],
        "inputs": _full_inputs(
            day="2026-04-22",
            li_max=11.0,
            overnight_cloud_pct=97.0,
            daytime_low_cloud_pct=80.0,
            dew_spread=2.0,
            soil=0.40,
        ),
        "ground_truth": {"machine": None, "human": None},
    })

    summary = rescore_all(store=store)
    assert summary["rewritten"] == ["2026-04-22"]
    assert summary["flipped"] == [("2026-04-22", "no_go", "maybe")]

    rewritten = store.read("2026-04-22")
    assert rewritten["overall"] == "no_go"        # historical kept
    assert rewritten["overall_resimulated"] == "maybe"
    assert any(v["rule"] == "thermik" for v in rewritten["verdicts_resimulated"])
    # Ground truth must survive the round-trip.
    assert rewritten["ground_truth"] == {"machine": None, "human": None}


def test_rescore_all_dry_run_does_not_write(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    store.write("2026-04-22", {
        "day": "2026-04-22", "overall": "no_go", "verdicts": [],
        "inputs": _full_inputs(day="2026-04-22", thermik_delta=0.5),
        "ground_truth": {"machine": None, "human": None},
    })
    summary = rescore_all(store=store, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["rewritten"] == []
    rewritten = store.read("2026-04-22")
    assert "overall_resimulated" not in rewritten


def test_export_csv_writes_features_and_ground_truth(tmp_path: Path):
    store = LocalRunStore(tmp_path / "runs")
    # Day 1: full inputs + ground truth → emitted.
    store.write("2026-04-22", {
        "day": "2026-04-22",
        "overall": "go",
        "verdicts": [],
        "inputs": _full_inputs(day="2026-04-22"),
        "ground_truth": {
            "machine": {
                "peak_avg_knots": 14.0,
                "peak_gust_knots": 19.0,
                "first_ignition_at": "2026-04-22T12:00:00",
                "samples_above_8kt": 18,
                "samples_above_12kt": 6,
            },
            "human": None,
        },
    })
    # Day 2: missing ground truth → skipped.
    store.write("2026-04-23", {
        "day": "2026-04-23", "overall": "no_go", "verdicts": [],
        "inputs": _full_inputs(day="2026-04-23"),
        "ground_truth": {"machine": None, "human": None},
    })

    csv_path = tmp_path / "out.csv"
    n = export_csv(csv_path, store=store)
    assert n == 1

    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    row = rows[0]
    assert row["day"] == "2026-04-22"
    assert row["thermik_delta_hpa"] == "5.0"
    assert row["peak_avg_knots"] == "14.0"
    assert row["first_ignition_minute"] == "720"  # 12:00 → 720 minutes since midnight
    assert row["samples_above_8kt"] == "18"
    assert row["samples_above_12kt"] == "6"
    assert row["actual_verdict"] == "go"
    assert row["storm_suspected"] == "False"  # li_min default 1.0 → clear air
    assert row["forecast_overall"] == "go"


def test_format_text_report_shows_offenders(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    store.write("2026-04-01", _record(
        day="2026-04-01", overall="no_go", peak=14.0,
        verdicts=[
            _verdict("solar_radiation", "no_go", "soft"),
            _verdict("thermik", "go"),
        ],
    ))
    text = format_text_report(compile_report(store=store))
    assert "solar_radiation" in text
    assert "FP-veto" in text
    assert "Confusion matrix" in text


# --- replay scoring (the join) ---------------------------------------------
# Replay records carry verdicts + inputs; the matching main record (for the
# historical backfill, a stub) carries the buoy ground truth. `replayed=True`
# joins the two.


def _seed_replay_pair(store: LocalRunStore, iso: str, *, peak: float = 14.0,
                      overall: str = "no_go") -> None:
    store.write(iso, {
        "day": iso,
        "ground_truth": {"machine": {"peak_avg_knots": peak}, "human": None},
    })
    store.write_replay(iso, {
        "day": iso,
        "overall": overall,
        "verdicts": [_verdict("thermik", overall)],
        "inputs": _full_inputs(day=iso),
        "ground_truth": {"machine": None, "human": None},
        "replay_day": iso,
        "replay_source": "historical-forecast",
    })


def test_compile_report_replayed_joins_verdicts_with_stub_ground_truth(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    _seed_replay_pair(store, "2021-06-15", peak=14.0, overall="no_go")

    report = compile_report(store=store, replayed=True)
    assert report.replayed is True
    assert report.sample_size == 1
    # Replay said no_go, the lake actually fired → counted as the miss it is.
    assert report.confusion["no_go"]["go"] == 1
    assert report.rule_stats["thermik"].false_positive_vetos == 1

    # The default (live) view must not see replay records: the stub has no
    # verdicts, so the sample stays empty.
    assert compile_report(store=store).sample_size == 0


def test_compile_report_replayed_skips_replay_without_main_record(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    store.write_replay("2021-06-15", {
        "day": "2021-06-15", "overall": "go",
        "verdicts": [_verdict("thermik", "go")],
        "ground_truth": {"machine": None, "human": None},
    })
    report = compile_report(store=store, replayed=True)
    assert report.sample_size == 0


def test_rescore_all_replayed_writes_to_replay_record_only(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    _seed_replay_pair(store, "2021-06-15")

    summary = rescore_all(store=store, replayed=True)
    assert summary["rewritten"] == ["2021-06-15"]

    replay = store.read_replay("2021-06-15")
    assert "overall_resimulated" in replay
    assert "verdicts_resimulated" in replay
    # The main (stub) record is untouched.
    assert "overall_resimulated" not in store.read("2021-06-15")

    # The rescored replay layer is scoreable: replayed + resimulated combine.
    report = compile_report(store=store, replayed=True, resimulated=True)
    assert report.sample_size == 1
    assert report.resimulated is True and report.replayed is True


def test_export_csv_replayed_emits_joined_rows(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    _seed_replay_pair(store, "2021-06-15", peak=13.5)
    out = tmp_path / "replay.csv"

    n = export_csv(out, store=store, replayed=True)
    assert n == 1
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["day"] == "2021-06-15"
    assert rows[0]["peak_avg_knots"] == "13.5"
    assert rows[0]["forecast_overall"] == "no_go"


# --- Phase A: training-dataset export ---------------------------------------
# The ML spike needs the three target scales (peak / duration / thermal) so the
# notebook can pick the right one without re-running the buoy gates, plus
# month / year / era metadata for the season filter, year-blocked CV, and the
# era-aware sanity check (Hollmann 2023 / Roberts 2017: random k-fold on
# autocorrelated daily weather is the wrong way to validate this corpus).
# See docs/findings/ml-research-2026-06-13.md §3.2 + §3.4.

def _phase_a_record(day: str, *, peak: float, samples_above_8kt: int | None,
                    samples: list[dict] | None,
                    samples_above_12kt: int | None = None) -> dict:
    """Build a record with the shape `_row_for` expects.

    `samples_above_8kt=None` simulates a legacy record that has only the
    peak reading (no buoy day-curve, no per-bucket count) — used to verify
    the duration/thermal target columns fall back cleanly to empty cells
    instead of crashing the export.

    When `samples_above_8kt` is set but `samples` is not, the legacy
    fallback in `actual_verdict_duration` looks at `samples_above_12kt`,
    so the helper defaults that to a matching value when the caller
    hasn't supplied a real curve.
    """
    machine: dict = {"peak_avg_knots": peak}
    if samples_above_8kt is not None:
        machine["samples_above_8kt"] = samples_above_8kt
        # Default the 12-kt count to the same value so the legacy fallback
        # path yields a real verdict; callers can override or leave None.
        machine["samples_above_12kt"] = (
            samples_above_12kt if samples_above_12kt is not None else samples_above_8kt
        )
    elif samples_above_12kt is not None:
        # Edge case: only the 12-kt count survives (rare). Keep it.
        machine["samples_above_12kt"] = samples_above_12kt
    if samples is not None:
        machine["samples"] = samples
    return {
        "day": day,
        "overall": "go",
        "verdicts": [],
        "inputs": _full_inputs(day=day),
        "ground_truth": {"machine": machine, "human": None},
    }


def test_export_csv_includes_three_target_scales(tmp_path: Path):
    """All three ground-truth label scales are emitted as separate columns.

    The peak label only needs `peak_avg_knots`; duration/thermal additionally
    need the buoy day-curve (or the legacy `samples_above_8kt` fallback).
    Both columns must be present in the header even when empty, so the
    ML notebook can address them by name without conditional loading.
    """
    store = LocalRunStore(tmp_path)
    store.write("2026-04-22", _phase_a_record(
        "2026-04-22", peak=14.0, samples_above_8kt=18, samples=None,
    ))
    out = tmp_path / "out.csv"
    export_csv(out, store=store)
    with out.open() as f:
        rows = list(csv.DictReader(f))
    row = rows[0]
    # Peak label is always populated when the row exists at all (peak is
    # the row's existence gate in `_row_for`). Legacy fallback (no raw
    # curve) yields "go" because samples_above_8kt=18 ≥ the 6-sample
    # ignition bar.
    assert row["actual_verdict"] == "go"
    assert row["actual_verdict_duration"] == "go"
    assert row["actual_verdict_thermal"] == "go"  # legacy record → keep duration verdict

    # Legacy record without samples_above_8kt → duration/thermal targets
    # are empty (the cells exist but the value is the empty string from
    # csv.DictWriter's None handling).
    store.write("2022-08-15", _phase_a_record(
        "2022-08-15", peak=5.0, samples_above_8kt=None, samples=None,
    ))
    export_csv(out, store=store)
    with out.open() as f:
        rows = list(csv.DictReader(f))
    legacy = next(r for r in rows if r["day"] == "2022-08-15")
    assert legacy["actual_verdict"] == "no_go"
    assert legacy["actual_verdict_duration"] == ""
    assert legacy["actual_verdict_thermal"] == ""


def test_export_csv_thermal_label_rejects_foehn_onset(tmp_path: Path):
    """Thermal label downgrades a day that fired (duration=go) but ignited
    before the daytime window — a foehn/foehn-flank signature, not a
    thermal session. Confirms the three target scales actually differ
    on a real case, not just coexist as duplicate columns.
    """
    # Wind already blowing at 06:00 — same strong sustained wind as a
    # successful thermal, but pre-ignition onset → foehn, not thermal.
    samples = _timed_curve("06:00", [12] * 6, gust_factor=1.4)
    store = LocalRunStore(tmp_path)
    store.write("2023-06-15", _phase_a_record(
        "2023-06-15", peak=14.0, samples_above_8kt=6, samples=samples,
    ))
    out = tmp_path / "out.csv"
    export_csv(out, store=store)
    with out.open() as f:
        rows = list(csv.DictReader(f))
    row = rows[0]
    assert row["actual_verdict"] == "go"           # peak alone
    assert row["actual_verdict_duration"] == "go"  # ~1 h sustained
    assert row["actual_verdict_thermal"] == "no_go"  # onset gate rejected it


def test_export_csv_emits_month_year_era_metadata(tmp_path: Path):
    """Date metadata columns let the ML notebook apply the season filter
    and the year-blocked CV split without re-parsing `day`.

    `era` is "ifs" before config.ICON_ERA_START (2022-11-24) and "icon"
    from it — mirrors `era_of()`. Verified explicitly for both boundaries.
    """
    store = LocalRunStore(tmp_path)
    for iso in ("2021-07-04", "2022-11-23", "2022-11-24", "2026-04-22"):
        store.write(iso, _phase_a_record(iso, peak=12.0, samples_above_8kt=6, samples=None))
    out = tmp_path / "out.csv"
    export_csv(out, store=store)
    with out.open() as f:
        rows = list(csv.DictReader(f))
    by_day = {r["day"]: r for r in rows}
    assert by_day["2021-07-04"]["month"] == "7" and by_day["2021-07-04"]["year"] == "2021"
    assert by_day["2021-07-04"]["era"] == "ifs"
    # Day before the ICON flip — still IFS.
    assert by_day["2022-11-23"]["era"] == "ifs"
    # ICON-ERA_START itself — first ICON day (boundary is inclusive per `era_of`).
    assert by_day["2022-11-24"]["era"] == "icon"
    assert by_day["2026-04-22"]["month"] == "4" and by_day["2026-04-22"]["era"] == "icon"


def test_export_csv_months_filter_excludes_off_season(tmp_path: Path):
    """`export_csv(months=...)` filters at the iteration layer; the new
    metadata columns don't change that — verified by row count and by
    the months on the kept rows.
    """
    store = LocalRunStore(tmp_path)
    for iso in ("2023-04-15", "2023-06-15", "2023-10-31", "2023-12-15", "2024-02-15"):
        store.write(iso, _phase_a_record(iso, peak=12.0, samples_above_8kt=6, samples=None))
    out = tmp_path / "out.csv"
    n = export_csv(out, store=store, months=frozenset({4, 5, 6, 7, 8, 9, 10}))
    assert n == 3
    with out.open() as f:
        rows = list(csv.DictReader(f))
    months = sorted(int(r["month"]) for r in rows)
    assert months == [4, 6, 10]
    # No off-season row leaked through the filter.
    days = {r["day"] for r in rows}
    assert "2023-12-15" not in days and "2024-02-15" not in days
