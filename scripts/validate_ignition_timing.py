#!/usr/bin/env python3
"""Validate the Stage-0 ignition-timing band against ground truth.

For every run JSON with a backfilled `ground_truth.machine`, compute the
predicted band from the stored `inputs.*` (exactly what would ship) and compare
to the observed `first_ignition_at`. Restricted to **thermal-character** days
(`actual_verdict_thermal` non-None) and a configurable month window, so winter
synoptic/foehn wind doesn't pollute the timing signal.

Reports:
  * per-band count + mean / median observed ignition clock time
  * monotonicity (does observed ignition rise EARLY < MIDDAY < LATE?)
  * Spearman rank correlation between the lateness score and observed minute
  * the "rider pain" slice: late-igniting days we did / didn't flag LATE

No sklearn/pandas — pure stdlib, runs against a local runs dir.

    python3 scripts/validate_ignition_timing.py --runs /tmp/runs2026
    python3 scripts/validate_ignition_timing.py --runs data/runs --months 4-10
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
from statistics import mean, median
from typing import Iterator

from oracle.calibration import (
    _ACTUAL_MAYBE_KT,
    _merged_replay_record,
    _sustained_onset_minute,
    _THERMAL_ONSET_RUN,
    actual_verdict_thermal,
    parse_months,
)
from oracle.knowledge.ignition_timing import Band, estimate_from_inputs
from oracle.logger import LocalRunStore

_BANDS = (Band.EARLY, Band.MIDDAY, Band.LATE)


def _hhmm(minute: float) -> str:
    m = int(round(minute))
    return f"{m // 60:02d}:{m % 60:02d}"


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, ties via average rank. Stdlib only."""
    def rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = mean(rx), mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = (sum((v - mx) ** 2 for v in rx) * sum((v - my) ** 2 for v in ry)) ** 0.5
    return num / den if den else 0.0


def _iter_records(args: argparse.Namespace) -> tuple[Iterator[dict], int]:
    """Yield run records and the scan count. `--replayed` joins each replay
    record's stored inputs with the main stub's ground truth (same merge
    `oracle calibrate --replayed` uses), unlocking the ~3.3k-day Urfeld
    archive instead of just the live runs."""
    if args.replayed:
        store = LocalRunStore(Path(args.store))
        days = store.list_replays()

        def gen() -> Iterator[dict]:
            for iso in days:
                rec = _merged_replay_record(store, iso)
                if rec is not None:
                    yield rec
        return gen(), len(days)

    files = sorted(glob.glob(os.path.join(args.runs, "*.json")))

    def gen() -> Iterator[dict]:
        for f in files:
            with open(f) as fh:
                yield json.load(fh)
    return gen(), len(files)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="/tmp/runs2026",
                    help="directory of run JSON files (flat mode)")
    ap.add_argument("--replayed", action="store_true",
                    help="score the ~3.3k-day historical replay archive instead")
    ap.add_argument("--store", default="data/runs",
                    help="RunStore directory for --replayed (holds runs/ + replay/)")
    ap.add_argument("--months", default="4-10",
                    help="month window to keep, e.g. '4-10' (warm season)")
    ap.add_argument("--late-after", type=int, default=14 * 60,
                    help="observed ignition minute counted as 'late' (default 14:00)")
    args = ap.parse_args()

    months = parse_months(args.months)
    records, scanned = _iter_records(args)

    rows = []  # (day, band, score, observed_minute)
    skipped_nonthermal = skipped_nodata = 0
    for rec in records:
        try:
            month = int(rec["day"][5:7])
        except (KeyError, ValueError):
            continue
        if month not in months:
            continue
        machine = (rec.get("ground_truth") or {}).get("machine")
        # Keep only true thermal *sessions*. actual_verdict_thermal returns
        # "no_go" (not None!) for days where wind blew but not as a thermal —
        # night/frontal/foehn — so filtering on `is None` would let all of those
        # through and poison the onset distribution with 00–04h crossings.
        if actual_verdict_thermal(machine) not in ("go", "maybe"):
            skipped_nonthermal += 1
            continue
        # Validate against the *sustained daytime onset* — the start of the first
        # ~30-min ≥8 kt run — not `first_ignition_at` (the first crossing anywhere
        # in 24h, which captures overnight synoptic/frontal wind and poisons the
        # timing label). actual_verdict_thermal already guarantees this onset
        # exists and lands at/after the ignition window.
        samples = (machine or {}).get("samples")
        observed = (
            _sustained_onset_minute(samples, _ACTUAL_MAYBE_KT, _THERMAL_ONSET_RUN)
            if samples else None
        )
        inp = rec.get("inputs") or {}
        pressure, meteo = inp.get("pressure"), inp.get("meteo")
        if observed is None or not pressure or not meteo:
            skipped_nodata += 1
            continue
        est = estimate_from_inputs(pressure, meteo)
        rows.append((rec["day"], est.band, est.score, observed))

    if not rows:
        print("no thermal-day rows matched — widen --months or check --runs path")
        return

    print(f"runs scanned: {scanned}   thermal days used: {len(rows)}   "
          f"(skipped {skipped_nonthermal} non-thermal, {skipped_nodata} no-data)")
    print(f"month window: {args.months}\n")

    print(f"{'band':7} {'n':>3} {'mean ignite':>12} {'median':>8} {'observed range':>18}")
    band_means = {}
    for band in _BANDS:
        obs = [o for _, b, _, o in rows if b == band]
        if not obs:
            print(f"{band.value:7} {0:>3}")
            continue
        band_means[band] = mean(obs)
        print(f"{band.value:7} {len(obs):>3} {_hhmm(mean(obs)):>12} "
              f"{_hhmm(median(obs)):>8} {_hhmm(min(obs)) + '–' + _hhmm(max(obs)):>18}")

    mono = (band_means.get(Band.EARLY, -1) <= band_means.get(Band.MIDDAY, 1e9)
            <= band_means.get(Band.LATE, 1e9))
    print(f"\nmonotonic (EARLY ≤ MIDDAY ≤ LATE mean ignition)? {'YES' if mono else 'NO'}")

    rho = _spearman([s for _, _, s, _ in rows], [o for _, _, _, o in rows])
    print(f"Spearman(score, observed ignition minute): {rho:+.3f}  "
          f"(higher = score tracks lateness)")

    # Rider-pain slice: days that actually ignited late.
    late_days = [(d, b, o) for d, b, _, o in rows if o >= args.late_after]
    flagged = sum(1 for _, b, _ in late_days if b == Band.LATE)
    print(f"\nlate-igniting days (≥ {_hhmm(args.late_after)}): {len(late_days)}")
    if late_days:
        print(f"  flagged LATE in advance: {flagged}/{len(late_days)} "
              f"({100 * flagged / len(late_days):.0f}%)")
        missed = [(d, b.value, _hhmm(o)) for d, b, o in late_days if b != Band.LATE]
        if missed:
            print("  missed (predicted earlier, ignited late):")
            for d, b, hh in missed:
                print(f"    {d}  predicted={b:7} observed={hh}")


if __name__ == "__main__":
    main()
