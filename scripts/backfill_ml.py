"""Backfill the shadow ML classifier block into existing day records.

The `ml_classifier` block is normally written at forecast time (since the
classifier shipped). This recomputes it for *older* records from their
already-stored `inputs.pressure` / `inputs.meteo` — the exact values the
live scorer uses — so the dashboard's 30-day ML row populates retroactively
instead of filling in one day at a time.

Additive and idempotent: it only sets `record["ml_classifier"]`, leaving
`overall`, `verdicts`, `ground_truth`, etc. untouched. Storage backend is
the usual env switch (`RUNS_BUCKET` set -> GCS, else local data/runs).

    python scripts/backfill_ml.py [--since 2026-04-22] [--until 2026-06-14] [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from oracle.logger import default_store
from oracle.ml_classifier import classify


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-04-22")
    ap.add_argument("--until", default=None, help="default: latest record")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = default_store()
    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else date.today()

    written = skipped_no_inputs = already = 0
    d = since
    while d <= until:
        iso = d.isoformat()
        d += timedelta(days=1)
        record = store.read(iso)
        if record is None:
            continue
        inputs = record.get("inputs") or {}
        ml = classify(inputs.get("pressure"), inputs.get("meteo"))
        if ml is None:
            skipped_no_inputs += 1
            continue
        existing = record.get("ml_classifier")
        new_block = ml.to_dict()
        if existing == new_block:
            already += 1
            continue
        record["ml_classifier"] = new_block
        if not args.dry_run:
            store.write(iso, record)
        written += 1
        print(f"  {iso}: ml={ml.verdict}  ({'dry-run' if args.dry_run else 'written'})")

    print(f"\n{written} {'would change' if args.dry_run else 'written'}, "
          f"{already} already current, {skipped_no_inputs} skipped (no inputs).")


if __name__ == "__main__":
    main()
