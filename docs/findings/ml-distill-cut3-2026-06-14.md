# Phase D · Cut 3 (started) — replay-gate validation of Cut 1 hypotheses (2026-06-14)

**What this is.** The validation gate for Cut 1's findings
(`ml-distill-cut1-2026-06-14.md`): run the candidates through
`oracle calibrate --replayed` (offender list) and the
config-change → `rescore --replayed` → `calibrate --replayed --resimulated`
loop to see whether they actually improve the rule layer. **No production
threshold changed — every config edit was reverted; `config.py` is clean.**
Replay data (`data/runs/replay/`, gitignored/regenerable) was restored to
the production-config rescore at the end.

## The offender list (diagnostic, n=1912 ground-truthed days, 68 storm days quarantined)

`oracle calibrate --replayed` (as-written verdicts):

| rule | vetos | FP-veto | read |
|---|---:|---:|---|
| `thermik` | 1150 | **1077** | worst over-vetoer; 93.7% of its NO_GOs killed a real session |
| `dew_point_spread` | 880 | 801 | heavy over-veto (rule *direction* is right per Cut 1 — threshold too aggressive) |
| `solar_radiation` | 877 | 784 | heavy over-veto |
| `daytime_clouds` | 709 | 648 | over-vetoes (**corrects Cut 1's "tighten" → loosen**) |
| `overnight_cooling` | 478 | 424 | over-vetoes |
| `boundary_layer_height` | 61 | 49 | minor |
| others (`upper_level_wind`, `atmospheric_stability`, `air_lake_delta`, `foehn_override`, `synoptic_override`, `thermal_ignition`) | ≤47 | ≤41 | rare-event guardrails — leave |

FP-veto = rule said NO_GO but actual was GO/MAYBE (killed a real session).

**Meta-finding:** the rule layer over-vetoes *systematically* — its mean
cost (0.450 historical / 0.535 resimulated) is **worse than the "always
GO" constant** (0.263). It has positive discrimination skill (Peirce
+0.06) but pays for it with expensive false vetos. This is the same gap
the ML exploits under Bayes-optimal thresholding.

## Experiment 1 — `thermik` (worst offender)

Resimulated baseline (current rule layer): **Peirce +0.063, cost 0.535,
acc 44.0%**. Sweep `MIN_THERMIK_DELTA_HPA` (veto fires below it):

| threshold | Peirce | cost (r=2) | accuracy | thermik vetos |
|---|---:|---:|---:|---:|
| **−1.0 (production)** | **+0.063** | 0.535 | 44.0% | 1150 |
| −3.0 | +0.050 | 0.507 | 44.8% | — |
| −5.0 | +0.050 | 0.504 | 44.9% | — |
| −99 (veto off) | +0.051 | 0.503 | 45.0% | 0 |

**Verdict: not a clean ship — a cost/skill tradeoff.** Loosening thermik
monotonically improves cost (0.535 → 0.503) and accuracy (+1 pp) but drops
Peirce/Heidke skill (+0.063 → +0.050), and the drop is immediate (even
−3.0 pays it in full — no Pareto sweet spot). The 1077 false vetos are
expensive under the missed-session-weighted cost, but thermik's handful of
correct no-go catches do carry real discrimination. **Whether to loosen it
is a per-rider-cost call** — exactly the knob the ML cost-story and
`scripts/cost_ratio_sweep.py` already frame. Confirms Cut 1's "do not flip
blind": the inversion is real on cost, but skill says thermik isn't pure
noise.

## What this means for the plan

- **Single-threshold tuning reproduces the project's central tension**
  (skill vs cost), it doesn't dissolve it. No single Cut 1 candidate is a
  free Pareto win on the replay gate.
- The systematic over-veto (whole layer costlier than always-GO) points at
  an **aggregator-level** lever — veto aggressiveness / turning hard soft-
  vetos into graded contributions — more than at any one threshold. That's
  a bigger change than "one threshold per commit" and should be scoped
  deliberately, not slipped in.
- **Cost-ratio dependence is now empirical for the rules too**: a rider who
  weights missed sessions heavily (high r) would loosen thermik + the cloud
  vetoes; a rider who hates wasted drives (low r) keeps them. This is the
  per-rider-config conversation the ship/no-ship call already deferred.

## Experiments 2–3 — cloud / dew / solar loosening (each isolated from baseline)

Each candidate loosened independently (config reverted between runs):

| candidate | change | Peirce | cost | acc | verdict |
|---|---|---:|---:|---:|---|
| *baseline* | production | +0.063 | 0.535 | 44.0% | — |
| `daytime_clouds` | 75→88 | +0.055 | 0.533 | 43.8% | loosening **hurts** — leave |
| **`overnight_cooling`** | **95→100 (off)** | **+0.072** | **0.517** | **45.1%** | **Pareto win** |
| `dew_point_spread` | 2.5→1.5 | +0.050 | 0.513 | 44.5% | cost/skill tradeoff |
| `solar_radiation` | 380→300 | +0.061 | 0.533 | 44.0% | neutral |

### `overnight_cooling` — the one clean win
Removing this SOFT veto improves **all three** metrics simultaneously
(Peirce +0.063→+0.072, cost 0.535→0.517, acc 44.0→45.1%). It fired 478
vetos, **424 false-positive**. Mechanism note: intermediate thresholds
(97, 98) improve only *cost* (0.531, 0.530), not Peirce — the skill gain
appears only at full removal (100). Because it's a SOFT veto under
`SOFT_VETO_BAR=2`, it only ever changed a verdict when it was the **2nd**
soft veto tipping a day down; so loosening the threshold is the wrong
lever — the rule's veto is net-harmful and should be **removed**, not
re-tuned. The predictive content of overnight cloud lives in the 50–71%
mid-range (thermal mean 52% vs no-go 71%), not the >95% tail the rule
actually vetoes — same "veto fires in the wrong place" pattern as the
cloud finding in Cut 1/§2.

**This is the single distillation result that touches production — and it
is a *weak* positive, not a proven win.** Prepared on branch
`tune-overnight-cooling` (off `main`, commit `8c9b8d5`): threshold 95→100
(disabled via the never-fire idiom matching FOEHN/SYNOPTIC), plus the
`tests/test_rules.py` update.

**McNemar (paired, baseline-95 vs disabled-100, same days): NOT
significant.** 58 discordant days — 33 baseline-wrong→right, 25
right→wrong, net +8 — **p = 0.358** (χ², cont.corr.). The aggregate gains
(Peirce +0.009, cost −0.018, acc +1.1 pp) are **directionally favorable but
within noise**. Robust facts: the 89% false-veto rate and the
flat-to-better aggregates; the per-day improvement is not statistically
distinguishable from chance.

**Reframed recommendation:** "remove a demonstrably bad veto that doesn't
hurt, marginally helps, and simplifies the layer" — *not* "ship a
significant accuracy improvement." Defensible to merge (the veto is
miscalibrated and removal is reversible), but the call is the user's with
the non-significance in view. Not merged to `main`.

### The others
- `daytime_clouds`: loosening *hurts* Peirce — its 75% veto is closer to
  right than the binary surrogate's 87.5 split suggested (the surrogate was
  fitting fire/no-fire; the soft-veto-in-aggregate context differs). Leave.
- `dew_point_spread`: same cost/skill tradeoff shape as thermik.
- `solar_radiation`: neutral — the 380 threshold is fine; its over-veto
  count is mostly redundant with cloud.

## Still open
- [~] `overnight_cooling` removal PREPARED on `tune-overnight-cooling`
      (`8c9b8d5`, off main); McNemar p=0.358 (not significant) → merge is a
      user judgment call (weak positive, reversible), not yet on `main`.
- [ ] `foehn_delta_hpa` inverted-U (Cut 2): needs a *non-monotonic* rule
      (low-Δ caution + existing high-Δ veto), not a single-threshold test.
- [ ] Aggregator-level lever: the systematic over-veto + the soft-veto
      tipping mechanism both point at veto-aggressiveness / graded signals
      as the bigger structural opportunity (= the strength-grading edge
      Cut 2 isolated). Architectural; scope deliberately.

Cut 2 (interactions) is complete — see `ml-distill-cut2-2026-06-14.md`.

## Reproduction
```bash
oracle calibrate --replayed                       # offender list (as-written)
oracle rescore --replayed && \
  oracle calibrate --replayed --resimulated        # resimulated baseline
# edit MIN_THERMIK_DELTA_HPA in config.py, then:
oracle rescore --replayed && \
  oracle calibrate --replayed --resimulated        # measure the change
# revert config.py + rescore --replayed to restore production state
```
