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
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from oracle import config
from oracle.engine import aggregate, apply_rules
from oracle import storm_classifier
from oracle.knowledge.rules import SIGNAL_ORDER, Signal, Verdict
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


# --- skill scores & cost matrix -------------------------------------------
# Raw 3-class accuracy (the diagonal sum) is a trap on this corpus: the
# always-GO constant forecast scores ~49.5% (GO is the plurality class), which
# *beats* the tuned system's 48.3%. Optimising accuracy therefore optimises
# toward a constant. The skill scores below subtract off what a constant
# forecast achieves by chance, so a constant scores exactly 0 and only genuine
# discrimination shows up. Use these — not `overall_accuracy` — to compare tunes.
#
#   Heidke (HSS): (PC − E) / (1 − E),       E = Σ p(fc=i)·p(obs=i)
#   Peirce (PSS): (PC − E) / (1 − E_obs),   E_obs = Σ p(obs=i)²
#
# PC is the proportion correct (= overall_accuracy). Both are 0 for any
# constant forecast and 1 for a perfect one; PSS is unbiased to the base rate
# (can't be hedged up by always predicting the common class).

# The two user-facing errors are asymmetric. A *missed session* (we said
# NO_GO/MAYBE but the lake fired GO) strands the rider at home on a rare good
# day — the worst outcome for a forecast whose whole job is catching the
# thermal. A *wasted drive* (we said GO, lake was dead) costs an hour each way.
# We weight a missed GO as MISSED_SESSION_COST wasted drives. That ratio is the
# one judgement call here — kept explicit so it can be argued with. MAYBE is the
# hedge and carries half-credit on either side; the diagonal is free.
WASTED_DRIVE_COST = 1.0
MISSED_SESSION_COST = 2.0   # a missed GO day hurts ~2× a wasted drive

# cost[forecast][actual]
_COST: dict[str, dict[str, float]] = {
    "go":    {"go": 0.0,                        "maybe": 0.5 * WASTED_DRIVE_COST,   "no_go": WASTED_DRIVE_COST},
    "maybe": {"go": 0.5 * MISSED_SESSION_COST,  "maybe": 0.0,                       "no_go": 0.5 * WASTED_DRIVE_COST},
    "no_go": {"go": MISSED_SESSION_COST,        "maybe": 0.5 * MISSED_SESSION_COST, "no_go": 0.0},
}


def _marginals(confusion: dict[str, dict[str, int]]) -> tuple[dict[str, int], dict[str, int], int]:
    """(forecast totals, actual totals, grand total) from a confusion dict."""
    fc = {s.value: sum(confusion[s.value].values()) for s in SIGNAL_ORDER}
    ac = {s.value: sum(confusion[f.value][s.value] for f in SIGNAL_ORDER) for s in SIGNAL_ORDER}
    return fc, ac, sum(fc.values())


def _proportion_correct(confusion: dict[str, dict[str, int]], total: int) -> float:
    if total == 0:
        return 0.0
    return sum(confusion[s.value][s.value] for s in SIGNAL_ORDER) / total


def heidke_skill_score(confusion: dict[str, dict[str, int]]) -> float:
    """Heidke skill score — accuracy relative to a random forecast that keeps
    both marginals. 0 for any constant forecast, 1 for perfect."""
    fc, ac, total = _marginals(confusion)
    if total == 0:
        return 0.0
    pc = _proportion_correct(confusion, total)
    e = sum((fc[s.value] / total) * (ac[s.value] / total) for s in SIGNAL_ORDER)
    return 0.0 if e >= 1.0 else (pc - e) / (1.0 - e)


def peirce_skill_score(confusion: dict[str, dict[str, int]]) -> float:
    """Peirce (Hanssen–Kuipers) skill score — base-rate-unbiased: a forecast
    can't inflate it by always calling the common class. 0 for any constant."""
    fc, ac, total = _marginals(confusion)
    if total == 0:
        return 0.0
    pc = _proportion_correct(confusion, total)
    e = sum((fc[s.value] / total) * (ac[s.value] / total) for s in SIGNAL_ORDER)
    e_obs = sum((ac[s.value] / total) ** 2 for s in SIGNAL_ORDER)
    return 0.0 if e_obs >= 1.0 else (pc - e) / (1.0 - e_obs)


def mean_cost(confusion: dict[str, dict[str, int]]) -> float:
    """Average per-day cost under `_COST` (lower is better). Captures the
    missed-session vs wasted-drive asymmetry that accuracy and skill ignore."""
    _, _, total = _marginals(confusion)
    if total == 0:
        return 0.0
    c = sum(
        confusion[f.value][a.value] * _COST[f.value][a.value]
        for f in SIGNAL_ORDER for a in SIGNAL_ORDER
    )
    return c / total


def constant_baselines(confusion: dict[str, dict[str, int]]) -> dict[str, dict[str, float]]:
    """Accuracy + mean-cost of each always-X constant forecast, derived from the
    observed marginals. Skill scores are 0 by construction, so the system beats
    a constant iff its skill > 0 — but the cost column still shows *which*
    constant a cost-tuned system has to beat."""
    _, ac, total = _marginals(confusion)
    out: dict[str, dict[str, float]] = {}
    for f in SIGNAL_ORDER:
        if total == 0:
            out[f.value] = {"accuracy": 0.0, "mean_cost": 0.0}
            continue
        out[f.value] = {
            "accuracy": ac[f.value] / total,
            "mean_cost": sum(ac[a.value] * _COST[f.value][a.value] for a in SIGNAL_ORDER) / total,
        }
    return out


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


# --- thermal-character gates for the `thermal` label ----------------------
# The duration label asks "did rideable wind blow for ~an hour?". The thermal
# label adds "...and did it look like a *thermal*, not foehn/frontal wind?".
# We can't measure wind direction (the Urfeld buoy doesn't report it, and the
# 2026-06-13 source probe + station survey found no lakeside station with
# direction — see docs/fable_findings.md §1), so we gate on the two thermal
# signatures we *can* read off the stored sample curve:
#
#   1. Onset timing — a solar thermal ignites mid-morning. Wind already blowing
#      before the ignition window opens is synoptic/foehn, not a thermal.
#      Reuses config.IGNITION_WINDOW_LOCAL — no new fitted constant.
#   2. Gust coherence — thermals are comparatively smooth (gust factor ~1.5–1.7
#      at Urfeld). A very ragged session is a gust front / frontal squall.
#      (Foehn is laminar and passes this gate; it's caught by the onset gate.)
#
# These are LABEL-defining domain estimates, not forecaster thresholds: they
# describe what counts as a ground-truth "thermal session", are set from
# physics, and are deliberately *not* calibrated — you can't fit the target
# against itself. Kept lenient so they reject only clear non-thermals.
_THERMAL_ONSET_RUN = 3            # consecutive ≥8 kt samples (~30 min) that define onset
_THERMAL_MAX_GUST_FACTOR = 2.2    # median gust/avg over ignited samples; above → ragged/frontal


def _minute_of_day(iso_ts: str | None) -> int | None:
    return _ignition_minute_of_day(iso_ts)  # shared parser; named for readability here


def _sustained_onset_minute(samples: list[dict], threshold_kt: float, run: int) -> int | None:
    """Minute-of-day at the start of the first run of `run` consecutive samples
    with avg ≥ `threshold_kt`. Ignores lone early blips (a single 8 kt gust at
    dawn won't count as onset). None if no such run exists."""
    count = 0
    start_idx: int | None = None
    for i, s in enumerate(samples):
        if (s.get("avg_kt") or 0.0) >= threshold_kt:
            if count == 0:
                start_idx = i
            count += 1
            if count >= run and start_idx is not None:
                return _minute_of_day(samples[start_idx].get("t"))
        else:
            count = 0
            start_idx = None
    return None


def _median_gust_factor(samples: list[dict], threshold_kt: float) -> float | None:
    """Median gust/avg ratio over samples at or above `threshold_kt`. None if the
    curve carries no ignited sample with a usable gust value."""
    factors: list[float] = []
    for s in samples:
        avg = s.get("avg_kt") or 0.0
        gust = s.get("gust_kt")
        if avg >= threshold_kt and gust:
            factors.append(gust / avg)
    if not factors:
        return None
    factors.sort()
    mid = len(factors) // 2
    return factors[mid] if len(factors) % 2 else (factors[mid - 1] + factors[mid]) / 2.0


def actual_verdict_thermal(machine: dict | None) -> str | None:
    """Thermal-session label — the duration label, gated on thermal *character*.

    A day that produced sustained rideable wind is labelled GO/MAYBE only if the
    wind also looks thermal: it ignited at/after the daytime ignition window and
    wasn't a ragged frontal squall. Otherwise NO_GO — the wind fired, but not as
    a thermal. This de-contaminates the label so foehn/frontal days stop being
    counted as thermal sessions (Fable review #1). Season is applied upstream by
    the `--season` filter; convective (storm) days are scored on thermal merit
    since the LI-decouple experiment, only tallied separately, not excluded.

    Records without the raw `samples` curve can't be character-assessed, so they
    keep the duration verdict unchanged (no evidence to downgrade on).
    """
    base = actual_verdict_duration(machine)
    if base is None or base == Signal.NO_GO.value:
        return base
    assert machine is not None  # base is None when machine is falsy
    samples = machine.get("samples")
    if not samples:
        return base
    # Gate 1 — onset at or after the thermal ignition window opens.
    onset = _sustained_onset_minute(samples, _ACTUAL_MAYBE_KT, _THERMAL_ONSET_RUN)
    window_start = config.IGNITION_WINDOW_LOCAL[0]
    if onset is None or onset < window_start.hour * 60 + window_start.minute:
        return Signal.NO_GO.value
    # Gate 2 — gust coherence (only when the curve lets us judge it).
    gust_factor = _median_gust_factor(samples, _ACTUAL_MAYBE_KT)
    if gust_factor is not None and gust_factor > _THERMAL_MAX_GUST_FACTOR:
        return Signal.NO_GO.value
    return base


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
    replayed: bool = False  # scored replay records joined with main-record ground truth
    storm_days: list[str] = field(default_factory=list)  # storm-suspected, now scored (LI-decoupled)
    months: frozenset[int] | None = None  # season filter applied, if any

    @property
    def overall_accuracy(self) -> float:
        """Diagonal sum / total. Approximate — same-bucket only.

        Beatable by a constant on this corpus — report `peirce_score` /
        `heidke_score` / `mean_cost` alongside it, never alone (see the
        module-level skill-score note for why).
        """
        if self.sample_size == 0:
            return 0.0
        hits = sum(self.confusion.get(s.value, {}).get(s.value, 0) for s in SIGNAL_ORDER)
        return hits / self.sample_size

    @property
    def peirce_score(self) -> float:
        """Peirce (Hanssen–Kuipers) skill score — base-rate-unbiased; 0 for any constant."""
        return peirce_skill_score(self.confusion)

    @property
    def heidke_score(self) -> float:
        """Heidke skill score — accuracy over chance; 0 for any constant."""
        return heidke_skill_score(self.confusion)

    @property
    def mean_cost(self) -> float:
        """Average per-day cost (lower is better) under the missed-session/wasted-drive matrix."""
        return mean_cost(self.confusion)

    def baselines(self) -> dict[str, dict[str, float]]:
        """Accuracy + mean-cost of the three always-X constant forecasts."""
        return constant_baselines(self.confusion)

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
    """Forecast-time thunderstorm advisory for a stored record.

    Single source of truth for the storm advisory (dashboard yellow border +
    storm-day tally in `compile_report`), delegated to the calibrated
    `storm_classifier` over the record's afternoon convective features, which
    falls back to the LI ≤ −2 rule when those features are absent (archive host /
    pre-2021 / legacy records). As of the LI-decouple experiment storm days are
    **no longer quarantined** — they are scored on thermal merit like any other
    day (the thermal usually still fires before the gust front), and
    `atmospheric_stability` stays GREEN on them rather than vetoing. The flag is
    retained to label/count storm days and paint the advisory border.
    """
    meteo = (record.get("inputs") or {}).get("meteo") or {}
    return storm_classifier.storm_advisory_from_meteo_dict(meteo)


def observed_storm(machine: dict | None) -> bool | None:
    """Whether a thunderstorm/gust front **actually** hit the lake, from the
    stored buoy curve — the observed counterpart to `storm_suspected` (forecast).

    True when the afternoon (12–18 local) buoy samples show a gust spike
    (≥ `STORM_OBS_GUST_KT`) AND a sharp MSL pressure jump
    (≥ `STORM_OBS_PRESSURE_JUMP_HPA`). Returns None when there isn't enough buoy
    data to decide (so the dashboard shows no border and training skips the day,
    rather than scoring a gap as "no storm"). Same definition as the storm
    classifier's training label.
    """
    samples = (machine or {}).get("samples") or []
    aft = [
        s for s in samples
        if s.get("t") and 12 <= datetime.fromisoformat(s["t"]).hour <= 18
    ]
    gusts = [s["gust_kt"] for s in aft if s.get("gust_kt") is not None]
    if len(gusts) < 3:
        return None
    press = [s["pressure_hpa"] for s in aft if s.get("pressure_hpa") is not None]
    jump = (max(press) - min(press)) if len(press) > 1 else 0.0
    return max(gusts) >= config.STORM_OBS_GUST_KT and jump >= config.STORM_OBS_PRESSURE_JUMP_HPA


def _label_record(record: dict, mode: str) -> str | None:
    """Dispatch the configured labeller against one record's machine block."""
    if mode == "thermal":
        return actual_verdict_thermal(_machine_from(record))
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


_MONTH_ABBR = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def parse_months(spec: str) -> frozenset[int]:
    """Parse a months spec like '4-10' (range) or '4,5,9' (list) into a set.

    Raises ValueError on out-of-range or malformed input so the CLI can
    surface a clean BadParameter.
    """
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            out.update(range(lo, hi + 1))
        else:
            out.add(int(part))
    if not out or any(m < 1 or m > 12 for m in out):
        raise ValueError(f"months out of 1–12 range: {spec!r}")
    return frozenset(out)


def _months_label(months: frozenset[int]) -> str:
    """Compact human label: 'Apr–Oct' for a contiguous run, else 'Apr,May,Sep'."""
    ms = sorted(months)
    if ms == list(range(ms[0], ms[-1] + 1)) and len(ms) > 1:
        return f"{_MONTH_ABBR[ms[0]]}–{_MONTH_ABBR[ms[-1]]}"
    return ",".join(_MONTH_ABBR[m] for m in ms)


def _iter_window_days(
    store: RunStore, since: date | None, until: date | None,
    replayed: bool = False, months: set[int] | frozenset[int] | None = None,
) -> Iterator[str]:
    """Yield logged ISO days that fall within [since, until] (inclusive ends).

    Single source of the date-window filter shared by `rescore_all`,
    `compile_report` and `export_csv`. With `replayed=True`, walk the
    replay namespace (`runs/replay/`) instead of the main records.

    `months` (e.g. `config.ACTIVE_SEASON_MONTHS`) restricts to those calendar
    months — the product only serves Apr–Oct, so calibration scores on that
    window rather than letting winter dominate the negative class.
    """
    days = store.list_replays() if replayed else store.list_days()
    for iso in days:
        d = date.fromisoformat(iso)
        if since and d < since:
            continue
        if until and d > until:
            continue
        if months is not None and d.month not in months:
            continue
        yield iso


def _merged_replay_record(store: RunStore, iso: str) -> dict | None:
    """Join one replay record with the main record's ground truth.

    Replay records carry verdicts + inputs but an empty `ground_truth`
    (the buoy outcome lives in the main record — for the historical
    backfill, a stub with ground truth and no verdicts). Overlaying the
    main record's `ground_truth` onto the replay record yields a dict
    with the exact shape of a live record, so every record-level helper
    (`_label_record`, `storm_suspected`, `_row_for`, the verdict-key
    selection) works on it unchanged.

    Caveat: `storm_suspected` reads the lifted index from the *replay*
    inputs. Pre-2021 archive data has no LI, so the storm quarantine
    never fires for that era — gust-front days there stay in the matrix.
    """
    replay = store.read_replay(iso)
    main = store.read(iso)
    if replay is None or main is None:
        return None
    return {**replay, "ground_truth": main.get("ground_truth") or {"machine": None, "human": None}}


# --- record re-scoring ----------------------------------------------------
# Re-run the rule layer against a record's stored `inputs` block under the
# *current* aggregator. Used to surface "what would the new severity-tiered
# aggregator have said" on historical records — without re-fetching the
# upstream APIs, which would return today's data anyway.


def rescore_record(record: dict, *, now: datetime | None = None) -> tuple[str, list[Verdict]] | None:
    """Re-run the rule layer against a record's stored inputs.

    Returns (overall, verdicts) under the current aggregator, or None if the
    record's inputs are too incomplete to reconstruct (older log schema —
    common for records written before later meteo fields shipped).

    `now` anchors time-sensitive rules (air_lake_delta staleness). Replay
    rescoring passes the record's own day so a decade-old buoy reading
    isn't flagged stale against the wall clock; live records leave it None
    (their readings are recent relative to any rescore run).
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

    verdicts = apply_rules(snapshot, meteo_snap, winds, lake_temp, now=now)
    return aggregate(verdicts).value, verdicts


def rescore_all(
    store: RunStore | None = None,
    since: date | None = None,
    until: date | None = None,
    dry_run: bool = False,
    replayed: bool = False,
    months: set[int] | frozenset[int] | None = None,
) -> dict:
    """Walk every logged record, re-score it, and persist the result.

    Adds two fields to each successfully re-scored record without touching
    the historical `overall` / `verdicts` (which stay as evidence of what
    the aggregator-of-the-day actually said):

      - `overall_resimulated`
      - `verdicts_resimulated`

    Returns a small report: counts + list of skipped days.

    With `replayed=True`, walk the replay records instead — this is the
    fast inner loop of the historical calibration: tune a threshold, then
    re-score ~3,300 replay days from their stored inputs without touching
    the Open-Meteo archive again.

    Since 2026-06-12 the bucket also contains ~3,600 historical buoy
    stub records (2016-2026, no `inputs` block). `rescore_record` returns
    None for those, so they land in `skipped` — no harm, but a no-arg
    rescore is slow because every stub is read from GCS and discarded.
    Pass `since=config.PROJECT_FIRST_DAY` to rescore only the project's days.
    """
    store = store or default_store()
    rewritten: list[str] = []
    unchanged: list[str] = []
    skipped: list[str] = []
    flipped: list[tuple[str, str, str]] = []  # (iso, old_overall, new_overall)

    for iso in _iter_window_days(store, since, until, replayed=replayed, months=months):
        record = store.read_replay(iso) if replayed else store.read(iso)
        if record is None:
            continue
        # Anchor time-sensitive rules to the replay day's noon, mirroring
        # engine.run_replay; live records keep wall-clock semantics.
        now = datetime.combine(date.fromisoformat(iso), time(12, 0)) if replayed else None
        result = rescore_record(record, now=now)
        if result is None:
            skipped.append(iso)
            continue
        new_overall, new_verdicts = result
        record["overall_resimulated"] = new_overall
        record["verdicts_resimulated"] = [verdict_to_dict(v) for v in new_verdicts]
        old_overall = record.get("overall") or ""
        if old_overall != new_overall:
            flipped.append((iso, old_overall, new_overall))
        if not dry_run:
            if replayed:
                store.write_replay(iso, record)
            else:
                store.write(iso, record)
            rewritten.append(iso)
        else:
            unchanged.append(iso)

    return {
        "rewritten": rewritten,
        "unchanged": unchanged,
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
    replayed: bool = False,
    months: set[int] | frozenset[int] | None = None,
) -> Report:
    """Walk every logged record and aggregate forecast-vs-actual metrics.

    Since 2026-06-12 the bucket also contains ~3,600 historical buoy
    stub records (2016-2026, no `overall` block). They contribute to
    the `actual` (ground truth) side of the confusion matrix — useful
    for hypothesis testing at scale — but the per-record forecast lookup
    is None, so they don't pollute the verdict scoring. Pass
    `since=config.PROJECT_FIRST_DAY` to restrict to the project's own days when
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

    `replayed`: when True, score the replay records (`runs/replay/`) against
    the ground truth stored in the matching main records — the join that
    makes the historical backfill usable for calibration. Orthogonal to
    `resimulated`: replay verdicts go stale after a threshold tune like any
    others, so the loop is `oracle rescore --replayed` then
    `compile_report(replayed=True, resimulated=True)`.
    """
    overall_key = "overall_resimulated" if resimulated else "overall"
    verdicts_key = "verdicts_resimulated" if resimulated else "verdicts"

    store = store or default_store()
    confusion = _empty_confusion()
    rule_stats: dict[str, RuleStats] = {}
    sample_days: list[str] = []
    storm_days: list[str] = []

    for iso in _iter_window_days(store, since, until, replayed=replayed, months=months):
        record = _merged_replay_record(store, iso) if replayed else store.read(iso)
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
            # LI-decouple experiment: storm days are no longer quarantined. The
            # thermal usually still fires before the gust front, so we score the
            # day on thermal merit like any other and only tally it as a storm
            # day (for the count + the dashboard advisory).
            storm_days.append(iso)
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
        replayed=replayed,
        storm_days=storm_days,
        months=frozenset(months) if months is not None else None,
    )


def format_text_report(report: Report, rule_filter: str | None = None) -> str:
    """Plain-text summary suitable for `oracle calibrate` stdout."""
    if report.sample_size == 0:
        msg = (
            "No days with ground truth yet. Run `oracle backfill` to merge "
            "Urfeld peak data into the day's forecast log first."
        )
        if report.storm_days:
            msg += (
                f" ({len(report.storm_days)} storm-suspected day(s) seen — "
                "see `oracle backfill`.)"
            )
        return msg

    label_desc = {
        "peak": "peak avg ≥12 kt → GO, ≥8 kt → MAYBE",
        "duration": "≥6 samples (~1h) above 12 kt → GO, above 8 kt → MAYBE",
        "thermal": "duration label, gated on thermal character (mid-day onset + coherent gusts)",
    }.get(report.label_mode, report.label_mode)

    view = "resimulated (current rule layer)" if report.resimulated else "historical (verdicts as written)"
    if report.replayed:
        view = f"replay (archive forecasts), {view}"
    lines: list[str] = []
    season = "all months" if report.months is None else _months_label(report.months)
    lines.append(
        f"Calibration sample: {report.sample_size} days with ground truth "
        f"(label = {report.label_mode}: {label_desc}; view = {view}; season = {season})."
    )
    if report.storm_days:
        lines.append(
            f"  ⚡ {len(report.storm_days)} storm-suspected day(s) "
            f"(LI ≤ {config.MIN_LIFTED_INDEX:.0f}) included and scored on thermal "
            "merit (LI-decoupled) — the storm shows as a separate Caution advisory."
        )
    if report.sample_size < 14:
        lines.append(
            "  ⚠  small sample — interpret with caution. Wait for "
            "≥ 14 days before tuning thresholds from this report."
        )
    lines.append("")
    baselines = report.baselines()
    best_const = min(baselines, key=lambda k: baselines[k]["mean_cost"]) if report.sample_size else None
    lines.append("Skill (constant forecasts score 0 — accuracy alone is misleading here):")
    lines.append(f"  Peirce (HK) skill : {report.peirce_score:+.3f}")
    lines.append(f"  Heidke skill      : {report.heidke_score:+.3f}")
    lines.append(f"  mean cost / day   : {report.mean_cost:.3f}  (lower is better)")
    lines.append(f"  overall accuracy  : {report.overall_accuracy:.1%}  (beatable by a constant — see below)")
    lines.append("")
    lines.append("Constant-forecast baselines (what a single fixed verdict would score):")
    lines.append(f"  {'always':<8s}  {'accuracy':>8s}  {'mean cost':>9s}")
    for sig in SIGNAL_ORDER:
        b = baselines[sig.value]
        marker = "  ← cheapest constant" if sig.value == best_const else ""
        lines.append(f"  {sig.value:<8s}  {b['accuracy']:>7.1%}  {b['mean_cost']:>9.3f}{marker}")
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
    # Three ground-truth target scales. The peak label is the simplest
    # (max avg knots → go/maybe/no_go); duration adds a "wind blew for an
    # hour" gate; thermal further requires thermal character (mid-day
    # onset + coherent gusts) so foehn/frontal days don't pollute the
    # positive class. Phase A/C: the ML spike trains on `actual_verdict_thermal`.
    "actual_verdict", "actual_verdict_duration", "actual_verdict_thermal",
    # storm flag: True = LI ≤ MIN_LIFTED_INDEX. Scored on thermal merit (LI-decoupled),
    # tallied separately, and used to paint the dashboard's Caution advisory.
    # Kept in the export (not dropped) so the ML notebook can mask or model it.
    "storm_suspected",
    # what the rule layer said (for benchmarking ML against the heuristic)
    "forecast_overall", "forecast_overall_resimulated",
    # date metadata for season filter + year-blocked CV + era-aware sanity
    # checks. `era` is "ifs" or "icon" (see `era_of` and `config.ICON_ERA_START`).
    "month", "year", "era",
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
    # day → month / year / era for the season filter and year-blocked CV.
    # `_iter_window_days` only yields iso strings, so the parse is safe in
    # practice; tolerate a malformed field by leaving the metadata empty
    # rather than dropping the row (the row's other columns are still valid).
    day_iso = record.get("day")
    try:
        day_obj = date.fromisoformat(day_iso) if day_iso else None
    except (TypeError, ValueError):
        day_obj = None
    return {
        "day": day_iso,
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
        # Three target scales. Duration/thermal need the buoy day-curve; a
        # record with only the peak reading yields None → empty CSV cell.
        "actual_verdict": actual_verdict(peak),
        "actual_verdict_duration": actual_verdict_duration(machine),
        "actual_verdict_thermal": actual_verdict_thermal(machine),
        "storm_suspected": storm_suspected(record),
        "forecast_overall": record.get("overall"),
        "forecast_overall_resimulated": record.get("overall_resimulated"),
        "month": day_obj.month if day_obj else None,
        "year": day_obj.year if day_obj else None,
        "era": era_of(day_iso) if day_iso else None,
    }


def export_csv(
    path: Path | str,
    store: RunStore | None = None,
    since: date | None = None,
    until: date | None = None,
    replayed: bool = False,
    months: set[int] | frozenset[int] | None = None,
) -> int:
    """Write every ground-truthed record to `path` as a flat CSV. Returns row count.

    With `replayed=True`, export the replay records joined with the main
    records' ground truth — the full historical feature/outcome dataset for
    offline ML (see GH issue #12)."""
    store = store or default_store()
    rows: list[dict] = []
    for iso in _iter_window_days(store, since, until, replayed=replayed, months=months):
        record = _merged_replay_record(store, iso) if replayed else store.read(iso)
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


# --- validation harness ---------------------------------------------------
# The replay pass shipped without any of this: every claim was in-sample on the
# same ~3,300 days, with no significance test and no era/holdout split. So
# noise-level changes (one tune was +4 days; another McNemar p≈0.54) shipped
# as "data-fitted (n=3,263)". These helpers make a tune defend itself:
# McNemar for "did this change actually move the needle", and year/era splits
# for "does the optimum hold out of sample". Fable review §5.

_VALID_FORECASTS = frozenset(s.value for s in SIGNAL_ORDER)


@dataclass
class McNemarResult:
    """Paired before/after significance test on the same days.

    `b` = days the change fixed (old wrong → new right); `c` = days it broke
    (old right → new wrong). Only discordant days carry information. A change
    is real only if b ≫ c *and* p is small; b ≈ c (whatever the raw day delta)
    is noise.
    """
    b: int                  # old wrong, new right  (fixed)
    c: int                  # old right, new wrong  (broke)
    n_discordant: int
    statistic: float        # continuity-corrected χ², 1 dof
    p_value: float
    exact: bool             # True → exact binomial (small n), χ² is advisory

    @property
    def net(self) -> int:
        """Net days moved in the new forecast's favour (b − c)."""
        return self.b - self.c


def mcnemar(old_correct: list[bool], new_correct: list[bool], *, exact_below: int = 25) -> McNemarResult:
    """McNemar's test over two correct/incorrect verdict sequences for the same days.

    Uses the exact two-sided binomial when the discordant count is small
    (< `exact_below`, where the χ² approximation is unreliable) and the
    continuity-corrected χ² otherwise. p-value needs no scipy: for 1 dof,
    P(χ² > x) = erfc(√(x/2)).
    """
    b = sum(1 for o, n in zip(old_correct, new_correct, strict=True) if (not o) and n)
    c = sum(1 for o, n in zip(old_correct, new_correct, strict=True) if o and (not n))
    n = b + c
    if n == 0:
        return McNemarResult(0, 0, 0, 0.0, 1.0, exact=True)
    statistic = (abs(b - c) - 1) ** 2 / n
    if n < exact_below:
        k = min(b, c)
        tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
        return McNemarResult(b, c, n, statistic, min(1.0, 2.0 * tail), exact=True)
    return McNemarResult(b, c, n, statistic, math.erfc((statistic / 2.0) ** 0.5), exact=False)


def era_of(iso: str) -> str:
    """'ifs' (Open-Meteo IFS HRES era) or 'icon' (DWD ICON-D2 era) for a day."""
    return "ifs" if date.fromisoformat(iso) < config.ICON_ERA_START else "icon"


def mcnemar_keys(
    store: RunStore | None = None,
    key_old: str = "overall",
    key_new: str = "overall_resimulated",
    *,
    since: date | None = None,
    until: date | None = None,
    label: str = "thermal",
    replayed: bool = False,
    months: set[int] | frozenset[int] | None = None,
) -> McNemarResult:
    """Compare two forecast fields on the same days against the ground-truth label.

    Defaults compare the as-written verdict (`overall`) to the current rule
    layer (`overall_resimulated`) — run `oracle rescore` first to populate the
    latter. To test a single threshold change in isolation, snapshot
    `overall_resimulated` to a custom field before the change, re-score after,
    and pass both field names here. Storm days are scored (not excluded), as in
    `compile_report` since the LI-decouple experiment, so the two views are
    scored on an identical day set.
    """
    store = store or default_store()
    old_correct: list[bool] = []
    new_correct: list[bool] = []
    for iso in _iter_window_days(store, since, until, replayed=replayed, months=months):
        record = _merged_replay_record(store, iso) if replayed else store.read(iso)
        if record is None:
            continue
        actual = _label_record(record, label)
        if actual is None:
            continue
        fo, fn = record.get(key_old), record.get(key_new)
        if fo not in _VALID_FORECASTS or fn not in _VALID_FORECASTS:
            continue
        old_correct.append(fo == actual)
        new_correct.append(fn == actual)
    return mcnemar(old_correct, new_correct)


def reports_by_era(
    store: RunStore | None = None,
    *,
    label: str = "thermal",
    resimulated: bool = False,
    replayed: bool = False,
    months: set[int] | frozenset[int] | None = None,
) -> dict[str, Report]:
    """One Report per model era (IFS vs ICON), split at `config.ICON_ERA_START`.

    A corpus-wide threshold optimum that disagrees across the two eras is
    unstable — the input distributions differ (see the era boundary note in
    config). Reads the store once per era.
    """
    store = store or default_store()
    return {
        "ifs": compile_report(
            store=store, until=config.ICON_ERA_START - timedelta(days=1),
            label=label, resimulated=resimulated, replayed=replayed, months=months,
        ),
        "icon": compile_report(
            store=store, since=config.ICON_ERA_START,
            label=label, resimulated=resimulated, replayed=replayed, months=months,
        ),
    }


def reports_by_year(
    store: RunStore | None = None,
    *,
    label: str = "thermal",
    resimulated: bool = False,
    replayed: bool = False,
    months: set[int] | frozenset[int] | None = None,
) -> dict[int, Report]:
    """One Report per calendar year present in the store — a cheap year-wise CV:
    a tune that only helps in one or two years isn't a real signal. Walks the
    store listing once to find the years, then once more per year."""
    store = store or default_store()
    years = sorted({
        date.fromisoformat(iso).year
        for iso in _iter_window_days(store, None, None, replayed=replayed, months=months)
    })
    return {
        y: compile_report(
            store=store, since=date(y, 1, 1), until=date(y, 12, 31),
            label=label, resimulated=resimulated, replayed=replayed, months=months,
        )
        for y in years
    }


def format_skill_line(name: str, report: Report) -> str:
    """One-line skill summary for a partition, for the --split table."""
    return (
        f"  {name:<8s}  n={report.sample_size:>5d}  "
        f"Peirce={report.peirce_score:+.3f}  Heidke={report.heidke_score:+.3f}  "
        f"cost={report.mean_cost:.3f}  acc={report.overall_accuracy:5.1%}"
    )


def format_mcnemar(result: McNemarResult, *, old: str, new: str) -> str:
    """Human-readable McNemar verdict line."""
    method = "exact binomial" if result.exact else "χ², cont.corr."
    sig = "SIGNIFICANT" if result.p_value < 0.05 else "not significant"
    return (
        f"McNemar {old} → {new}: fixed {result.b}, broke {result.c} "
        f"(net {result.net:+d} of {result.n_discordant} discordant); "
        f"p={result.p_value:.3f} [{method}] → {sig} at α=0.05"
    )
