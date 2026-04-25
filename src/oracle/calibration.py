"""Calibrate the rule thresholds against logged ground truth.

Reads every record from a `RunStore` that has both forecast verdicts and a
populated `ground_truth.machine.peak_avg_knots` (the Urfeld buoy peak that
the 21:00 backfill job writes — the green/yellow/red bars in the dashboard's
'Actual (Urfeld peak)' row).

For each day it categorises the actual outcome on the same scale the
dashboard already uses (`actual_verdict`), then computes:

- Overall confusion matrix (forecast × actual).
- Per-rule false-positive vetos: rule said NO_GO but the day actually fired
  (peak ≥ 8 kt). These are the rules whose thresholds are over-aggressive.
- Per-rule false-negative greens: rule said GO but the day didn't fire.
  Less critical — we mostly care about over-vetoing.

Deliberately doesn't auto-tune thresholds; surfaces evidence for a human.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from oracle.config import StationRole
from oracle.engine import _aggregate, apply_rules
from oracle.knowledge.rules import Verdict
from oracle.logger import RunStore, default_store
from oracle.pillars.measurements import WindReading
from oracle.pillars.meteo import MeteoSnapshot
from oracle.pillars.pressure import PressureReading, PressureSnapshot

# Same scale the dashboard uses to colour the 'Actual (Urfeld peak)' strip.
_ACTUAL_GO_KT = 12.0      # session-worthy
_ACTUAL_MAYBE_KT = 8.0    # ignited but marginal


def actual_verdict(peak_avg_kt: float | None) -> str | None:
    """Categorise an Urfeld-peak ground-truth value onto the go/maybe/no_go scale.

    Single source of truth — the dashboard imports this too so both views agree.
    """
    if peak_avg_kt is None:
        return None
    if peak_avg_kt >= _ACTUAL_GO_KT:
        return "go"
    if peak_avg_kt >= _ACTUAL_MAYBE_KT:
        return "maybe"
    return "no_go"


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

    @property
    def overall_accuracy(self) -> float:
        """Diagonal sum / total. Approximate — same-bucket only."""
        if self.sample_size == 0:
            return 0.0
        hits = sum(self.confusion.get(k, {}).get(k, 0) for k in ("go", "maybe", "no_go"))
        return hits / self.sample_size

    def worst_offenders(self, n: int = 5) -> list[RuleStats]:
        """Top-N rules by false-positive vetos — the ones killing real session days."""
        return sorted(
            (s for s in self.rule_stats.values() if s.false_positive_vetos > 0),
            key=lambda s: (-s.false_positive_vetos, s.rule),
        )[:n]


def _empty_confusion() -> dict[str, dict[str, int]]:
    return {f: {a: 0 for a in ("go", "maybe", "no_go")} for f in ("go", "maybe", "no_go")}


def _peak_from(record: dict) -> float | None:
    machine = (record.get("ground_truth") or {}).get("machine") or {}
    return machine.get("peak_avg_knots")


# --- record re-scoring ----------------------------------------------------
# Re-run the rule layer against a record's stored `inputs` block under the
# *current* aggregator. Used to surface "what would the new severity-tiered
# aggregator have said" on historical records — without re-fetching the
# upstream APIs, which would return today's data anyway.


def _pressure_from_dict(p: dict) -> PressureSnapshot:
    measured = datetime.fromisoformat(p["measured_at"])
    return PressureSnapshot(
        thermik_north=PressureReading("Munich", float(p["munich_hpa"]), measured),
        thermik_south=PressureReading("Innsbruck", float(p["innsbruck_hpa"]), measured),
        foehn_south=PressureReading("Bolzano", float(p["bolzano_hpa"]), measured),
    )


def _meteo_from_dict(m: dict) -> MeteoSnapshot:
    return MeteoSnapshot(
        day=date.fromisoformat(m["day"]),
        overnight_cloud_cover_pct=float(m["overnight_cloud_cover_pct"]),
        morning_solar_radiation_wm2=float(m["morning_solar_radiation_wm2"]),
        synoptic_wind_knots=float(m["synoptic_wind_knots"]),
        min_dew_point_spread_c=float(m["min_dew_point_spread_c"]),
        max_boundary_layer_height_m=float(m["max_boundary_layer_height_m"]),
        soil_moisture_m3m3=float(m["soil_moisture_m3m3"]),
        rained_yesterday=bool(m["rained_yesterday"]),
        yesterday_precipitation_mm=float(m["yesterday_precipitation_mm"]),
        max_lifted_index=float(m["max_lifted_index"]),
        min_lifted_index=float(m["min_lifted_index"]),
        max_cape_j_kg=float(m["max_cape_j_kg"]),
        max_daytime_low_cloud_pct=float(m["max_daytime_low_cloud_pct"]),
        wind_850_direction_at_peak_deg=float(m["wind_850_direction_at_peak_deg"]),
        max_wind_700_knots=float(m["max_wind_700_knots"]),
    )


def _wind_from_dict(w: dict) -> WindReading:
    return WindReading(
        station=w["station"],
        role=StationRole(w["role"]),
        avg_knots=float(w["avg_knots"]),
        gust_knots=float(w["gust_knots"]),
        direction_deg=w.get("direction_deg"),
        measured_at=datetime.fromisoformat(w["measured_at"]),
    )


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
    if not p or not m:
        return None
    try:
        snapshot = _pressure_from_dict(p)
        meteo_snap = _meteo_from_dict(m)
        winds = [_wind_from_dict(w) for w in winds_raw]
    except (KeyError, ValueError, TypeError):
        return None

    verdicts = apply_rules(snapshot, meteo_snap, winds)
    return _aggregate(verdicts).value, verdicts


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
    """
    store = store or default_store()
    rewritten: list[str] = []
    unchanged: list[str] = []
    skipped: list[str] = []
    flipped: list[tuple[str, str, str]] = []  # (iso, old_overall, new_overall)

    for iso in store.list_days():
        if since and date.fromisoformat(iso) < since:
            continue
        if until and date.fromisoformat(iso) > until:
            continue
        record = store.read(iso)
        if record is None:
            continue
        result = rescore_record(record)
        if result is None:
            skipped.append(iso)
            continue
        new_overall, new_verdicts = result
        record["overall_resimulated"] = new_overall
        record["verdicts_resimulated"] = [
            {
                "rule": v.rule,
                "signal": v.signal.value,
                "severity": v.severity.value,
                "reason_en": v.reason_en,
                "reason_de": v.reason_de,
            }
            for v in new_verdicts
        ]
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
) -> Report:
    """Walk every logged record and aggregate forecast-vs-actual metrics."""
    store = store or default_store()
    confusion = _empty_confusion()
    rule_stats: dict[str, RuleStats] = {}
    sample_days: list[str] = []

    for iso in store.list_days():
        if since and date.fromisoformat(iso) < since:
            continue
        if until and date.fromisoformat(iso) > until:
            continue
        record = store.read(iso)
        if record is None:
            continue
        peak = _peak_from(record)
        actual = actual_verdict(peak)
        if actual is None:
            continue
        forecast = record.get("overall")
        if forecast not in confusion:
            # Unknown overall — skip rather than crash on legacy data.
            continue
        confusion[forecast][actual] += 1
        sample_days.append(iso)

        for v in record.get("verdicts", []):
            stats = rule_stats.setdefault(v["rule"], RuleStats(rule=v["rule"]))
            if v["signal"] == "no_go":
                stats.vetos += 1
                if actual in ("go", "maybe"):
                    stats.false_positive_vetos += 1
            elif v["signal"] == "go":
                stats.greens += 1
                if actual == "no_go":
                    stats.false_negative_greens += 1

    return Report(
        sample_size=len(sample_days),
        days_with_ground_truth=sample_days,
        confusion=confusion,
        rule_stats=rule_stats,
    )


def format_text_report(report: Report, rule_filter: str | None = None) -> str:
    """Plain-text summary suitable for `oracle calibrate` stdout."""
    if report.sample_size == 0:
        return (
            "No days with ground truth yet. Run `oracle backfill` to merge "
            "Urfeld peak data into the day's forecast log first."
        )

    lines: list[str] = []
    lines.append(f"Calibration sample: {report.sample_size} days with ground truth.")
    if report.sample_size < 14:
        lines.append(
            "  ⚠  small sample — interpret with caution. Wait for "
            "≥ 14 days before tuning thresholds from this report."
        )
    lines.append("")
    lines.append(f"Overall accuracy (same-bucket): {report.overall_accuracy:.0%}")
    lines.append("")
    lines.append("Confusion matrix (rows=forecast, cols=actual):")
    lines.append(f"  {'':>10s}  {'go':>5s}  {'maybe':>5s}  {'no_go':>5s}")
    for f in ("go", "maybe", "no_go"):
        row = report.confusion[f]
        lines.append(
            f"  {f:>10s}  {row['go']:>5d}  {row['maybe']:>5d}  {row['no_go']:>5d}"
        )
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
        "FP-veto = rule said NO_GO but Urfeld peak ≥ 8 kt (rule killed a real session). "
        "FN-green = rule said GO but day didn't fire."
    )
    return "\n".join(lines)
