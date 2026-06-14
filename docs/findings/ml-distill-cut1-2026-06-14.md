# Phase D · Cut 1 — logistic coefficients vs current thresholds (2026-06-14)

**What this is.** The first, cheapest distillation cut (see `TODO.md` →
"Phase D — REFRAMED"): read what the linear model leans on and lay it
next to the rule layer's `config.py` thresholds, to tell whether the ML
edge is *threshold mis-placement* (feeds the calibration backlog) or
*missing feature interactions* (escalate to Cut 2). **Read-only — no
`config.py` or `rules.py` change was made. These are hypotheses, not
commits; each must clear the replay-calibration gate (Cut 3) before any
ship.**

## Method

The shipped bundle's logistic is **multinomial** (3-class go/maybe/no_go),
whose per-class coefficients are treacherous to read in isolation. The
rule baseline's anchor is **binary** thermal (GO/MAYBE = fired vs NO_GO),
so I fit a binary-thermal logistic on the *same* year-blocked train split
(≤2022, n=1197; `class_weight='balanced'`, median-impute → standardize →
LR), and read its standardized coefficients. Positive = pushes toward
**thermal fired**. Binary LR **test accuracy = 0.642** on the 715-day
ICON holdout — modest; the linear story is only partial (the HGB
interaction edge is what Cut 2 chases).

**Every coefficient is cross-checked against the univariate raw
correlation with the label.** A standardized LR coefficient that flips
sign vs the raw correlation is a collinearity/suppression artifact, not a
real per-feature direction — reading it as a "rule fix" would be a bug.
This check rejected 2 of the 4 surprising signs.

## The table

| Feature | LR coef (pro-thermal) | Raw corr | Robust? | Rule threshold | Read |
|---|---:|---:|:--:|---|---|
| `thermik_delta_hpa` | **−0.496** | −0.310 | ✅ | `MIN_THERMIK_DELTA_HPA=−1.0`, soft-veto below | **Direction looks inverted — investigate (Cut 3)** |
| `max_daytime_low_cloud_pct` | −0.408 | −0.327 | ✅ | `MAX_DAYTIME_LOW_CLOUD_PCT=75` | Agrees; threshold likely too lenient → tighten |
| `min_dew_point_spread_c` | −0.252 | **+0.197** | ❌ flip | `MIN_DEW_POINT_SPREAD_C=2.5` | **Rule sign is correct**; LR neg = collinear w/ cloud |
| `overnight_cloud_cover_pct` | −0.237 | −0.270 | ✅ | `MAX_OVERNIGHT_CLOUD_COVER_PCT=95` | Agrees; veto at 95 ≈ never fires → tighten |
| `rained_yesterday` | +0.221 | −0.080 | ❌ flip | soil/rain rule | Artifact; thermal days rained *less*. Not a signal |
| `morning_solar_radiation_wm2` | +0.143 | +0.245 | ✅ | `MIN_MORNING_SOLAR_WM2=380` | Agrees (sun good); minor, partly captured by cloud |
| `innsbruck_hpa` | +0.139 | +0.177 | ✅ | (only feeds deltas) | Level info the delta-only rules discard |
| `bolzano_hpa` | +0.132 | +0.228 | ✅ | (only feeds deltas) | Level info the delta-only rules discard |
| `foehn_delta_hpa` | −0.037 | +0.109 | weak | `FOEHN_TRIGGER_DELTA_HPA=10` | Near-zero; rare-event guardrail — leave |
| `yesterday_precipitation_mm` | −0.023 | — | ~0 | soil/rain rule | Leave |
| `munich_hpa` | +0.014 | — | ~0 | (only feeds deltas) | Leave |

## Findings

### 1. `thermik_delta_hpa` direction looks inverted (headline — but not a blind flip)
Both raw corr (−0.31) and LR (−0.50) robustly say **lower Δ → more
thermal**. Concretely: thermal days average Δ = **−1.78 hPa** (below the
−1.0 threshold → the `thermik` rule fires a SOFT NO_GO on them), while
no-go days average Δ = **−0.85** (above → rule passes). On this label the
rule's veto-below-(−1.0) fires on the days that tend to be thermal.

Caveats that keep this a *hypothesis*, not a fix:
- It's a **SOFT** veto; with `SOFT_VETO_BAR=2` a single thermik veto
  doesn't flip the verdict, so the real-world cost is bounded.
- The −1.0 threshold was hand-fit on **n=10 GO days (peak ≥12 kt)**, which
  had Δ ∈ [−0.8, +2.6]. The replay binary label here is **GO/MAYBE**
  (weaker, peak ≥~8 kt). The association may be that strong sessions sit
  near Δ≈0 while *weaker* thermal days skew very negative — i.e. Δ may
  discriminate strength, not fire-vs-no-fire. Label definition matters.
- `config.py` already calls Δ "a *background* condition, not a trigger —
  local slope-vs-lake T-gradient is the real driver." So the rule layer
  already de-weights it.

**Action:** route to Cut 3 — run `oracle calibrate --replayed` and check
whether `thermik` is on the over-vetoing offender list (ties to the
[[project_calibration_backlog]] note). Test loosen / remove / re-sign,
one change per commit, score on replay. **Do not flip blind** — a
confound (negative Δ co-occurring with some thermally-active synoptic
pattern) is a live alternative explanation.

### 2. Cloud thresholds are too lenient (lowest-risk tuning candidates)
`max_daytime_low_cloud_pct` (rule veto at **75**) and
`overnight_cloud_cover_pct` (veto at **95**) are the cleanest robust
signals after thermik. Thermal vs no-go means: daytime low cloud **31% vs
58%**, overnight cloud **52% vs 71%**. Both vetoes sit so high they almost
never fire, yet the model weights these features 2nd and 4th. Hypothesis:
**lower both thresholds** (and/or make daytime cloud a graded signal, not
a hard cut). Direction agrees with the rule, so low risk — but still
through the gate, one threshold per commit, per CLAUDE.md.

### 3. Absolute pressure levels carry signal the rules throw away
`innsbruck_hpa` (+0.18 raw) and `bolzano_hpa` (+0.23 raw) robustly
associate with thermal — higher pressure = anticyclonic = clearer = more
thermal. The rule layer uses only the **deltas** (Munich−Innsbruck,
Bolzano−Innsbruck) and discards the level. Possible cheap rule: a weak
"high-pressure" pro-thermal nudge or a low-pressure soft caution. Lower
priority (partly collinear with cloud/solar), but a real gap.

### 4. Rejected by the raw-corr check (the methodological catch)
- `min_dew_point_spread_c`: raw corr **+0.197** confirms the rule's
  "drier = better" is correct (thermal 3.6 °C vs no-go 2.6 °C spread). The
  LR's −0.25 is collinearity with cloud cover. **No rule change.**
- `rained_yesterday`: raw corr −0.08 (thermal days rained *less*); the LR
  +0.22 is noise/collinearity. **Not a pro-thermal signal.**

Had we read the multinomial (or even binary) coefficients mechanically,
both would have become wrong "rule fixes." This is the whole reason Cut 1
pairs every coefficient with its raw correlation.

## Verdict for the plan

The linear edge is **mostly threshold mis-placement**, not exotic
structure: the actionable, robust findings (#1 thermik, #2 cloud
thresholds, #3 pressure levels) are all things the existing
calibrate→rescore→calibrate loop can test and ship as ordinary
rule/threshold changes — fully inside "no model ships." But binary LR
tops out at 0.642 test accuracy while HGB clears it by a wide margin
(+0.142 Peirce in the head-to-head), so **interactions remain unexplained
→ Cut 2 is warranted** (shallow surrogate tree / SHAP on HGB), focused on
conjunctions the rules treat independently.

**Next:** Cut 3 on finding #2 first (lowest risk, direction-consistent),
then #1 thermik (highest value, needs the offender-list check), then Cut 2
for interactions. No `config.py` change ships without a replay-validated,
one-change-per-commit pass.

## Reproduction
```bash
uv run python - <<'PY'
from sklearn.impute import SimpleImputer; from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline; from sklearn.preprocessing import StandardScaler
import numpy as np
from oracle.ml.dataset import load_replay_csv, split_by_year, binarise_thermal
d = load_replay_csv('data/replay_full.csv', label_col='actual_verdict_thermal')
s = split_by_year(d, train_until_year=2022, test_from_year=2023, calibration_year=None)
ytr, yte = binarise_thermal(s.train.y_int), binarise_thermal(s.test.y_int)
p = Pipeline([('imp',SimpleImputer(strategy='median')),('sc',StandardScaler()),
              ('clf',LogisticRegression(max_iter=5000, class_weight='balanced', random_state=42))]).fit(s.train.X, ytr)
c = p.named_steps['clf'].coef_[0]
for i in np.argsort(-np.abs(c)):
    f = s.train.feature_names[i]
    r = np.corrcoef(s.train.X[f].fillna(s.train.X[f].median()), ytr)[0,1]
    print(f'{f:30s} coef={c[i]:+.3f}  rawcorr={r:+.3f}')
PY
```
