# Active task: feat-icon-coverage-shadow (2026-06-15)

Goal: re-test the 11→13 feature restriction that motivated the shadow
classifier. The spike's 17→11 finding (cross-era distribution shift)
was correct, but the 11→13 question — "use the 2 ICON-coverage
features the production regime actually has" — is a different
experiment now that 4 ICON years are available. Decide if the shadow
should be retrained.

- [x] Create branch `feat-icon-coverage-shadow`
- [x] Pull NaN rates from `data/replay_full.csv` — confirmed 6 of 8
      "ICON-era-only" features are 100% NaN even in ICON; 2 (BLH, CAPE)
      have real ICON signal
- [x] Write `scripts/icon_coverage_experiment.py` — three configs (a/b/c)
      plus (d) cross-era on the same test window
- [x] Run the experiment; per-fold LOYO + year-blocked results recorded
- [x] Decision: retrain shadow with 13-feature ICON-only (c). +0.0309
      Peirce over (b) on year-blocked 2025+2026, cost penalty shrinks
      4.7× (Δ +0.058 → +0.012)
- [x] Extend `scripts/export_ml_coeffs.py` with `--feature-set`
      and `--train-filter` flags
- [x] Re-export `ml_coeffs.py` with 13-feature ICON-only bundle
      (n=715, 2023-2026 ICON in-season)
- [x] Add labels for the two new features in `ml_classifier.py` label dicts
- [x] Update golden-vector test (verdict: maybe, go/maybe/no_go =
      0.106/0.665/0.228 — verified to match sklearn to 6 dp)
- [x] Verify the production meteo snapshot already carries both fields
      (it does — MeteoSnapshot.to_dict includes them)
- [x] Write `docs/findings/ml-icon-coverage-shadow-2026-06-15.md`
- [x] `pytest` 248/248, `ruff check` clean, `mypy src` clean
- [x] Commit + push branch (not merged to main — user reviews first)

# Deferred from this branch (documented in the writeup)
- Live A/B between (c) and (d) — two parallel shadow cards
- HGB re-test on the 13-feature ICON-only schema
- Lead-time-aware training (replay is lead-0, prod runs lead-1/2)
- 13-feature cross-era sweep (columns mostly-learned-from-the-median
  add noise; assumed same as the spike's 17→11 finding until evidence)

# Old ml-classifier TODO (kept for context, was the prior task on
# 2026-06-14 — that work is fully merged to main; see
# docs/findings/ml-work-session-2026-06-14.md and the ml-classifier
# branch that's now merged)
