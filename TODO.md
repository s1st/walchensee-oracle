# Phase A — Training dataset

Goal: make the replay CSV export emit all three ground-truth labels
(peak / duration / thermal) and the metadata columns (month / year / era)
the spike needs for year-blocked CV and era-aware sanity checks.

- [x] Confirm current state of `calibration.py` (read 2026-06-14)
- [x] Extend `_CSV_COLUMNS` with `actual_verdict_duration`, `actual_verdict_thermal`, `month`, `year`, `era`
- [x] Extend `_row_for` to populate the new columns
- [x] Add unit tests asserting the new columns exist + thermal label decontaminates foehn + months filter still works
- [x] `pytest` green (187/187)
- [x] `ruff check` clean
- [x] `mypy src` — only pre-existing errors on `main`/branch (logger.py:101, calibration.py:582) — not in scope
- [x] End-to-end smoke via the CLI's `_resolve_months(season=True)` path (Apr–Oct default)
- [x] Commit on `ml-classifier`
- [ ] (Follow-up, not blocking) Regenerate `data/replay_*.csv` on the user's local env once the bucket is mounted — the files are gitignored. The next session can run `oracle replay --from/--to` then `oracle calibrate --csv data/replay_full.csv --replayed` to refresh.

