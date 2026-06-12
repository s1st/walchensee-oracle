"""Calibrate the rule thresholds against logged ground truth.

Reads every record from a `RunStore` that has both forecast verdicts and a
populated `ground_truth.machine` block (Urfeld backfill written by the 21:00
job) — the green/yellow/red bars in the dashboard's 'Realität (Session ≥ 1 h)'
row.

For each day it categorises the actual outcome onto the go/maybe/no_go scale,
then computes:

- Overall confusion matrix (forecast × actual).
- Per-rule false-positive vetos: rule said NO_GO but the day actually fired
  (peak ≥ 8 kt). These are the rules whose thresholds are over-aggressive.
- Per-rule false-negative greens: rule said GO but the day didn't fire.
  Less critical — we mostly care about over-vetoing.

Deliberately doesn't auto-tune thresholds; surfaces evidence for a human.
"""
from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from oracle import config
from oracle.engine import aggregate, apply_rules
from oracle.knowledge.rules import SIGNAL_ORDER, Signal, Verdict, is_storm_risk
from oracle.logger import RunStore, default_store, verdict_to_dict
from oracle.pillars.measurements import LakeTempSnapshot, WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureSnapshot

# Peak-of-day scale. Used by the calibrate CLI's "peak" label mode; the
# dashboard's strip uses the duration-aware variant below since 2026-05.
_ACTUAL_GO_KT = 12.0      # session-worthy
_ACTUAL_MAYBE_KT = 8.0    # ignited but marginal

# Duration label — Urfeld samples are ~10 min apart, so 6 samples ≈ 1 hour.
# A "GO" day needs ~an hour of session-strength average wind; "MAYBE" needs
# ~an hour of ignition-strength wind. Anything shorter is NO_GO regardless of peak.
_DURATION_GO_KT = 11.0          # was 12.0; lowered 2026-05 after n=34 Urfeld days.
                                # Walchi thermals run long rideable sessions at
                                # 10–11 kt avg with 16–18 kt gusts but rarely
                                # sustain a 12 kt *average* — the 12 kt bar
                                # labelled genuine sessions MAYBE (e.g. 05-28/29).
_DURATION_GO_SAMPLES = 6        # ~1 hour at Urfeld's ~10-min cadence
_DURATION_MAYBE_SAMPLES_8KT = 6


def actual_verdict(peak_avg_kt: float | None) -> str | None:
    """Categorise an Urfeld-peak ground-truth value onto the go/maybe/no_go scale.

    Peak-only label — feeds the `--label peak` mode of `oracle calibrate`.
    The dashboard uses `actual_verdict_duration` instead.
    Returns the Signal `.value` string so the result is template- and JSON-safe.
    """
    if peak_avg_kt is None:
        return None
    if peak_avg_kt >= _ACTUAL_GO_KT:
        return Signal.GO.value
    if peak_avg_kt >= _ACTUAL_MAYBE_KT:
        return Signal.MAYBE.value
    return Signal.NO_GO.value


def actual_verdict_duration(machine: dict | None) -> str | None:
    """Duration-aware label: needs sustained wind, not just a transient peak.

    A 20-minute gust to 14 kt that dies again stays NO_GO because the average
    never held session strength for an hour.

    GO needs ≥ _DURATION_GO_SAMPLES samples (~1 h) where the 10-min average was
    ≥ _DURATION_GO_KT (11 kt). That count is recomputed live from the stored raw
    `samples` so the threshold can be tuned without a re-backfill — the logger's
    `samples_above_12kt` field keeps its original 12 kt meaning as a frozen
    historical metric (see CLAUDE.md: stored duration metrics aren't rewritten).
    MAYBE still keys off the stored ≥ 8 kt ignition count.
    """
    if not machine:
        return None
    above_8 = machine.get("samples_above_8kt")
    if above_8 is None:
        return None
    samples = machine.get("samples")
    if samples is not None:
        above_go = sum(1 for s in samples if (s.get("avg_kt") or 0) >= _DURATION_GO_KT)
    else:
        # Legacy record without the raw curve: fall back to the stored 12 kt
        # count (one notch stricter than the live 11 kt rule, but the best we have).
        legacy = machine.get("samples_above_12kt")
        if legacy is None:
            return None
        above_go = legacy
    if above_go >= _DURATION_GO_SAMPLES:
        return Signal.GO.value
    if above_8 >= _DURATION_MAYBE_SAMPLES_8KT:
        return Signal.MAYBE.value
    return Signal.NO_GO.value


@dataclass
class RuleStats:
    rule: str
    vetos: int = 0                      # times rule emitted NO_GO
    false_positive_vetos: int = 0       # NO_GO but actual ≥ 8 kt
    greens: int = 0                     # times rule emitted GO
    false_negative_greens: int = 0      # GO but actual < 8 kt


@dataclass
class Report:
    sample_size: int
    days_with_ground_truth: list[str]
    confusion: dict[str, dict[str, int]]    # forecast → actual → count
    rule_stats: dict[str, RuleStats] = field(default_factory=dict)
    label_mode: str = "peak"
    resimulated: bool = False
    quarantined_days: list[str] = field(default_factory=list)  # storm-suspected, excluded

    @property
    def overall_accuracy(self) -> float:
        """Diagonal sum / total. Approximate — same-bucket only."""
        if self.sample_size == 0:
            return 0.0
        hits = sum(self.confusion.get(s.value, {}).get(s.value, 0) for s in SIGNAL_ORDER)
        return hits / self.sample_size

    def worst_offenders(self, n: int = 5) -> list[RuleStats]:
        """Top-N rules by false-positive vetos — the ones killing real session days."""
        return sorted(
            (s for s in self.rule_stats.values() if s.false_positive_vetos > 0),
            key=lambda s: (-s.false_positive_vetos, s.rule),
        )[:n]


def _empty_confusion() -> dict[str, dict[str, int]]:
    return {f.value: {a.value: 0 for a in SIGNAL_ORDER} for f in SIGNAL_ORDER}


def _peak_from(record: dict) -> float | None:
    machine = (record.get("ground_truth") or {}).get("machine") or {}
    return machine.get("peak_avg_knots")


def _machine_from(record: dict) -> dict | None:
    return (record.get("ground_truth") or {}).get("machine") or None


def storm_suspected(record: dict) -> bool:
    """Forecast-time thunderstorm flag for a stored record, read from the lifted
    index in its meteo inputs.

    Drives both the calibration quarantine in `compile_report` and the
    dashboard's yellow storm border. Storm days are excluded from the confusion
    matrix and per-rule offender stats: the high wind a gust front delivers is
    not a thermal session, so counting it would punish the very rules that
    correctly vetoed the storm (e.g. `atmospheric_stability`). Defensive against
    legacy records written before the lifted-index field existed.
    """
    meteo = (record.get("inputs") or {}).get("meteo") or {}
    li = meteo.get("min_lifted_index")
    return li is not None and is_storm_risk(float(li))


def _label_record(record: dict, mode: str) -> str | None:
    """Dispatch the configured labeller against one record's machine block."""
    if mode == "duration":
        return actual_verdict_duration(_machine_from(record))
    return actual_verdict(_peak_from(record))


def _ignition_minute_of_day(iso_ts: str | None) -> int | None:
    """ISO timestamp → minutes since local midnight. Robust to naive/aware."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    return dt.hour * 60 + dt.minute


def _iter_window_days(
    store: RunStore, since: date | None, until: date | None
) -> Iterator[str]:
    """Yield logged ISO days that fall within [since, until] (inclusive ends).

    Single source of the date-window filter shared by `rescore_all`,
    `compile_report` and `export_csv`.
    """
    for iso in store.list_days():
        d = date.fromisoformat(iso)
        if since and d < since:
            continue
        if until and d > until:
            continue
        yield iso


# --- record re-scoring ----------------------------------------------------
# Re-run the rule layer against a record's stored `inputs` block under the
# *current* aggregator. Used to surface "what would the new severity-tiered
# aggregator have said" on historical records — without re-fetching the
# upstream APIs, which would return today's data anyway.


def rescore_record(record: dict) -> tuple[str, list[Verdict]] | None:
    """Re-run the rule layer against a record's stored inputs.

    Returns (overall, verdicts) under the current aggregator, or None if the
    record's inputs are too incomplete to reconstruct (older log schema —
    common for records written before later meteo fields shipped).
    """
    inputs = record.get("inputs") or {}
    p = inputs.get("pressure")
    m = inputs.get("meteo")
    winds_raw = inputs.get("measurements") or []
    lake_temp_raw = inputs.get("lake_temp")
    if not p or not m:
        return None
    try:
        snapshot = PressureSnapshot.from_dict(p)
        meteo_snap = MeteoSnapshot.from_dict(m)
        winds = [WindReading.from_dict(w) for w in winds_raw]
        lake_temp = (
            LakeTempSnapshot.from_dict(lake_temp_raw) if lake_temp_raw else None
        )
    except (KeyError, ValueError, TypeError):
        return None

    verdicts = apply_rules(snapshot, meteo_snap, winds, lake_temp)
    return aggregate(verdicts).value, verdicts


def rescore_all(
    store: RunStore | None = None,
    since: date | None = None,
    until: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Walk every logged record, re-score it, and persist the result.

    Adds two fields to each successfully re-scored record without touching
    the historical `overall` / `verdicts` (which stay as evidence of what
    the aggregator-of-the-day actually said):

      - `overall_resimulated`
      - `verdicts_resimulated`

    Returns a small report: counts + list of skipped days.

    Since 2026-06-12 the bucket also contains ~3,600 historical buoy
    stub records (2016-2026, no `inputs` block). `rescore_record` returns
    None for those, so they land in `skipped` — no harm, but a no-arg
    rescore is slow because every stub is read from GCS and discarded.
    Pass `since=date(2026, 4, 22)` to rescore only the project's days.
    """
    store = store or default_store()
    rewritten: list[str] = []
    unchanged: list[str] = []
    skipped: list[str] = []
    flipped: list[tuple[str, str, str]] = []  # (iso, old_overall, new_overall)

    for iso in _iter_window_days(store, since, until):
        record = store.read(iso)
        if record is None:
            continue
        result = rescore_record(record)
        if result is None:
            skipped.append(iso)
            continue
        new_overall, new_verdicts = result
        record["overall_resimulated"] = new_overall
        record["verdicts_resimulated"] = [verdict_to_dict(v) for v in new_verdicts]
        old_overall = record.get("overall")
        if old_overall != new_overall:
            flipped.append((iso, old_overall, new_overall))
        if not dry_run:
            store.write(iso, record)
            rewritten.append(iso)
        else:
            unchanged.append(iso)

    return {
        "rewritten": rewritten,
        "skipped": skipped,
        "flipped": flipped,
        "dry_run": dry_run,
    }


def compile_report(
    store: RunStore | None = None,
    since: date | None = None,
    until: date | None = None,
    label: str = "peak",
    resimulated: bool = False,
) -> Report:
    """Walk every logged record and aggregate forecast-vs-actual metrics.

    Since 2026-06-12 the bucket also contains ~3,600 historical buoy
    stub records (2016-2026, no `overall` block). They contribute to
    the `actual` (ground truth) side of the confusion matrix — useful
    for hypothesis testing at scale — but the per-record forecast lookup
    is None, so they don't pollute the verdict scoring. Pass
    `since=date(2026, 4, 22)` to restrict to the project's own days when
    you only want the project's own forecast accuracy.

    `label` picks the ground-truth scale:
      - "peak" (default): bucket by `peak_avg_knots` (≥12 GO, ≥8 MAYBE).
      - "duration": bucket by sustained samples (≥6 above 12 kt GO,
        ≥6 above 8 kt MAYBE) — requires roughly an hour of session/ignition wind.

    `resimulated`: when True, read `overall_resimulated` / `verdicts_resimulated`
    instead of the historical `overall` / `verdicts`. Use this to evaluate the
    *current* rule layer against the same ground truth — the historical fields
    reflect whatever thresholds were in force when each record was written, so
    they go stale immediately after any threshold tune. Records lacking the
    resimulated fields are skipped (run `oracle rescore` to populate them).
    """
    overall_key = "overall_resimulated" if resimulated else "overall"
    verdicts_key = "verdicts_resimulated" if resimulated else "verdicts"

    store = store or default_store()
    confusion = _empty_confusion()
    rule_stats: dict[str, RuleStats] = {}
    sample_days: list[str] = []
    quarantined: list[str] = []

    for iso in _iter_window_days(store, since, until):
        record = store.read(iso)
        if record is None:
            continue
        actual = _label_record(record, label)
        if actual is None:
            continue
        forecast = record.get(overall_key)
        if forecast not in confusion:
            # Unknown overall — skip rather than crash on legacy data, or on
            # records that haven't been rescored yet when --resimulated is set.
            continue
        if storm_suspected(record):
            # Gust-front wind isn't a thermal session; learning from it would
            # punish the rules that correctly vetoed the storm. Quarantine it.
            quarantined.append(iso)
            continue
        confusion[forecast][actual] += 1
        sample_days.append(iso)

        for v in record.get(verdicts_key, []):
            stats = rule_stats.setdefault(v["rule"], RuleStats(rule=v["rule"]))
            if v["signal"] == Signal.NO_GO:
                stats.vetos += 1
                if actual in (Signal.GO, Signal.MAYBE):
                    stats.false_positive_vetos += 1
            elif v["signal"] == Signal.GO:
                stats.greens += 1
                if actual == Signal.NO_GO:
                    stats.false_negative_greens += 1

    return Report(
        sample_size=len(sample_days),
        days_with_ground_truth=sample_days,
        confusion=confusion,
        rule_stats=rule_stats,
        label_mode=label,
        resimulated=resimulated,
        quarantined_days=quarantined,
    )


def format_text_report(report: Report, rule_filter: str | None = None) -> str:
    """Plain-text summary suitable for `oracle calibrate` stdout."""
    if report.sample_size == 0:
        msg = (
            "No days with ground truth yet. Run `oracle backfill` to merge "
            "Urfeld peak data into the day's forecast log first."
        )
        if report.quarantined_days:
            msg += (
                f" ({len(report.quarantined_days)} storm-suspected day(s) "
                "quarantined — see `oracle backfill`.)"
            )
        return msg

    label_desc = {
        "peak": "peak avg ≥12 kt → GO, ≥8 kt → MAYBE",
        "duration": "≥6 samples (~1h) above 12 kt → GO, above 8 kt → MAYBE",
    }.get(report.label_mode, report.label_mode)

    view = "resimulated (current rule layer)" if report.resimulated else "historical (verdicts as written)"
    lines: list[str] = []
    lines.append(
        f"Calibration sample: {report.sample_size} days with ground truth "
        f"(label = {report.label_mode}: {label_desc}; view = {view})."
    )
    if report.quarantined_days:
        lines.append(
            f"  ⚡ {len(report.quarantined_days)} storm-suspected day(s) quarantined "
            f"(LI ≤ {config.MIN_LIFTED_INDEX:.0f}) — excluded from the matrix and "
            "offender stats; gust-front wind isn't a thermal session."
        )
    if report.sample_size < 14:
        lines.append(
            "  ⚠  small sample — interpret with caution. Wait for "
            "≥ 14 days before tuning thresholds from this report."
        )
    lines.append("")
    lines.append(f"Overall accuracy (same-bucket): {report.overall_accuracy:.0%}")
    lines.append("")
    lines.append("Confusion matrix (rows=forecast, cols=actual):")
    headers = "  ".join(f"{s.value:>5s}" for s in SIGNAL_ORDER)
    lines.append(f"  {'':>10s}  {headers}")
    for f in SIGNAL_ORDER:
        row = report.confusion[f.value]
        cells = "  ".join(f"{row[s.value]:>5d}" for s in SIGNAL_ORDER)
        lines.append(f"  {f.value:>10s}  {cells}")
    lines.append("")

    rule_items = sorted(
        report.rule_stats.values(),
        key=lambda s: (-s.false_positive_vetos, -s.vetos, s.rule),
    )
    if rule_filter:
        rule_items = [s for s in rule_items if s.rule == rule_filter]

    if not rule_items:
        lines.append(f"No data for rule {rule_filter!r}." if rule_filter else "No rule data.")
        return "\n".join(lines)

    lines.append("Per-rule offenders (sorted by false-positive vetos):")
    lines.append(f"  {'rule':<25s}  {'vetos':>5s}  {'FP-veto':>7s}  {'greens':>6s}  {'FN-green':>8s}")
    for s in rule_items:
        lines.append(
            f"  {s.rule:<25s}  {s.vetos:>5d}  {s.false_positive_vetos:>7d}  "
            f"{s.greens:>6d}  {s.false_negative_greens:>8d}"
        )
    lines.append("")
    lines.append(
        "FP-veto = rule said NO_GO but actual label was GO/MAYBE (rule killed a real session). "
        "FN-green = rule said GO but actual label was NO_GO."
    )
    return "\n".join(lines)


# --- ML-friendly CSV export ----------------------------------------------
# Flat one-row-per-day projection of every record that has both reconstructable
# inputs and Urfeld ground truth. Intended for offline notebooks: load with
# pandas, fit a shallow tree against `actual_peak_avg_knots` (regression) or
# `actual_verdict` (classification), inspect feature importance.

_CSV_COLUMNS = [
    "day",
    # pressure
    "munich_hpa", "innsbruck_hpa", "bolzano_hpa",
    "thermik_delta_hpa", "foehn_delta_hpa",
    # meteo
    "overnight_cloud_cover_pct", "morning_solar_radiation_wm2",
    "synoptic_wind_knots", "min_dew_point_spread_c",
    "max_boundary_layer_height_m", "soil_moisture_m3m3",
    "rained_yesterday", "yesterday_precipitation_mm",
    "max_lifted_index", "min_lifted_index", "max_cape_j_kg",
    "max_daytime_low_cloud_pct", "wind_850_direction_at_peak_deg",
    "max_wind_700_knots",
    # ground truth (Urfeld peak)
    "peak_avg_knots", "peak_gust_knots",
    "first_ignition_minute", "samples_above_8kt", "samples_above_12kt",
    "actual_verdict",
    # storm flag: True = gust-front-contaminated label, quarantined from calibration.
    # Kept in the export (not dropped) so the ML notebook can mask or model it.
    "storm_suspected",
    # what the rule layer said (for benchmarking ML against the heuristic)
    "forecast_overall", "forecast_overall_resimulated",
]


def _row_for(record: dict) -> dict | None:
    """Project one record into a flat CSV row, or None if not usable."""
    inputs = record.get("inputs") or {}
    p = inputs.get("pressure") or {}
    m = inputs.get("meteo") or {}
    if not p or not m:
        return None
    machine = _machine_from(record) or {}
    peak = machine.get("peak_avg_knots")
    if peak is None:
        return None
    return {
        "day": record.get("day"),
        "munich_hpa": p.get("munich_hpa"),
        "innsbruck_hpa": p.get("innsbruck_hpa"),
        "bolzano_hpa": p.get("bolzano_hpa"),
        "thermik_delta_hpa": p.get("thermik_delta_hpa"),
        "foehn_delta_hpa": p.get("foehn_delta_hpa"),
        "overnight_cloud_cover_pct": m.get("overnight_cloud_cover_pct"),
        "morning_solar_radiation_wm2": m.get("morning_solar_radiation_wm2"),
        "synoptic_wind_knots": m.get("synoptic_wind_knots"),
        "min_dew_point_spread_c": m.get("min_dew_point_spread_c"),
        "max_boundary_layer_height_m": m.get("max_boundary_layer_height_m"),
        "soil_moisture_m3m3": m.get("soil_moisture_m3m3"),
        "rained_yesterday": m.get("rained_yesterday"),
        "yesterday_precipitation_mm": m.get("yesterday_precipitation_mm"),
        "max_lifted_index": m.get("max_lifted_index"),
        "min_lifted_index": m.get("min_lifted_index"),
        "max_cape_j_kg": m.get("max_cape_j_kg"),
        "max_daytime_low_cloud_pct": m.get("max_daytime_low_cloud_pct"),
        "wind_850_direction_at_peak_deg": m.get("wind_850_direction_at_peak_deg"),
        "max_wind_700_knots": m.get("max_wind_700_knots"),
        "peak_avg_knots": peak,
        "peak_gust_knots": machine.get("peak_gust_knots"),
        "first_ignition_minute": _ignition_minute_of_day(machine.get("first_ignition_at")),
        "samples_above_8kt": machine.get("samples_above_8kt"),
        "samples_above_12kt": machine.get("samples_above_12kt"),
        "actual_verdict": actual_verdict(peak),
        "storm_suspected": storm_suspected(record),
        "forecast_overall": record.get("overall"),
        "forecast_overall_resimulated": record.get("overall_resimulated"),
    }


def export_csv(
    path: Path | str,
    store: RunStore | None = None,
    since: date | None = None,
    until: date | None = None,
) -> int:
    """Write every ground-truthed record to `path` as a flat CSV. Returns row count."""
    store = store or default_store()
    rows: list[dict] = []
    for iso in _iter_window_days(store, since, until):
        record = store.read(iso)
        if record is None:
            continue
        row = _row_for(record)
        if row is not None:
            rows.append(row)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
