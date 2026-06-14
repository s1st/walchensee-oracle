# Publishing plan — the Walchi ML shadow-classifier story

**Drafted 2026-06-14. Self-contained:** you can hand this to a fresh
session (or a collaborator) with zero prior context and start producing
publication artifacts. It recaps *what was built*, *the honest story*,
*where the source material lives*, *what each channel needs*, and *what
must be resolved before publishing externally*.

---

## 0. What this is for

Turn the ML work on the Walchi Thermic Oracle into public artifacts across
several channels (Discord, subreddits, windinfo.eu chat, a LinkedIn
long-form, and possibly a research-y paper). The **engineering record is
already complete** (see §4); what's missing is *audience translation*.
This doc is the brief for that translation.

---

## 1. The project in one paragraph (context for any reader)

**Walchi Thermic Oracle** forecasts thermal wind ("Thermik") for
windsurfers/kiters at Lake Walchensee in the Bavarian Alps. Global weather
models don't resolve the local thermal, so the project fuses three data
"pillars" — cross-Alps **pressure** (Munich/Innsbruck/Bolzano), **meteo**
(cloud, solar, dew-point, precip, etc. from Open-Meteo), and **live
measurements** (DWD + an Urfeld shore anemometer) — through **14 hand-tuned
heuristic rules** + a severity-tiered aggregator into a daily
**GO / MAYBE / NO_GO** verdict. It runs on GCP (Cloud Run jobs + a FastAPI
dashboard at `walchensee.simon-stieber.de`), bilingual DE/EN. Ground truth
= the Urfeld wind curve (a "session" ≈ ≥1 h above ~8–12 kt). Roughly 1,900
archive-reconstructed in-season days (2017–2026) form the calibration set.

---

## 2. What was built — the ML arc (the substance to publish)

The question: **is the 14-rule heuristic near the ceiling of what the data
allows, or can a learned model do better?** The investigation, in order:

1. **Ceiling spike.** Trained logistic regression and gradient-boosted
   trees (HGB) on 11 era-stable features, year-blocked holdout. Answer:
   the rule is **not** at the ceiling — ML has real, statistically
   significant discrimination headroom over it.

2. **Distillation as "ML-as-oracle"** (not "ship a model"): use the model
   to find structure the rules miss, then express it as ordinary rules.
   - The **fire/no-fire decision is essentially linear** — no exotic
     feature interactions to harvest into clever new rules.
   - HGB's extra edge lives in **GO-vs-MAYBE strength grading**, which the
     veto-based rule architecture doesn't even attempt — i.e. capturing it
     is an *architecture* change, not a new rule.
   - The replay "offender list" showed the rule layer **over-vetoes
     systematically**. One clean fix fell out and shipped: removing the
     `overnight_cooling` veto (89% false-positive on the replay).

3. **The validation journey** (the methodological heart of the story):
   - A single year-blocked split flattered the model and *wasted data*
     (trained only on the older "IFS" weather-model era, tested on the
     newer "ICON" era).
   - **Expanding-window time-series CV** (train on all past, test next
     year) — forward-realistic, the way you'd actually retrain.
   - **Leave-one-year-out CV** — maximal-data discrimination ceiling.
   - Result: the **logistic generalizes; the boosted trees do not.** LR
     beats the rule in **9 of 10 leave-one-year-out folds** (mean Peirce
     skill **+0.215 vs the rule's +0.114**). HGB is stronger some years but
     **overfits and collapses on 2026**, the most recent (live) season.
   - On the **current 2026 season (small n, in progress) the rule still
     wins** — for *both* models. The edge is real historically but
     unproven on the data you'd actually deploy into.

4. **The decision: shadow-mode, don't replace.** Ship the *interpretable*
   logistic (not the black-box HGB) as a **shadow classifier**: it runs on
   every forecast, is **logged** and **shown on the dashboard** (an
   "experimental" card + a row in the 30-day strip), but it **never drives
   the official verdict** — that stays the 14 rules. Shadow mode exists to
   gather *live forward evidence* and answer the only open question (does
   the edge survive on 2026?) without betting the verdict on it.
   Engineering nicety: the model is distilled to ~69 floats scored in pure
   Python, so it adds **zero dependencies** to production.

### Headline numbers (for figures/claims)
| Metric (3-class Peirce skill, 715-day holdout) | Rule | Logistic | HGB |
|---|---|---|---|
| Year-blocked holdout | +0.066 | +0.158 | +0.208 |
| Leave-one-year-out, mean | +0.114 | **+0.215** | +0.200 (erratic) |
| 2026 alone (live, n≈73) | **+0.160** | +0.101 | +0.090 |

(HGB beats the rule by +0.142 Peirce overall with McNemar p = 3.8×10⁻⁸ —
real, but it *loses to the rule on 2026*. That contrast is the story.)

---

## 3. The honest narrative (the publishable kernel)

This is what makes it worth a stranger's time — state it as the headline:

> **"I built a heuristic forecaster, then spent a serious effort trying to
> beat it with ML. The honest answer isn't 'ML wins' — it's nuanced, and
> the nuance is the interesting part."**

Transferable lessons (each is a potential post/section):
- **Shadow-mode beats replace.** When a learned model *might* beat your
  hand-built logic, run it alongside in the open and let it earn its keep
  on live data — don't rip out the interpretable thing on the strength of
  a backtest.
- **Beware the recent-season overfit.** The fancy model (HGB) looked best
  in aggregate but was *worst* on the newest, most deployment-relevant
  data. The simple linear model generalized. Classic, underappreciated.
- **Distillation as a hypothesis generator.** Using the model to *find
  rule/threshold bugs* (validated through the existing calibration gate)
  is higher-leverage than shipping the model — and it shipped one real fix.
- **Cost is a per-rider knob, not a project constant.** Whether you
  optimize for "never miss a session" vs "never waste a drive" flips the
  verdict; the model logs probabilities so the *rider* can set the cut.
- **Honest negative/nuanced results are publishable** and more credible
  than hype.

⚠️ **Timing/honesty constraint:** the 2026 shadow results are **not mature**
(season in progress, small n). So the publishable claim *right now* is the
**methodology + system + the nuanced finding** — NOT "the ML model wins."
The "did shadow beat the rule?" payoff is a *follow-up* once the season
completes. Don't let any artifact overclaim current ML superiority.

---

## 4. Where the source material lives (in this repo)

All under `docs/findings/` unless noted:
- **`ml-work-session-2026-06-14.md`** — the index/narrative + operations
  (retrain/promote/rollback). **Start here.**
- `ml-shadow-classifier-design-2026-06-14.md` — the shipped system design.
- `ml-distill-cut1-2026-06-14.md` — logistic coefficients vs thresholds.
- `ml-distill-cut2-2026-06-14.md` — interaction ablation (edge = strength
  grading, not the veto).
- `ml-distill-cut3-2026-06-14.md` — replay-gate validation + the
  `overnight_cooling` fix + the offender list.
- `ml-classifier-2026-06-13.md` — the ceiling-spike empirical writeup.
- `ml-research-2026-06-13.md` — the deep methodology research.
- Architecture/domain: `docs/architecture.md`, `docs/thermal-model.md`.

**Existing visual assets:**
- `data/ml/cost_sweep.png` — cost-ratio sweep plot.
- A dashboard screenshot of the 30-day strip (Vorhersage / ML-Klassifikator
  / Tatsächlich rows) — reproducible from the live site.
- The live dashboard itself (`walchensee.simon-stieber.de`) — the ML card +
  strip row are visible features to screenshot.

**Reproducible analysis:** `oracle ml train|evaluate` CLI (behind the
`[ml]` extra), `scripts/cost_ratio_sweep.py`, `scripts/tune_ml.py`,
`scripts/export_ml_coeffs.py`, `scripts/backfill_ml.py`.

---

## 5. Channels & the artifact each needs

Identity/domain strategy (from prior project decisions): **anonymous
`s1st.de` face for Reddit/forums**, **real-name `simon-stieber.de` /
LinkedIn** for professional posts. The GitHub repo carries the author's
real name (commit authors, LICENSE) — keep that in mind for any "here's the
code" link on anonymous channels. Staged rollout precedent: Discord first
(launched 2026-05-31), then windinfo, then LinkedIn.

| Channel | Register / length | Include | Reuse from §4 |
|---|---|---|---|
| **Discord** (community) | Casual, short, 1 image | "Added an experimental ML line you can watch next to the forecast" + strip screenshot + 1 honest caveat | TL;DRs |
| **Subreddits** (e.g. r/MachineLearning, r/kitesurfing, r/datascience — different framings each) | Medium; lead with the nuance, not the build | The "tried to beat my heuristic, here's the honest result" hook; link code on the anon face | §2 + §3 |
| **windinfo.eu chat** | German, practical, rider-facing | "Does it actually call your sessions? New ML line, judge it yourself" — no stats jargon | rider framing only |
| **LinkedIn blog** | Narrative long-form, real name | Full arc §2 → the 5 lessons §3; figures; "shadow not replace" as the thesis | cut docs ARE the arc |
| **Research-y paper / preprint** | Formal | System + method (replay calibration, year-blocked vs expanding-window vs LOYO, McNemar, cost-ratio sweep, distillation-as-oracle), dataset description, figures, honest results | §2 method + ml-research doc |

Per-subreddit angle differs: ML subs want the validation-methodology +
overfit lesson; kite/surf subs want "a tool that forecasts your local
thermal"; data-science subs want the shadow-deployment pattern.

---

## 6. Blockers to resolve BEFORE external publication

1. **Data licensing / attribution / privacy.**
   - Ground truth is scraped from Addicted-Sports/Urfeld; features from
     Open-Meteo + DWD (Bright Sky). Check each source's terms before
     publishing/sharing the **replay dataset**; attribute Open-Meteo + DWD.
   - **Do NOT republish windinfo.eu chat content/data** — it was removed
     from the project for DSGVO + §87b UrhG (database-right) reasons.
     Posting *your own* content to their chat is fine; republishing theirs
     is not.
2. **Open-source decision.** Is the repo public? Commit history + LICENSE
   carry the real name — incompatible with the anonymous `s1st.de` channels
   unless you accept the de-anonymization or publish a scrubbed/separate
   artifact. Decide per channel.
3. **Figures.** Generate the per-year / leave-one-year-out Peirce chart and
   refresh `cost_sweep.png`; capture a clean dashboard-strip screenshot.
   (A blog/paper needs more than the one screenshot that exists.)
4. **Maturity caveat (see §3).** Keep claims to "method + system + nuanced
   finding"; the "shadow model won/lost on 2026" result is a follow-up.

---

## 7. Suggested sequence (matches the staged-rollout precedent)

1. **Discord** — lowest stakes; "new experimental ML line, watch it next to
   the forecast." Validates framing.
2. **windinfo.eu chat** — German rider audience; practical, judge-it-yourself.
3. **Subreddits** — anon face; tailor the hook per sub.
4. **LinkedIn long-form** — real name; the full honest narrative + lessons.
5. **Paper/preprint** — optional, later; strongest *after* the 2026 season
   matures the shadow result.

### Per-artifact checklist
- [ ] One-screen Discord post + strip screenshot
- [ ] windinfo.eu German rider post
- [ ] Subreddit posts (ML / kite / data-science variants)
- [ ] LinkedIn long-form (arc + 5 lessons + figures)
- [ ] Figures: LOYO/per-year Peirce chart, refreshed cost-sweep, clean strip shot
- [ ] Data/licensing review done; attribution lines drafted
- [ ] Decide open-source / anonymity per channel
- [ ] (Later) paper skeleton: abstract, system, method, results, honest limits

---

## 8. Open questions for the author (decide before drafting)
- **Anonymity per channel** — anon `s1st.de` vs real-name? Affects whether
  you link the (real-name) GitHub repo.
- **How much to share** — repo public? dataset public? just the writeup?
- **Paper or not** — and if so, venue/format (arXiv preprint vs workshop)?
  Note the results aren't mature for a "results" paper yet; a "system &
  method" framing is publishable now.
- **Lead claim** — confirm it's the *nuanced/honest* framing, not "ML beats
  my forecaster" (which the current data doesn't support).
