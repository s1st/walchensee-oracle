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

# Phase C — Ceiling spike (ML training + evaluation)

Goal: replace the Phase B `train` stub with the actual training loop, add `oracle ml evaluate` for the head-to-head scoring, and answer "does the rule baseline's +0.107 Peirce represent the data ceiling?"

- [x] Lock the design (PLAN.md above)
- [x] `src/oracle/ml/__init__.py` — package marker
- [x] `src/oracle/ml/dataset.py` — `load_replay_csv`, year-blocked split, label encoding
- [x] `src/oracle/ml/evaluate.py` — RPS, Brier+Murphy, expense-based relative value; reuse Peirce/HSS/McNemar from `calibration.py`
- [x] `src/oracle/ml/train.py` — `fit_logistic`, `fit_hgb`, optional `fit_tabpfn` (lazy import)
- [x] `cli.py` — replaced `train` stub body, added `evaluate` subcommand
- [x] `tests/test_ml_dataset.py` — load + split tests
- [x] `tests/test_ml_evaluate.py` — each metric unit-tested
- [x] `tests/test_ml_train.py` — fit + save + reload
- [x] `tests/test_ml_cli.py` — extended with end-to-end on synthetic CSV (12 tests)
- [x] `pytest` green (239/239, +45 from Phase C)
- [x] `ruff check` clean
- [x] `mypy src` — only the same 2 pre-existing errors (logger.py:101, calibration.py:582); no new errors
- [x] End-to-end smoke on synthetic data
- [x] Commit + push to `origin/ml-classifier`

## Phase C outcomes (to be filled in when run on the real bucket)

When the user runs `oracle ml train + oracle ml evaluate` on the real
`data/replay_full.csv`, the JSON report (`data/ml/<csv-stem>_report.json`)
will contain the headline numbers for the Phase E writeup. Until then,
the smoke runs on synthetic data show:
- HGB accuracy ≈ 0.92 (vs 0.88 for the synthetic ~70% baseline)
- HGB Peirce ≈ +0.815 (vs +0.799 for the baseline)
- HGB mean cost / day = +0.044 (vs +0.100 for the baseline)
- McNemar on the synthetic data is not significant (n too small) — on
  the real ~1,257-day ICON-era holdout it should be.
- Brier (binary) decomposition: BS = REL − RES + UNC + within-bin-var
  (the strict identity is approximate for continuous forecasts binned
  into K intervals, per the test).

# Phase E — Honest comparison writeup — DONE

Goal: write `docs/findings/ml-classifier-2026-06-13.md` (the empirical
writeup, distinct from the research doc) with the head-to-head numbers,
McNemar significance, and the ship/no-ship decision.

- [x] Writeup committed (88c1f9f, corrected in 00cedd4 / d37b069)
- [x] Headline numbers, cost-ratio sweep, McNemar, stale-baseline bug,
      per-rider cost framing, ship/no-ship call, reproduction block
- [ ] (Open gap) Per-era IFS/ICON breakdown is named in PLAN's scoring
      protocol but not in the writeup. The 11-feature re-run removed the
      era-boundary confound *by construction* (train/test share a feature
      distribution), so this is now a "nice-to-have" sanity check, not a
      blocker. Fold in if/when Phase D's analysis runs the breakdown anyway.

# Phase D — Distill — REFRAMED (ML-as-oracle, not model-ship)

Goal (reframed 2026-06-14): use the trained model as a **research
instrument** to surface rule/threshold structure the 14-rule layer
misses, then re-express it as ordinary rules/thresholds in
`knowledge/rules.py` + `config.py`. **This is fully inside "no model
ships"** — no sklearn in prod, no `model.predict()` on the serving path;
the model stays a branch artefact and only *rules* ship.

Hard constraint — distillation produces **hypotheses, not commits**:
every candidate must clear the project's existing validation gate
(`calibrate --replayed` → one change per commit → `rescore --replayed` →
re-calibrate, with the ≥10-day offender-list bar from CLAUDE.md). A rule
that only improves the 715-day ICON holdout is overfitting, not a ship.

Honesty caveat: distilling **HGB** is lossy — a small rule list can't
reproduce a 200-tree ensemble's full +0.142 Peirce. We harvest
*direction*, not the last decimal; the writeup must not imply the rule
layer inherited HGB's number.

Order of attack (cheap → heavy):
- [ ] **Cut 1 (logistic coefficients):** read the 11 signed standardized
      LR weights — which features carry the discrimination, and in which
      direction. Compare each against the current `config.py` threshold
      for that signal. Output: a table of (feature, LR weight sign/mag,
      current threshold, "does the rule use this signal consistently?").
      This alone likely explains most of the edge as *threshold
      mis-placement* → feeds the calibration backlog directly.
- [ ] **Cut 2 (interactions, only if Cut 1 leaves gain unexplained):**
      shallow surrogate tree (depth ≤3) and/or SHAP on HGB to find
      conjunctions the rules treat independently (e.g. "low Δp is
      forgivable *if* dew-spread AND LI both favorable"). Output:
      candidate conjunctive rules.
- [ ] **Cut 3 (validate + ship):** run each surviving candidate through
      the replay-calibration gate; ship only those that improve replay
      Peirce/cost without overfitting the ICON holdout. One change per
      commit.
