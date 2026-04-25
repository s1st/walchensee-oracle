"""Calibration: confusion matrix + per-rule offender stats."""
from pathlib import Path

from oracle.calibration import actual_verdict, compile_report, format_text_report
from oracle.logger import LocalRunStore


def test_actual_verdict_thresholds():
    assert actual_verdict(None) is None
    assert actual_verdict(7.9) == "no_go"
    assert actual_verdict(8.0) == "maybe"
    assert actual_verdict(11.9) == "maybe"
    assert actual_verdict(12.0) == "go"
    assert actual_verdict(20.0) == "go"


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


def test_format_text_report_handles_empty(tmp_path: Path):
    store = LocalRunStore(tmp_path)
    report = compile_report(store=store)
    text = format_text_report(report)
    assert "No days with ground truth" in text


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
