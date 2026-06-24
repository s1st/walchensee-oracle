# Decoupling the lifted-index storm veto from the verdict (2026-06-24)

**Branch:** `li-decouple-experiment`
**Hypothesis (Simon):** *"A thunderstorm day can still be a good thermal day right up
until the storm arrives."* So the LI ≤ −2 thunderstorm HARD veto may be too
aggressive: instead of forcing `NO_GO`, score the thermal on its merits and show
the storm risk as a separate Caution advisory.

## What changed

- `atmospheric_stability` no longer emits a HARD `NO_GO` on storm risk (LI ≤
  `MIN_LIFTED_INDEX`). It stays GREEN; only the high-LI "too stable" SOFT cap
  remains in the rule. The aggregator therefore never sees a thunderstorm veto.
- `is_storm_risk` is retained as the **single source of truth for the advisory**
  (dashboard Caution box + yellow storm border + calibration storm tally).
- Calibration **lifts the storm quarantine**: storm days are scored on thermal
  merit like any other day (`Report.quarantined_days` → `Report.storm_days`,
  populated but no longer excluded). `mcnemar` likewise stops excluding them.
- Dashboard: the Caution box now shows for **any** storm day regardless of the
  GO/MAYBE/NO_GO headline (was gated on `NO_GO`), reworded to "good thermal, but a
  storm is coming — watch the sky, come in early."

## The decisive ground-truth check

Of the **68 storm-suspected days** (LI ≤ −2) in the replay corpus, the actual
Urfeld peak label was:

| actual peak | days |
|---|---|
| GO | 45 |
| MAYBE | 21 |
| NO_GO | 2 |

The status-quo ruleset forecasts **NO_GO on all 68** (the HARD veto) — i.e. it is
"wrong" against the thermal outcome on **66/68 (97%)** days. The hypothesis holds:
the thermal almost always still fires before the gust front.

## Metrics (replay, Apr–Oct, resimulated current rule layer, peak label)

Clean same-rule-layer comparison (only the LI veto differs):

| View | n | Peirce | Accuracy | Mean cost |
|---|---|---|---|---|
| Prod status quo (veto + storm days **quarantined**) | 1912 | +0.072 | 45.1% | 0.517 |
| Veto, storm days **scored** (honest full corpus) | 1980 | +0.066 | 43.7% | 0.556 |
| **Decoupled, storm days scored (new)** | 1980 | +0.062 | **44.8%** | **0.520** |

The fair comparison is the bottom two rows (identical corpus). Decoupling:

- **Accuracy +1.1pp** (43.7% → 44.8%)
- **Mean cost −6.5%** (0.556 → 0.520) — the rider-relevant metric
- **Peirce −0.004** (+0.066 → +0.062) — base-rate-corrected, dips because we trade
  confidently-wrong NO_GOs for a spread of GO/MAYBE.

On the **storm-day subset alone** the effect is large: mean cost **1.63 → 0.60
(−63%)**, and 66/68 days now flag a rideable thermal instead of a blanket NO_GO,
at the price of **one** GO→actual-NO_GO over-promise — which is precisely what the
Caution overlay is there to cover.

The prod headline Peirce (+0.072) is slightly flattering: it excludes the 68 days
where the system is most confidently wrong. Scored honestly (full corpus), the
veto's Peirce is +0.066 and the decoupled rule is +0.062 — a wash on Peirce, a
clear win on cost and accuracy.

## Decision

Per the project's own rule — report Peirce / Heidke / cost together, never one
alone — the rider-relevant **cost** metric and **accuracy** both improve, the
base-rate-corrected **Peirce** is essentially unchanged (−0.004), and the change
makes the forecast *useful* on ~66 storm days a year that the blanket veto told
you to skip. The safety concern is handled by the (now always-on) Caution
advisory rather than by destroying the thermal verdict.

Open question for promotion to `main`: confirm the framing reads safely to users
(GO headline + prominent ⚡ Caution) and that no rider treats the GO as "no storm."
The single GO→NO_GO over-promise in the corpus is the case to keep watching.
