# Storm handling: rule HARD-veto vs. learned models — 2026-06-21

## TL;DR

The rule layer treats thunderstorm risk as an **all-or-nothing cliff**: lifted
index ≤ −2.0 fires a single `atmospheric_stability` **HARD** veto, which forces
the overall verdict to **FLAUTE** no matter how green the other 13 rules are.
The two learned shadow models have no such cliff — in fact **neither uses the
lifted index at all**. The logistic weighs storm *energy* (CAPE) as one
continuous, soft feature; HGB sees no explicit storm signal whatsoever. So both
can call wind on a low-LI day. That's a real structural advantage *if the call
is right* — and a safety hazard if it isn't. Not yet quantified.

## The rule: one LI threshold, one hard cliff

- `is_storm_risk(lo) = min_lifted_index <= config.MIN_LIFTED_INDEX` with
  `MIN_LIFTED_INDEX = -2.0` (`config.py:212`).
- On a hit, `atmospheric_stability` returns `NO_GO` with `severity=HARD`
  (`rules.py:238-244`).
- Aggregation: **only HARD vetos can flip the overall forecast to NO_GO, and a
  single one suffices** (`rules.py:35`). → storm risk ⇒ FLAUTE, guaranteed.
- `is_storm_risk` is the **single source of truth** for three consumers that
  must agree (`rules.py:209-215`): the HARD veto, the calibration
  storm-quarantine (`calibration.storm_suspected`), and the dashboard's yellow
  storm border.
- Scope of "thunderstorm" is narrow: **CAPE and target-day precipitation are
  read into the snapshot but not yet folded into the trigger** (`rules.py:213-
  215`). The storm decision hangs entirely on LI today.
- Exception: if LI is unavailable (e.g. replay years where IFS HRES doesn't
  expose `lifted_index`), the rule returns **MAYBE**, not FLAUTE
  (`rules.py:222-230`). "Always FLAUTE" assumes an LI value is present — the
  norm for the live ICON/Open-Meteo models.

## The learned models: no LI, no cliff

Verified 2026-06-21 against the live bundle:

| | `lifted_index` | `max_cape_j_kg` (CAPE) |
|---|---|---|
| Logistic (`ml_coeffs.ML_MODEL['features']`, 13) | **no** | **yes** |
| HGB (`replay_full.pkl` → `feature_names_in_`, 11) | **no** | **no** |

Consequences:

- The **logistic** never learned LI as a k.o. criterion; it treats storm
  *energy* (CAPE) as one weighted, continuous input among 13. A low-LI /
  high-CAPE day nudges it toward `no_go` softly, but the thermal drivers
  (pressure Δ, sun, boundary layer …) can outweigh it → GO/MAYBE.
- **HGB** reads neither LI nor CAPE — it only sees the 11 thermal-driver
  features and judges purely on "does this look like a wind day". It is
  structurally **blind to the storm signal**.

This matches the Phase-D distillation finding: the fire decision is linear;
the models' edge is **strength-grading**, not a smarter storm gate
(`docs/findings/ml-distill-cut{1,2,3}-2026-06-14.md`).

## Why this is a double-edged advantage

- **Upside:** on a day where instability is storm-favourable but a clean
  thermal still blows (storms hold off until evening, or never fire), the rule
  says FLAUTE and the models can correctly say wind. A genuine recall gain.
- **Downside (safety):** because HGB is blind to the storm signal, on a *real*
  thunderstorm day it can happily say GO and send a rider onto the lake into
  lightning risk. The rule's HARD veto is **deliberately conservative** — "miss
  a windy storm day rather than send someone into a thunderstorm". A "wind
  despite storm" call is only a win when it's right; when it's wrong it's worse
  than a missed session.
- **Live evidence so far:** on the small 2026 live sample the logistic still
  *loses* to the rule (see the shadow-classifier design doc), so this
  theoretical edge has not yet shown up in deployed data.

## Open question — how to quantify (not yet run)

Filter the ~3,300 replay days where `is_storm_risk` fires (LI ≤ −2.0) and tabulate
what rule / logistic / HGB each said vs. the Urfeld ground truth (session yes/no):

- How often is the rule's FLAUTE *wrong* (a rideable session actually happened)?
- On those days, do the models recover the session (true edge) — and at what
  cost in false GOs on days that really did storm out?

Until that's measured, treat the models' storm-day independence as a *plausible*
advantage, not a demonstrated one. The shadow log is accumulating exactly the
live ground truth needed to settle it.

## Status

Documentation only — no code or behaviour change. The rule's HARD storm veto
stays as the production safety floor; the learned models remain shadow-only and
never touch `overall`.
