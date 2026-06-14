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
- [x] Commit `a77df22` on `ml-classifier` (pushed to `origin/ml-classifier`)
- [ ] (Follow-up, not blocking) Regenerate `data/replay_*.csv` on the user's local env once the bucket is mounted — the files are gitignored. The next session can run `oracle replay --from/--to` then `oracle calibrate --csv data/replay_full.csv --replayed` to refresh.

# Phase B — `ml` dep group + CLI subcommand shell

Goal: add the optional `ml` dep group to `pyproject.toml` (kept out of both Dockerfiles) and wire a `oracle ml train` subcommand shell with a lazy dep guard. Phase C replaces the body with the actual training loop; the CLI surface and the deps-guard contract are stable from this commit.

- [x] Add `ml = [scikit-learn>=1.4, pandas>=2.0, numpy>=1.24, matplotlib>=3.7]` to `pyproject.toml`
- [x] Verify `Dockerfile.job` and `Dockerfile.dashboard` don't pick up the new extra (grep'd — no matches)
- [x] Add `ml_app` sub-typer in `cli.py` with `train` command
- [x] `train` signature: `--csv PATH (required), --label thermal|peak|duration, --horizon N, --out PATH`
- [x] Deps guard: `importlib.util.find_spec("sklearn")` (mypy-friendly, no real import on the prod image)
- [x] `pytest` green (194/194 — 7 new in test_ml_cli.py)
- [x] `ruff check` clean
- [x] `mypy src` — only the same 2 pre-existing errors (logger.py:101, calibration.py:582); no new errors from this commit
- [x] Smoke: `oracle --help` shows `ml`; `oracle ml train --csv /tmp/x.csv` (no sklearn) fails cleanly with install hint
- [x] Commit + push to `origin/ml-classifier`
