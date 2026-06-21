# Ignition-timing spike — can we forecast *when* the thermal fires? — 2026-06-21

**Motivation.** The 14-rule forecaster answers *whether* the thermal fires, not
*when*. A rider waited out a dead morning on 2026-06-21 (oracle said NO_GO:
counter-gradient + convective instability), drove home, and the thermal ignited
~15:00. The question: can we predict an **ignition band** (early / midday / late)
or onset time from day-ahead data?

Answer: **no, not at useful precision.** Documented here so we don't re-spike it.

## Ground truth

`ground_truth.machine.first_ignition_at` exists on 3 287 historical days, but it
is the **first 8 kt crossing in the full 24 h** — overnight synoptic/frontal wind
poisons it (onsets at 00:08, 23:19…). The right label is the **sustained daytime
onset**: `calibration._sustained_onset_minute(samples, 8 kt, run=3)` — start of
the first ~30-min ≥8 kt run — restricted to true thermal **sessions**
(`actual_verdict_thermal ∈ {go, maybe}`; note it returns `"no_go"`, *not* `None`,
for non-thermal days — filtering on `is None` lets every night-wind day through).

Warm-season (Apr–Oct) thermal sessions: **1 103** over 2016–2026, **653** for
2021+ (where the historical-forecast archive carries intraday meteo).

## Stage 0 — early/midday/late band from morning aggregates

`src/oracle/research/ignition_timing.py`: signed "lateness" score from the
existing snapshot (gradient dominant → counter-gradient delays; + instability,
midday low cloud, weak solar/BLH earlier-pulls). Validate with
`scripts/validate_ignition_timing.py [--replayed]`.

- Bands are **monotonic** (early < midday < late mean onset) on every slice.
- But **weak**: Spearman(score, onset) = **+0.17** on the full 10-year sample
  (the rosy +0.45 on 34 live 2026 days was small-sample optimism).
- 78 % of ≥14:00 sessions flagged `late` in advance — high recall, low precision
  (the `late` band fires on ~⅔ of all days).

## Stage 1 — does the *intraday shape* carry the timing signal?

Hypothesis: onset lives in *when* solar crosses ignition levels / the low-cloud
deck clears / the surface heats — the hourly arrays `meteo.py` collapses to
10:30–15:00 maxima and discards. `src/oracle/research/intraday_timing.py`
extracts those crossing-times; `scripts/intraday_timing_spike.py` compares
feature sets under **leave-one-year-out CV** (Ridge) at predicting onset minute,
on 653 sessions (2021+). Intraday meteo (solar, low cloud, temp, lifted index)
is fetched fresh from the historical-forecast API; **BLH is null pre-2025** and
was excluded. Stage-0 aggregates are derived from the *same* fetched payload
(the 2021–25 main records are ground-truth-only stubs — real archived inputs
live in the replay record; reading the stubs gives all-null inputs and a bogus
+0.51, a trap fixed mid-spike).

| feature set | Spearman | MAE (min) |
|---|---|---|
| naive (predict mean onset) | — | **73.3** (floor) |
| gradient-only | +0.182 | 71.9 |
| Stage-0 heuristic (hand-weighted) | +0.188 | — |
| Stage-0 aggregates (Ridge) | +0.297 | 69.3 |
| **Stage-1 intraday (Ridge)** | **+0.219** | 70.5 |
| combined (Ridge) | +0.314 | 69.3 |
| combined (HGB, nonlinear) | **+0.347** | **67.3** |

## Conclusion — no-build

1. **The Stage-1 hypothesis is falsified.** Intraday features (+0.219) are
   *worse* than the morning aggregates (+0.297); combined barely moves over
   Stage-0 alone. The little predictable signal is already in the aggregates
   (mostly the gradient). The timing is **not** hiding in the intraday shape.
2. **The ceiling is too low to ship.** The best model (HGB, all 15 features)
   beats "guess the average" by **6 minutes** of MAE (67.3 vs 73.3) against an
   onset std of 93 min — typically wrong by **over an hour**. You cannot tell a
   rider "come at 13:30 not 11:00" with a ±67-min predictor.
3. **Open door (small):** BLH — the feature most physically tied to when the
   mixed layer deepens enough to fire — is unavailable historically, so its
   contribution is untested. Live records carry it from 2025+; revisit only if a
   future season accumulates enough BLH-complete days, and only expecting a
   marginal lift, not a bridge from ±67 min to useful.

**Recommendation:** do not wire intraday timing into prod. If any hint is ever
wanted, a Ridge fitted on the *existing* morning aggregates (+0.297, no new data
path) beats the hand-weighted Stage-0 heuristic (+0.188) — but even that is a
coarse tendency, not a clock. Artefacts kept on branch `ignition-timing-stage1`
so the spike can be re-run after future threshold/feature changes.
