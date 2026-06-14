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

## Still open (next gate runs)
- [ ] Experiment 2 — loosen `daytime_clouds` / `overnight_cooling`
      (Cut 1 #2, corrected direction): do they show the same cost/skill
      tradeoff or a cleaner win? One change per commit.
- [ ] Experiment 3 — `dew_point_spread` / `solar_radiation` threshold
      (both over-veto ~800 FP with correct direction): how far can the
      threshold relax before skill drops?
- [ ] Cut 2 — interactions (surrogate tree / SHAP on HGB): the linear +
      single-threshold story leaves HGB's +0.142 Peirce edge unexplained.

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
