# Corrected-methodology rework — findings & state, 2026-06-13

Umbrella summary of the rework that responded to Fable's external review
(`docs/fable_findings.md`) of the 2026-06-12 threshold-tuning pass. This is the
"read this first" doc; the per-topic working notes live in `docs/findings/`.

---

## TL;DR

- The 2026-06-12 pass's reported gains were **measurement artifacts**: it
  optimised a metric an always-GO constant beats, against a label that measured
  "was it windy" instead of "was there a thermal", on a corpus ~40% out of
  season. Under honest measurement the pass's headline change (bar 2→5) was
  **within noise** and actually left the system slightly **worse than the
  original `main`**.
- Built the missing measurement machinery: a **thermal-character label**, an
  **Apr–Oct season restriction**, **skill + cost metrics** (a constant scores 0),
  and a **validation harness** (McNemar significance, year-wise CV, IFS/ICON era
  splits).
- The real problem was **structural**, not a threshold: the rules could not emit
  NO_GO on non-thermal days (NO_GO was bar-invariant), and the dominant
  non-thermal mode is **cloudy/low-sun days — not foehn** (which everyone,
  including the review, had focused on).
- Shipped fix: a **`no_insolation` HARD veto** (heavy daytime cloud AND low
  morning solar → NO_GO) + reverting **`SOFT_VETO_BAR` 5→2**. This is the only
  large, significant, era-stable improvement found across either pass.
- **Net result vs the original `main`: ~2.5× the skill** on the honest label
  (Peirce +0.043 → +0.107), significant overall and in both model eras.

---

## 1. What was wrong (the review, verified)

| # | Finding | Verified against code |
|---|---|---|
| 1 | Label measured **wind, not thermal** — foehn/frontal/synoptic wind counted as a "fired" thermal | `calibration.py` peak/duration labels bucket on Urfeld wind alone |
| 2 | **~40% of the corpus is out of season** (Nov–Mar); the product only serves Apr–Oct | no month filter existed in `_iter_window_days` |
| 3 | **3-class accuracy is beatable by a constant** — always-GO 49.5% > tuned 48.3% | `overall_accuracy` is plain diagonal-sum |
| 4 | All thresholds fit under `SOFT_VETO_BAR=2`, then the bar moved to 5 last | commit sequence on the branch |
| 5 | **No significance test, no holdout** | none existed |
| 6 | Doc-rot + a dead foehn detector + IFS/ICON model mismatch | all confirmed |

## 2. What was built (the methodology, all committed)

- **Skill + cost metrics** (`calibration.py`): Peirce (Hanssen–Kuipers) and
  Heidke skill scores — a constant forecast scores exactly 0 — plus an
  asymmetric **cost matrix** (a missed session weighted 2× a wasted drive). The
  text report shows the constant-forecast baselines so "always-GO beats us"
  can't hide again.
- **Season restriction** (`config.ACTIVE_SEASON_MONTHS`, `--season` default on
  `calibrate`): score Apr–Oct only.
- **Thermal label** (`actual_verdict_thermal`, `--label thermal`): the duration
  label gated on the two thermal signatures we can read off the buoy curve —
  **onset at/after the mid-day ignition window** and **coherent (not ragged)
  gusts**. No wind direction is used: the buoy doesn't report it and no usable
  lakeside station exists (researched 2026-06-13; see the memory note).
- **Validation harness** (`mcnemar`, `reports_by_year`, `reports_by_era`,
  `config.ICON_ERA_START`; CLI `--split year|era`, `--mcnemar`).

## 3. The metrics, and why they matter (reference)

**Why not plain accuracy?** Class imbalance makes it gameable. On the wind label
GO was the plurality (~50%) so "always GO" scored ~49.5%; on the thermal label
the balance flips and "always NO_GO" (~45%) becomes the cheap winner. Accuracy
rewards guessing the common class.

**Skill score** (Peirce/Heidke): subtracts off whatever the best *constant* guess
scores given the class frequencies, so a constant scores 0 regardless of which
class dominates. Scale −1…+1; **0 = no skill, ~0.1–0.3 = genuinely useful for
local weather** (it is *not* a percentage). For the binary case,
**Peirce = sensitivity + specificity − 1 = Youden's J** — the one-number summary
of an ROC operating point; the multiclass version relates to **balanced
accuracy** (mean per-class recall).

**Three distinct problems, three tools** (don't conflate them):

| Concern | Nature | Fixed by |
|---|---|---|
| One class dominates → accuracy gameable | class **frequency** imbalance | skill score |
| Some mistakes cost more (miss vs wasted drive) | **consequence** imbalance | cost matrix |
| Grading "wind" not "thermal" | wrong **target** | thermal label |

**Precision/recall/F1 apply too** and give the most diagnostic view — see §6.
The caveats: the classes are **ordinal** (calling a GO day MAYBE is a smaller
error than NO_GO), which nominal P/R ignore but the cost matrix captures; and the
aggregator emits a **discrete label**, so there's no score to sweep into a
PR/ROC *curve* — only a single operating point. The threshold-free ranking view
(AUC) was instead applied to the *inputs* (§5), which is what found the fix.

## 4. Key findings under honest measurement

- **Corrected baseline (thermal, in-season, n=1912):** the pre-fix ruleset had
  near-zero skill (Peirce +0.006) and forecast GO on 95% of days vs a 27% real
  thermal rate — a near-useless optimist.
- **The bar tune was noise:** bar 5→2 is within noise (McNemar p=0.20) and
  era-unstable (IFS prefers 2, ICON prefers 1). The soft bar only moves
  GO↔MAYBE; NO_GO is bar-invariant, so no bar value can supply the missing NO_GO.
  → `docs/findings/aggregator-bar-recalibrated.md`.
- **Soil/BLH data is an IFS-pin artifact, not a bug:** IFS doesn't model soil
  moisture / BLH; the IFS-pinned replay nulls both corpus-wide. The soil tune's
  "n=48" was a fragment. Those rules need an **ICON-only re-replay** before any
  re-tune. → `docs/findings/soil-moisture-replay-nulls.md`.

## 5. The structural fix (shipped, commit abd6948)

Feature-AUC ranking of what separates thermal from non-thermal days (in-season):

| feature | AUC | reading |
|---|---|---|
| daytime low cloud | **0.683** | strongest separator |
| overnight cloud | 0.663 | |
| thermik pressure delta | 0.653 | |
| morning solar | 0.344 | (low solar → non-thermal) |
| dew-point spread | 0.381 | |
| **foehn delta** | **0.391** | barely separates — *not* the lever |
| 700 hPa wind | 0.593 | |

Non-thermal days are **cloudy/low-sun**, not foehn — correcting both the prior
pass and the review's foehn emphasis. So the fix:

- **`no_insolation` HARD veto:** `daytime_low_cloud ≥ 70% AND morning_solar ≤ 400
  W/m² → NO_GO` ("no sun → no thermal"). HARD because no insolation physically
  precludes a thermal; either signal alone stays a SOFT hint. Thresholds fit on a
  **temporal holdout** (train ≤2022 / test ≥2023) minimising cost — it
  generalised: held-out Peirce **−0.012 → +0.066** (p=6e-8), cost-positive on
  both splits.
- **`SOFT_VETO_BAR` reverted 5→2** (restores MAYBE hedging; NO_GO now comes from
  the veto, not the bar).

→ `docs/findings/structural-insolation-veto.md`.

## 6. Is it better than `main`? (head-to-head, thermal label, in-season)

Main's *actual* verdicts were reproduced with main's own code (a throwaway git
worktree) and graded with the new tooling on the same 1,912 days:

| ruleset | Peirce skill | cost | note |
|---|---|---|---|
| 2026-06-12 pass (bar 5, loosened solar/cloud) | **+0.006** | 0.580 | *worse than the original* |
| **`main`** (original research-guess thresholds) | **+0.043** | 0.488 | |
| **new (shipped)** | **+0.107** | 0.491 | ~2.5× main's skill |

**new vs main, McNemar:** fixed **272**, broke 151, net **+121**, **p≈5e-9** —
significant overall and in both eras (IFS net +82 p=1.5e-6; ICON net +39
p=1.3e-3). On cost the two are ~tied (the new NO_GO calls trade wasted drives for
a few missed sessions ~evenly); the clear win is in **discrimination**.

**Per-class behaviour of the new ruleset** (thermal, in-season, n=1912):

| class | precision | recall | F1 | support |
|---|---|---|---|---|
| GO | 0.34 | 0.61 | 0.43 | 520 |
| MAYBE | 0.27 | 0.31 | 0.29 | 541 |
| NO_GO | **0.67** | **0.27** | 0.39 | 851 |

macro-F1 0.372, balanced accuracy 0.399, Peirce +0.107. Reading: GO is
low-precision/high-recall (over-fires, but catches most good days); **NO_GO is
high-precision/low-recall — the conservative veto by design** (accurate when it
fires, misses subtler duds).

**On the live project days (n≈47 since 2026-04-22):** consistent positive skill
on all three labels (thermal +0.156, duration +0.238, peak +0.158); vs the
verdicts actually shown that season, the new rules fixed 12 days and broke 4.
Small sample — a sanity check, not proof; the 1,912-day replay is the evidence.

## 7. What was deliberately *not* done

- **Per-threshold soft-veto sweep** (solar/cloud/dew/LI/foehn/cold-lake) —
  dropped; proven within-noise. Re-tuning those dials is a dead end.
- **Soil/BLH re-tuning** — blocked on an ICON-only re-replay (the IFS-pinned
  corpus nulls those fields).

## 8. What's next — the ML pitch (GH #12), in metric terms

The NO_GO row above (precision 0.67, recall 0.27) is a detector with headroom:
accurate when it fires, missing most of what it should catch. A hand-built rule
sits at **one** operating point. A **scored model** would expose the whole
**precision/recall curve**, letting you dial NO_GO recall up while watching
precision and pick the operating point by the **cost matrix**. The thermal label
is the training target; the feature-AUC ranking (cloud/solar/thermik on top) is
the feature shortlist. The label fix was the prerequisite — train on the old wind
label and a model would just learn "foehn = GO, winter = NO_GO".

## 9. Map

Commits on `threshold-tuning` (pushed, no PR yet):
- skill metric · season restriction · thermal label · validation harness
- soil precheck · recalibrated-bar analysis · structural diagnosis
- **`no_insolation` veto + bar revert** (the shipped behaviour change)
- doc-rot fixes + this summary

Findings docs: `docs/findings/{structural-insolation-veto, aggregator-bar-recalibrated, soil-moisture-replay-nulls}.md`.
Review: `docs/fable_findings.md`. Superseded prior summary: `docs/2026-06-12-historical-calibration-findings.md`.

Repro: `oracle rescore --replayed` then `oracle calibrate --replayed --resimulated --label thermal --split era --mcnemar`.

**Live behaviour change to watch:** the dashboard now shows **NO_GO on heavily
overcast, low-sun days** (intended — no sun, no thermal), where the old version
optimistically said GO.

## 10. Public-dashboard metric decisions (the back-and-forth)

Choosing what to *show the public* was a separate debate from how to *tune
internally*. The reasoning, so it isn't re-litigated:

- **"Doesn't always-GO only win 1/3 with three classes?"** No — a constant
  scores the **prevalence of the majority class**, not 1/3, because the classes
  are imbalanced. On the wind/peak label GO was ~50% of days → always-GO ≈
  49.5%. On the thermal label the balance flips and NO_GO (~45%) is the bar.
  The "1/3" intuition only holds for balanced classes.
- **Skill score (Peirce/Heidke): kept internal, *not* shown to the public.**
  It's the right A/B-comparison metric (imbalance-proof, a constant scores 0),
  but it's hard to explain/sell on a public page. Decision: keep it in the
  calibration tooling, and on the dashboard show the explainable substitute —
  a **naive-baseline line** ("accuracy 52% · always-GO would score 46%"). Same
  job (proves we beat a constant), no formula.
- **Public panel uses the `duration` label, not `thermal`.** On the thermal
  label the system scores ~40% vs always-NO_GO ~47% — i.e. plain accuracy on
  thermal would publicly show us *below a constant*. On `duration` the system
  beats its constant, and "was there ≥1 h of rideable wind" is the simplest
  thing to explain. So: **thermal = internal tuning target; duration = public
  display.** (The naive-baseline line only reads well on duration for the same
  reason.)
- **Precision/recall/sensitivity/specificity do apply** and are the most
  diagnostic view — the dashboard already shows sensitivity + specificity in the
  advanced panel. Note `Peirce = sensitivity + specificity − 1` (Youden's J), so
  it's the same toolkit. Caveats: the classes are ordinal (a GO→MAYBE miss <
  GO→NO_GO miss — captured by the cost matrix, not by nominal P/R) and the
  aggregator emits a discrete label (one operating point, no PR *curve* — that's
  the ML pitch, §8).
- **Why the public accuracy *dropped* on deploy (59% → 52%) and that's honest:**
  the old 59% came from the over-optimistic ruleset that almost never said
  NO_GO, which duration-accuracy rewards (specificity was 14%). The new rules
  trade a little catch-rate for the ability to say NO_GO — specificity 14% →
  50%, sensitivity 90% → 82%. Lower headline, genuinely more useful forecast.

## 11. Deployment to production (2026-06-13)

Merged `threshold-tuning` → `main` (fast-forward); the `dashboard-deploy-on-main`
and `job-build-on-main` triggers built both images. Steps + the gotcha (now in
CLAUDE.md → Deploy runbook):

1. Dashboard service auto-deployed by the build.
2. **Jobs re-pinned manually** — the build does not update Cloud Run *jobs*, so
   `oracle-forecast`/`oracle-backfill` were re-pointed to the new
   `oracle-job:latest` or they'd keep running old code.
3. **Prod bucket rescored** (`RUNS_BUCKET=… oracle rescore --since 2026-04-22`,
   55 records, backed up to `data/runs.prodbackup-20260613/` first) so the stats
   panel reflects the deployed rules.
4. Dashboard revision bounced to clear the 1 h stats cache.

Live stats, before → after:

| metric | before (old rules) | after (shipped) |
|---|---|---|
| Accuracy | 59% | 52% |
| Naive baseline (always-GO) | 48% | 46% |
| Sensitivity | 90% | 82% |
| Specificity | **14%** | **50%** |
