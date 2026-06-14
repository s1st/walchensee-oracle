# The Walchi Thermic Oracle — project story & timeline

**Drafted 2026-06-14. Narrative source for publishing** (LinkedIn long-form,
blog, paper intro, the "origin story" any channel wants). Self-contained:
usable without prior session context. Author-only facts are marked
**`[AUTHOR: …]`** — fill these before publishing.

> **Logline:** Over ~8 weeks a windsurfer builds a forecaster for his home
> lake's thermal wind that global weather models can't resolve — taking it
> from research-placeholder guesses, to something measured against real
> session data, to an honest test of whether machine learning can beat his
> hand-built rules. (Spoiler: the honest answer is the interesting part.)

**Span:** first commit **2026-04-19**, through **2026-06-14** — 168 commits,
~8 weeks ("almost two months"). The full history *is* in git; this timeline
is reconstructed directly from `git log` + `CHANGELOG.md` + `README.md`.

---

## Sources
- **`git log`** (root `833b86d` 2026-04-19 → HEAD 2026-06-14) — the
  authoritative timeline. (Heads-up for whoever reruns this: `git log`
  output gets truncated by the `rtk` token-killer to a recent window — use
  `git rev-list`, date-windowed `--since/--until`, or write to a file to see
  the whole history.)
- **`CHANGELOG.md`** — curated milestone narrative + a "throughline" summary.
- **`README.md`** — the pitch, the three pillars, the windinfo-chat removal.
- **`docs/findings/ml-*`** — the ML climax (`ml-work-session-2026-06-14.md`
  is the index). **`docs/architecture.md` / `docs/thermal-model.md`** —
  system + domain.

---

## The arc (with real dates)

### Phase 0 — Inception & the day-1 sprint (2026-04-19)
`[AUTHOR: the real hook — why start? The drive to the lake on a great-looking
forecast with no wind, or vice versa? The personal rider angle.]`

The problem is concrete: **global NWP models (GFS, ECMWF) don't resolve the
Walchensee thermal** — the local "Alpenpumpe"/"Thermik" wind from the
cross-Alps pressure gradient + slope heating. A rider asking "worth the
drive today?" has no good tool. The bet: fuse the signals the global models
smear over into one daily **GO / MAYBE / NO_GO** verdict.

**Day one (8 commits, 2026-04-19)** already scaffolded the whole skeleton:
config from thermal-wind research, and *all the data pillars in a single
sitting* — pressure (Open-Meteo, + Föhn rule), meteo (Open-Meteo, + cooling
& radiation rules), measurements (Bright Sky/DWD + the Urfeld anemometer),
**and** a fourth windinfo.eu chat pillar.

### Phase 1 — Going live (2026-04-22): the big build day
**25 commits in one day** — the project's own day-0 (`PROJECT_FIRST_DAY =
2026-04-22`). This is where it became a real, deployed product:
- Calibration **logger** (per-day JSON + Urfeld machine ground truth), the
  `RunStore` abstraction (local disk / GCS), the **dashboard service +
  Cloud Build + deploy**.
- Three more meteo rules; renamed `alpenpumpe` → **`thermik`** to match how
  the community talks; **bilingual DE/EN** rule reasons + i18n toggle.
- Dashboard: multi-day horizon, **webcam embed + live wind pinned on top**,
  the **30-day strip (forecast vs actual rows)**, German date formatting,
  wind sparkline, LICENSE + `architecture.md` + `CLAUDE.md`.
- A few days later (04-25): the **severity-tier aggregator** (only hard
  blockers are fatal) and **`oracle calibrate`** (confusion matrix vs Urfeld
  truth) + the rescored third row.

### Phase 2 — The ethics pivot & first real tuning (2026-05-03)
**16 commits.** Two beats matter:
- **The fourth pillar gets deleted (`709266e`).** The windinfo.eu chat
  scraper + sentiment badge + persisted chat are **removed for DSGVO + § 87b
  UrhG (database-right)** reasons — you can't scrape/republish third-party
  user posts. The dashboard now only *links* to the chat. *The project gave
  up a useful signal because using it wasn't right.* (Strong honesty beat.)
- **First threshold off a placeholder (`33b482c`): thermik +2.5 → −1.0 hPa**,
  fitted to real Urfeld data — plus consensus aggregator semantics (a single
  soft veto no longer downgrades), and **`calibrate --csv`** (the first
  ML-ready feature export).

### Phase 3 — Redefining "a session" (2026-05-15 → 05-30)
The subtle, honest maturity move — *what even counts as success?*
- 05-15: **duration-based ground-truth label** (sustained wind, not a
  transient peak); more retunes (overnight_cooling → 95%, LI cap → 10,
  dew-point spread → 2.5); the dashboard's actual row switches to **"Session
  ≥ 1 h"**. (≈ the n=22 calibration point.)
- 05-30: **GO bar lowered 12 → 11 kt** — real 11-kt-with-gusts Walchi
  sessions were being mislabelled MAYBE; `CHANGELOG.md` is born.

> *Note the honest tension here, straight from the CHANGELOG: most of the
> work so far was the **scoreboard**, not the forecaster — the rules were
> still mostly placeholders. He built the referee before fixing the player.*

### Phase 4 — Publishing begins & polish (2026-05-31 → 06-01)
- 05-31 (Discord launch day; 12 commits): docs corrected to drop stale
  "placeholder" claims, README chat-pillar removal, refactors, the 30-day
  strip collapsed to a single Forecast line, and the **anonymity move** —
  *hide the GitHub link on the pseudonymous `s1st.de` host* (`239f0e2`), the
  start of the dual-identity domain strategy. `[AUTHOR: Discord reception?]`
- 06-01: per-channel traffic stats, **storm-day quarantine** from
  calibration.

### Phase 5 — The real-data turn (2026-06-11 → 06-13): calibrate hard
The inflection the project was built toward — enough ground truth (own days
**+** a 9-year archive corpus) to fit the rules to reality:
- 06-11: drop the **rained-yesterday veto** (wrong 13/17) and the
  **direction-only SSE veto** (0/4); ship the **public statistics panel**
  (visitor count + forecast quality + sensitivity/specificity).
- 06-12 (**33 commits — the biggest day**): add a **lake-temp pillar +
  `air_lake_delta` rule**; capture the **full buoy payload + 9 years of
  historical ground truth** (~3,600 archive records); a **replay engine**
  (Open-Meteo Historical APIs) to re-score ~3,300 archived days with no live
  traffic; then the **threshold pass** — solar 600→380, daytime-cloud
  60→75, synoptic 15→25, Föhn 4→10, BLH 600→400, soil 0.35→0.30, plus
  aggregator-bar experiments — each one-per-commit; an external "fable"
  review.
- 06-13: a real **metrics/validation toolkit** — Peirce/Heidke skill +
  **cost matrix**, season restriction, the **thermal-character label**, and
  a **validation harness (McNemar, year/era splits)**; a structural finding
  (the NO_GO skill comes from *insolation*, not Föhn) → `no_insolation` HARD
  veto; deploy runbook + a first reframed publishing arc.

### Phase 6 — The ML climax (2026-06-14): can a model beat the rules?
The question the data finally made askable. (Full detail:
`ml-work-session-2026-06-14.md`.) 30 commits in a day:
1. **Ceiling spike** — is the heuristic near the data ceiling? **No.**
2. **Distillation as "ML-as-oracle"** — but the fire/no-fire decision is
   **linear** (no interactions to harvest); the model's edge is *strength
   grading*, an architecture question. It did surface one real bug → shipped
   the **`overnight_cooling` veto removal** (the very veto added back in
   Phase 3 — 89% false-positive on the replay).
3. **The validation journey** — a single split flattered the model and
   wasted data; expanding-window + **leave-one-year-out** CV told the truth:
   the **logistic generalizes (beats the rule 9/10 years, mean Peirce +0.215
   vs +0.114); the boosted trees overfit and collapse on 2026.** On the
   **live 2026 season the rule still wins.**
4. **Decision: shadow, don't replace.** Ship the interpretable logistic as a
   **shadow classifier** — logged + shown (an "experimental" card + a row in
   the 30-day strip, backfilled), 69 floats in pure Python (zero new prod
   deps) — that **never drives the verdict**. The rules stay in charge; the
   model earns its keep in the open against live 2026 sessions.

### Phase 6b — Documentation & consolidation (still 2026-06-14)
The research branch merged to `main`; full findings docs + this publishing
material written; branches cleaned up. The **promote decision** (let the
model drive the verdict) is deliberately open, gated on the 2026 season
maturing. `[AUTHOR: the winter shutdown — project pauses Nov–Mar, no
off-season samples — shapes a "we'll know by autumn" framing.]`

---

## The throughline (the theme to sell)
The same honest discipline runs end to end:
1. **Build the scoreboard before trusting the player** — measure outcomes,
   then tune, then (only then) model.
2. **Fit to reality before reaching for fancy** — placeholders → data-fitted
   rules → ML; and even then the *simple* model won.
3. **Shadow-deploy, don't replace** — when a model *might* beat your
   interpretable logic, run it alongside in the open; don't rip out what
   works on the strength of a backtest.
4. **Honesty as a feature** — gave up the windinfo signal on legal/ethical
   grounds; openly admitted the rules were placeholders; ships a model
   labelled "experimental, not in charge." The data doesn't support
   "ML wins (yet)" — so the story doesn't claim it.

(Per-channel use of these in `ml-shadow-classifier-publishing-plan.md` §3.)

---

## `[AUTHOR: …]` gaps — the things git can't tell us
- **The hook / motivation** and the personal rider angle (the one thing no
  commit records — and the most important for a narrative).
- **Discord launch reception** (2026-05-31); any user quotes.
- **Emotional beats**: first correctly-called session; the worst miss; the
  moment real data changed the game.
- **Where it's going**: promote-or-not after the 2026 season; future pillars
  (lake temp shipped; snow cover, Kesselberg channeling still to come — see
  `docs/future-factors.md`).

## Housekeeping (fix before a public "here's the code" link)
`README.md` is **stale**: "Twelve heuristic rules" (now 14 — added
`daytime_clouds`/`no_insolation`/`air_lake_delta`) and a **two-row** 30-day
strip (now three rows + the ML row). Bring it current first.
