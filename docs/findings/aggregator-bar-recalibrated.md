# Aggregator bar, recalibrated under the corrected methodology — 2026-06-13

Phase 4 of the methodology rework. Re-evaluates `SOFT_VETO_BAR` (raised 2→5 in
the prior pass, commit 743e610) under the fixed methodology: thermal-character
label, Apr–Oct only, scored by skill/cost with McNemar + era splits. Companion
to `aggregator-bar.md` (the prior pass's version) and `../fable_findings.md`.

## Corrected baseline (in-season replay, current rules, n=1912)

Skill barely clears a constant under *any* label — and collapses under the
clean one:

| label | system acc | best constant | Peirce |
|---|---|---|---|
| peak | 53.5% | always-GO 53.4% | +0.030 |
| duration | 47.9% | always-GO 47.8% | +0.021 |
| **thermal** | **28.0%** | **always-NO_GO 44.5%** | **+0.006** |

Under the thermal label the rules forecast GO on 1809/1912 days (95%) while real
thermal sessions are 27% of days. The ruleset is wildly over-optimistic and has
essentially zero skill. This is the review's thesis (§1, §3), quantified.

## Bar sweep (thermal label, re-aggregated from stored verdicts)

| bar | Peirce | cost | acc | GO | MAYBE | NO_GO |
|---|---|---|---|---|---|---|
| 1 | +0.025 | 0.509 | 30.2% | 150 | 1715 | 47 |
| 2 | +0.020 | **0.502** | 29.4% | 946 | 919 | 47 |
| 3 | +0.020 | 0.520 | 29.2% | 1345 | 520 | 47 |
| 4 | +0.018 | 0.552 | 28.9% | 1619 | 246 | 47 |
| **5 (current)** | **+0.006** | 0.580 | 28.0% | 1809 | 56 | 47 |
| 6+ | −0.001 | 0.593 | 27.6% | 1865 | 0 | 47 |

The current bar=5 sits at the low-skill end. Lower bars look better in
aggregate (best cost at bar=2, best Peirce at bar=1).

## But the change is within noise, and era-unstable

- **McNemar bar5 → bar2 (thermal, n=1912):** fixed 215, broke 188, net +27 of
  403 discordant, **p = 0.20 — not significant.** The aggregate gain doesn't
  survive a paired test.
- **Per-era optimum disagrees:** IFS cheapest at bar=2 (Peirce +0.039); ICON
  cheapest at bar=1, with bar=2 actually *negative* (−0.012). The two model eras
  don't agree on the bar — the same instability the prior era split showed.
- **Structural ceiling:** the soft-veto bar only moves GO↔MAYBE. NO_GO is fixed
  at 47 days (it comes solely from HARD vetoes) regardless of bar, so no bar
  value can approach the 44.5% NO_GO base rate. The bar cannot fix the
  over-optimism; it only redistributes GO vs MAYBE.

## Conclusion

1. The prior pass's 2→5 change was justified on the contaminated peak-label
   accuracy metric; under the corrected thermal label it lands at near-zero
   skill and McNemar does not support 5 over 2 (or vice versa).
2. **Recommended:** revert `SOFT_VETO_BAR` 5 → 2 — undo a change whose only
   justification was the bad metric, and restore meaningful MAYBE hedging
   (919 vs 56 days) that a forecast product wants. Flagged honestly as
   *within noise and era-unstable*, not a fitted optimum.
3. The deeper finding is that **threshold/bar tuning has a low skill ceiling
   here**: the rules over-forecast GO and can't produce enough NO_GO to match a
   thermal label. The high-leverage work is rules that generate NO_GO on
   non-thermal days (or the ML approach, GH #12) — not further soft-veto tuning.

## Repro

Throwaway analysis (not committed): iterate `_iter_window_days(replayed=True,
months=ACTIVE_SEASON_MONTHS)`, merge ground truth, label `thermal`, re-aggregate
`verdicts_resimulated` at each candidate bar, score with `peirce_skill_score` /
`mean_cost`, and `mcnemar` the bar5 vs bar2 correctness vectors.
