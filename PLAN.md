# Phase A — Training dataset plan

## Approach
Add columns to `_CSV_COLUMNS` + `_row_for` in `src/oracle/calibration.py`:

- `actual_verdict_duration` — `actual_verdict_duration(machine)`, target for the spike
- `actual_verdict_thermal`  — `actual_verdict_thermal(machine)`, primary target (decontaminates foehn/frontal)
- `actual_verdict` (peak)   — kept for backward compat with existing notebooks
- `month` / `year` / `era`  — derived from `record["day"]` via `era_of()` and `date.fromisoformat`

The months filter is already threaded through `export_csv` (per the handoff's
caveat — it was there in the live file, not pending). Test will assert
the filter still works end-to-end.

Records without `samples_above_8kt` get empty duration/thermal columns
(None → empty string in CSV) — that's correct: the duration/thermal labels
require the buoy day-curve, the peak label only needs the peak reading.
The row count stays the same for the peak label.

## Verification
- `pytest tests/test_calibration.py` green (new test + existing 14 CSV tests)
- `ruff check src tests` clean
- `mypy src` clean

## Out of scope
- Regenerating the actual `data/replay_*.csv` files (gitignored, on the user's bucket)
- Phase B (ml dep group) / C (ceiling spike) / D (distill) / E (honest comparison)
