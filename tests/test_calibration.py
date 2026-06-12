"""Calibration: confusion matrix + per-rule offender stats."""
from pathlib import Path

import csv

from oracle.calibration import (
    actual_verdict,
    actual_verdict_duration,
    compile_report,
    export_csv,
    format_text_report,
    rescore_all,
    rescore_record,
    storm_suspected,
)
from oracle.logger import LocalRunStore


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


def test_storm_suspected_reads_lifted_index():
    assert storm_suspected({"inputs": _full_inputs(day="2026-06-01", li_min=-3.0)}) is True
    assert storm_suspected({"inputs": _full_inputs(day="2026-06-01", li_min=1.0)}) is False
    # Legacy record with no meteo inputs → can't tell → not a storm (keep the day).
    assert storm_suspected({"day": "2026-06-01"}) is False


def test_compile_report_quarantines_storm_days(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    # Storm day: forecast NO_GO via the atmospheric_stability HARD veto, but the
    # Urfeld gust front peaked at 16 kt, which the peak label calls GO. This must
    # be quarantined — otherwise atmospheric_stability is charged a false-positive
    # veto for correctly calling the storm.
    store.write("2026-06-01", {
        "day": "2026-06-01",
        "overall": "no_go",
        "verdicts": [_verdict("atmospheric_stability", "no_go", "hard")],
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
    assert report.quarantined_days == ["2026-06-01"]
    assert report.sample_size == 1
    assert report.days_with_ground_truth == ["2026-06-02"]
    # Storm rule was NOT charged a false-positive veto…
    assert "atmospheric_stability" not in report.rule_stats
    # …while the genuine over-veto on the clear-air day still surfaces.
    assert report.rule_stats["post_rain_moisture"].false_positive_vetos == 1
    assert "quarantined" in format_text_report(report)


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
