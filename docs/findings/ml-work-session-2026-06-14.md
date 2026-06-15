# Session record — ML review, distillation, shadow classifier (2026-06-14)

A single-day arc that started as a review of the `ml-classifier` research
branch and ended with two production changes shipped and the research
consolidated onto `main`. This is the index/narrative; the deep detail
lives in the linked findings docs.

## TL;DR — what changed in production

1. **Removed the `overnight_cooling` veto** (`config.py`:
   `MAX_OVERNIGHT_CLOUD_COVER_PCT` 95 → 100). It was an 89%-false-positive
   SOFT veto; removing it improves replay Peirce/cost/accuracy in aggregate
   (McNemar p=0.358 — *not* significant, so framed as "drop a demonstrably
   bad veto", not a proven win). Reversible. Commit `dd638a9` (merge).
2. **Shipped a shadow ML classifier** — a distilled logistic run alongside
   the rules: logged on every forecast and shown as an "experimental" card
   on the dashboard, but it **never feeds `overall`**. Pure-Python scorer,
   no new prod dependencies. Commit on `main` via merge `023cd03`.
3. **Merged the `ml-classifier` research branch to `main`** so the spike,
   distillation, CLI, and writeups are no longer stranded on a branch.

The 14-rule + severity-tiered aggregator **remains the production
classifier**. No learned model drives the verdict.

## The arc

### 1. Review of the branch's ML work
Reviewed the four `ml-classifier` commits (ceiling spike + cost sweep +
feature-set cleanup). Code was sound; the problems were doc-rot in the
writeup: stale cost numbers, an over-broad "better on every metric" claim
(true only under Bayes-optimal thresholding, not argmax), and a 6-vs-8
feature-count inconsistency. Fixed those, added two anti-leakage tests.
Commits `d37b069`, `e302998`, `1713594`.

### 2. Phase D — distillation as ML-as-oracle (no model ships)
Reframed Phase D from "GBDT → rule list" to using the model as a research
instrument that surfaces rule/threshold hypotheses, validated through the
existing replay-calibration gate. Three cuts:
- **Cut 1** (`ml-distill-cut1-2026-06-14.md`): the fire/no-fire decision is
  **linear** — the edge is threshold mis-placement, not exotic structure.
  Cross-checking coefficients against raw correlations rejected 2 of 4
  surprising signs as collinearity artifacts (the classic trap).
- **Cut 2** (`ml-distill-cut2-2026-06-14.md`): interaction ablation. HGB's
  edge is **real but lives in GO-vs-MAYBE strength grading**, not the veto
  decision (full≈linear on binary thermal; +0.088 Peirce from interactions
  only on 3-class). So there is **no conjunctive rule to harvest** — the
  ML-only edge is an architecture question, not a rule.
- **Cut 3** (`ml-distill-cut3-2026-06-14.md`): replay-gate validation. The
  rule layer **over-vetoes systematically** (cost worse than always-GO).
  `thermik` (worst, 1077 FP) is a cost/skill tradeoff; `overnight_cooling`
  was the one clean Pareto improvement → shipped (see TL;DR #1).

### 3. Ship-decision review (the validation journey)
On "should an ML model ship," the analysis matured through several
validation schemes — this is the key intellectual record:
- Aggregate: HGB +0.142 Peirce over the rule, significant.
- **Per-year** breakdown: the edge is not uniform; HGB *collapses on 2026*.
- **Expanding-window TS-CV** (forward-realistic): training on recent ICON
  years lifts 2024/2025, but on 2026 both models still **lose to the rule**.
- **Leave-one-year-out** (max-data ceiling): the **logistic** beats the
  rule in **9/10 years** (mean Peirce +0.215 vs +0.114) and is far more
  stable than HGB — but 2026 (n=73, in-progress) is the lone holdout, and
  LOYO can't rescue it (it's the last year — no future to borrow).

**Conclusions:** (a) if anything ships, it's the **logistic, never HGB**;
(b) the only open question is whether the 2026 dip is noise or a regime
shift, and the only honest way to resolve it is with *live* data. Hence:

### 4. Shadow ML classifier (design → build → ship)
`ml-shadow-classifier-design-2026-06-14.md`. A 3-class multinomial
logistic, **distilled to ~69 floats** scored in pure Python (no
sklearn/numpy/pandas in prod — verified to reproduce sklearn exactly,
0/1912). Attached at serialize time so it is structurally incapable of
touching `overall`; logged per day; shown as an experimental dashboard
card (verdict + class %s + top-3 contributions + DE/EN, "not the official
verdict"). Shadow mode is *how we find out* if the edge holds on 2026.

## Repository state after this session (`main`)

- **Production rule change:** `overnight_cooling` veto disabled.
- **Shadow classifier (live):** `src/oracle/ml_classifier.py` +
  `knowledge/ml_coeffs.py` (frozen 69 floats) + dashboard card + the
  `ml_classifier` block in every new record.
- **Research (merged, not deployed):** `src/oracle/ml/*` (dataset/train/
  evaluate), the `oracle ml train|evaluate` CLI behind the `[ml]` extra
  (absent from both Dockerfiles → import-guarded, prod-safe),
  `scripts/{cost_ratio_sweep,tune_ml,export_ml_coeffs}.py`, and the
  findings docs (`ml-classifier-2026-06-13`, `ml-research-2026-06-13`,
  `ml-distill-cut{1,2,3}`, `ml-shadow-classifier-design`).

## Operations

**Retrain the shadow model** (e.g. after the 2026 season completes):
```
uv pip install -e ".[ml]"
python scripts/export_ml_coeffs.py --csv data/replay_full.csv
pytest tests/test_ml_classifier.py   # golden vector flags the coefficient change
```
Commit the regenerated `knowledge/ml_coeffs.py` (one change per commit).

**Promote past shadow** (only if 2026+ ground truth shows the logistic
tracks/beats the rule on the growing live sample): that's a deliberate
future decision — it would let the score (with a per-rider threshold)
influence `overall`, which the current shadow-invariant test forbids.

**Roll back overnight_cooling**: set `MAX_OVERNIGHT_CLOUD_COVER_PCT` back
to 95 (the veto is fully reversible).

**Deploy reminders** (this session's deploys followed the runbook):
push to `main` → re-pin both Cloud Run jobs to `oracle-job:latest` →
bounce the dashboard → `oracle forecast` to populate today. Rule/threshold
changes also need `oracle rescore` on the prod bucket; the shadow
classifier does not (it only adds a block to future records).

## Open items
- The **2026 shadow log** accrues automatically; revisit the promote
  decision when the season is complete and n is no longer 73.
- `foehn_delta_hpa` **inverted-U** (Cut 2): a candidate non-monotonic rule,
  unproven — needs the replay gate.
- **Aggregator-level veto-aggressiveness / graded strength**: the structural
  lever both Cut 2 and Cut 3 point at; bigger than one-threshold, scope
  deliberately.
- Merged branches `tune-overnight-cooling` and `feat-shadow-ml-classifier`
  can be deleted; `ml-classifier` is now merged.
- **2026-06-15 follow-up**: re-tested the 11→13 feature restriction that
  motivated the shadow. Branch `feat-icon-coverage-shadow` retrained the
  shadow on ICON-only data (2023-2026, n=715) with 13 features (11 stable
  + BLH + CAPE). Closes 26% of the 2026 dip and improves Peirce
  +0.0309 on the year-blocked within-ICON holdout. See
  `docs/findings/ml-icon-coverage-shadow-2026-06-15.md`. Not yet merged
  to main — user reviews first.
