# Phase D · Cut 2 — interactions: where is HGB's edge? (2026-06-14)

**What this is.** Cut 1 showed the linear model's view; Cut 3 showed
single-threshold tuning is a cost/skill tradeoff. Cut 2 asks the question
that decides whether distillation should produce **conjunctive rules** or
just **threshold tweaks**: *is HGB's celebrated edge driven by feature
interactions, or by non-linear univariate transforms?* Method is a
dependency-free **interaction ablation** (`shap` isn't installed; not
needed) — compare three models on the same year-blocked split, identical
where possible:
- **linear** — multinomial logistic (no interactions, no non-linearity)
- **additive HGB** — `interaction_cst='no_interactions'`, every other
  param identical to the project's HGB (non-linear per-feature, but NO
  cross-feature interactions)
- **full HGB** — the project's `fit_hgb` config (interactions allowed)

If full ≫ additive → interactions are real. If full ≈ additive ≫ linear →
the edge is non-linearity, not interactions. **Read-only; no model or
rule changed.**

## Result 1 — the interaction edge is real on 3-class, ABSENT on binary

3-class (go/maybe/no_go), 715-day ICON test:

| model | acc | Peirce | Heidke |
|---|---:|---:|---:|
| linear (no interaction) | 0.448 | +0.158 | +0.157 |
| additive HGB (no interaction) | 0.421 | +0.120 | +0.118 |
| **full HGB (interactions)** | 0.488 | **+0.208** | +0.209 |

Binary thermal (GO/MAYBE-fired vs NO_GO), same test:

| model | acc | Peirce | TPR | FPR |
|---|---:|---:|---:|---:|
| linear | 0.642 | +0.286 | 0.633 | 0.347 |
| additive HGB | 0.613 | +0.217 | 0.643 | 0.427 |
| **full HGB** | 0.649 | **+0.288** | 0.683 | 0.395 |

**The headline:**
- On **3-class**, interactions add **+0.088 Peirce** (full +0.208 vs
  additive +0.120 — identical params, only cross-feature interactions
  toggled). HGB's celebrated edge over the rule **is genuinely
  interaction-driven**, not just non-linearity (additive HGB is actually
  *worse* than linear, +0.120 < +0.158 — the per-feature non-linear shapes
  overfit n=1197 without either linear regularisation or interactions).
- On **binary thermal**, full HGB ≈ linear (**+0.288 vs +0.286**) — the
  interaction headroom **disappears**.

Binary pools GO+MAYBE vs NO_GO; the only thing it drops relative to
3-class is the GO-vs-MAYBE split. So **HGB's interaction edge lives in the
GO-vs-MAYBE strength distinction — NOT the fire/no-fire veto decision the
rule layer actually makes.**

### Why this matters for distillation
- For the **fire/no-fire decision** (what the veto rules drive): no
  interactions to harvest — it's linear. **Cut 1's coefficient + threshold
  story is the complete distillation** for the veto question.
- HGB's interaction edge requires the model to grade **session strength**
  (strong GO vs marginal MAYBE), which the veto-based architecture doesn't
  finely express. Capturing it means emitting a **graded strength score**,
  not adding conjunctive veto rules — an architectural change (ties to the
  Cut 3 aggregator-level meta-finding), not a one-rule commit. This
  **reinforces the writeup's ship/no-ship call**: the real ML edge isn't
  shippable as a rule without changing what the rule layer outputs.

## Result 2 — surrogate tree confirms "mostly univariate" for the fire call

Depth-3 tree distilling full HGB's binary predictions (fidelity:
train 0.69 / test 0.77 — lossy, as a small tree must be):
```
daytime_low_cloud ≤ 87.5
├─ thermik_delta ≤ −0.85 → THERMAL    (both pressure children agree)
└─ thermik_delta > −0.85
   ├─ dew_spread ≤ 2.55 → THERMAL
   └─ dew_spread > 2.55 → no-thermal
daytime_low_cloud > 87.5 → no-thermal (all children agree)
```
Root split is univariate (`daytime_low_cloud ≈ 87.5`), and most sibling
leaves predict the **same** class — i.e. the tree found dominant
*univariate* structure with weak conjunctions, corroborating "the fire
decision is ~linear." Two incidental confirmations: heavy daytime cloud
(>87.5) is a near-deterministic killer (but the rule vetoes at 75 — too
low), and `thermik_delta ≤ −0.85 → thermal` again shows the Cut 1
inversion.

## Result 3 — one non-linear univariate signal the rules + linear both miss

Permutation importance (full HGB, binary) ranks `foehn_delta_hpa` **#2**
(+0.021) despite its ~0 linear coefficient in Cut 1. Reason — its
thermal-fire rate is **inverted-U**, not monotone:

| `foehn_delta_hpa` (Bolzano−Innsbruck) | n | fire rate |
|---|---:|---:|
| (−∞, −3] | 283 | 0.35 |
| (−3, −1] | 459 | 0.63 |
| (−1, +1] | 321 | **0.64** |
| (+1, +3] | 93 | 0.58 |
| (+3, +∞] | 41 | 0.39 |

Fire peaks mid-range and falls at **both** extremes. A linear term can't
see this (the slopes cancel → ~0 coef); an additive non-linear model can.
The current Föhn rule only vetoes the **high-positive** extreme
(`FOEHN_TRIGGER_DELTA_HPA=10`); the **low-negative suppression** (fire 0.35
at Δ ≤ −3) is unmodeled. Candidate for a non-monotonic "Δ out of mid-range
→ caution" rule. **Caveat:** additive HGB (which captures this) scored
*below* linear overall, so this must clear the replay gate before
shipping — it may not survive once the rest of the layer is held fixed.

## Net verdict for Phase D

- **Shippable as rules (via Cut 3 gate):** Cut 1's threshold story — the
  fire/no-fire decision is linear, so threshold placement (thermik, cloud,
  dew/solar) is the whole harvestable signal, and Cut 3 shows each is a
  per-rider cost/skill tradeoff, not a free win.
- **Candidate, needs gate:** the `foehn_delta_hpa` inverted-U (a
  non-monotonic rule, not an interaction).
- **NOT shippable as a rule:** HGB's interaction edge (+0.088 Peirce on
  3-class) lives in GO-vs-MAYBE strength grading. Distilling it means
  changing the rule layer into a graded-strength model — the architectural
  move the ship/no-ship call already flagged, not a conjunctive veto rule.

**Conclusion:** distillation does **not** yield a clean new conjunctive
rule to bolt onto the veto layer. It confirms (a) the veto decision is
linear and already mapped by Cut 1, and (b) the genuinely ML-only edge is
strength-grading, which is an architecture decision, not a rule. Cut 2
closes the "is there hidden interaction structure to harvest?" question:
**not for the decision the rules make.**

## Reproduction
`/tmp/cut2.py` + `/tmp/cut2b.py` this session, or:
```python
from sklearn.ensemble import HistGradientBoostingClassifier as HGB
common=dict(max_iter=200,learning_rate=0.05,min_samples_leaf=20,class_weight='balanced',random_state=42)
full=HGB(**common); add=HGB(interaction_cst='no_interactions',**common)  # toggle interactions only
# fit both on the binarise_thermal / 3-class target over split_by_year(train≤2022,test≥2023)
```
