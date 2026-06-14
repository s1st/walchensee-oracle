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
- [x] **Cut 1 (logistic coefficients):** DONE 2026-06-14 →
      `docs/findings/ml-distill-cut1-2026-06-14.md`. Binary-thermal LR on
      the same split, standardized coefs cross-checked vs raw correlation
      (the raw-corr check rejected 2 of 4 surprising signs as collinearity
      artifacts — dew-spread + rained_yesterday; rule sign was right).
      Robust findings: (1) `thermik_delta_hpa` direction looks inverted vs
      the −1.0 soft-veto (headline; needs offender-list check, label-def
      caveat); (2) daytime(75)/overnight(95) cloud vetoes too lenient →
      tighten; (3) absolute pressure levels carry signal the delta-only
      rules discard. Linear edge ≈ threshold mis-placement; binary LR test
      acc 0.642 ≪ HGB → interactions unexplained, Cut 2 warranted.
- [x] **Cut 2 (interactions):** DONE 2026-06-14 →
      `docs/findings/ml-distill-cut2-2026-06-14.md`. Interaction ablation
      (linear vs additive-HGB `interaction_cst='no_interactions'` vs full
      HGB). Findings: (1) interactions add +0.088 Peirce on 3-class
      (+0.208 vs additive +0.120) — real and large; (2) but on BINARY
      thermal full≈linear (+0.288 vs +0.286) — so the interaction edge
      lives in GO-vs-MAYBE *strength*, NOT the fire/no-fire veto decision
      the rules make. Distilling it ⇒ a graded-strength model, not a
      conjunctive veto rule (architectural, = ship/no-ship call). (3) One
      non-monotonic univariate signal both rules+linear miss:
      `foehn_delta_hpa` inverted-U (fire peaks mid-range, drops at both
      extremes; rule only vetoes high-positive) — candidate for the gate.
      No clean new conjunctive rule to harvest.
- [~] **Cut 3 (validate + ship):** STARTED 2026-06-14 →
      `docs/findings/ml-distill-cut3-2026-06-14.md`. Offender list confirms
      systematic over-vetoing (rule layer cost 0.535 > always-GO 0.263).
      Exp 1 (thermik, worst offender, 1077 FP): loosening is a cost/skill
      tradeoff, NOT a clean ship — cost 0.535→0.503 but Peirce +0.063→0.050,
      no Pareto sweet spot. Reproduces the project's per-rider-cost tension.
      Also corrected Cut 1 #2: cloud vetoes OVER-fire (loosen, not tighten).
      Exp 2-3 (cloud/dew/solar loosen) done: daytime_clouds loosening hurts;
      dew/solar are tradeoff/neutral. ONE CLEAN PARETO WIN: removing the
      `overnight_cooling` SOFT veto (95→off) improves Peirce (+0.063→+0.072),
      cost (0.535→0.517) AND acc (44→45.1) together — 478 vetos, 424 FP, and
      it only ever mattered as the 2nd soft veto, so remove not re-tune.
      No production threshold changed; config.py reverted clean.
      Still open: SHIP overnight_cooling removal via a validated one-change
      commit (McNemar + overfit check, pending user sign-off); foehn_delta
      inverted-U needs a non-monotonic rule; aggregator-level veto-
      aggressiveness lever (the strength-grading edge Cut 2 isolated).
