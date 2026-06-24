# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Walchi Thermic Oracle — a Python CLI + FastAPI dashboard that forecasts thermal wind conditions at Lake Walchensee (Bavaria). Global NWP models don't resolve the local thermal, so this project fuses three data "pillars" (pressure, meteo, live measurements) through fourteen heuristic rules into a `GO` / `MAYBE` / `NO_GO` verdict. Deployed on GCP (project `walchi-oracle-prod`); the same package runs unmodified locally.

Read `docs/architecture.md` first for GCP layout, data flow, and component responsibilities. `docs/thermal-model.md` has the domain knowledge behind the rules.

## Commands

Environment uses `uv`; Python 3.11+, packaged with hatchling.

```bash
uv venv
uv pip install -e ".[dev,dashboard]"      # dev + dashboard extras
cp .env.example .env

# CLI (entry point declared in pyproject.toml: oracle = oracle.cli:app)
oracle forecast                           # today, logs to data/runs/<date>.json
oracle forecast --day 2026-05-15          # specific day
oracle forecast --horizon 3               # today + 2 days (this is what the scheduled job runs)
oracle forecast --json --no-log           # machine-readable, no log file
oracle backfill                           # merge Urfeld wind curve as ground truth into today's log
oracle backfill --day 2026-05-15

# Dashboard locally
uvicorn oracle.dashboard.main:app --reload

# Tests / lint / typecheck
pytest                                    # pytest-asyncio is in auto mode
pytest tests/test_rules.py                # single file
pytest tests/test_rules.py::test_thermik_go  # single test
ruff check src tests
mypy src

# Real dashboard traffic (bot-filtered, IPv6 /64-deduped) — for sizing rollout reach
python3 scripts/dashboard_traffic.py            # last 30d, prod
python3 scripts/dashboard_traffic.py --days 7   # shorter window

# ML ceiling spike + distillation (merged to main; ml CLI behind the [ml] extra, not in either Dockerfile)
uv pip install -e ".[ml]"                       # adds scikit-learn + pandas; not in either Dockerfile
oracle ml train --csv data/replay_full.csv --out data/ml/replay_full.pkl
oracle ml evaluate --csv data/replay_full.csv --model data/ml/replay_full.pkl \
                   --report data/ml/replay_full_report.json
python3 scripts/cost_ratio_sweep.py             # sensitivity sweep over the missed/wasted cost ratio
```

To measure **actual visitors** to the live dashboard, use `scripts/dashboard_traffic.py` — do **not** hand-roll `gcloud logging read | sort -u` IP counts, which overcount ~4× (AI crawlers like GPTBot send browser-ish UAs; one-off scanner IPs read as visitors). The script pulls Cloud Run GET logs, drops bots / exploit-scanner paths / empty-UA probes, and dedupes IPv6 by /64 (flagging m-net's `2001:a61::/32`, which rotates the customer /48 so one person spans several /64s). Run it the day after each rollout step to see what a channel actually delivered.

Storage backend is selected by env: `RUNS_BUCKET` set → `GCSRunStore` (writes `gs://$RUNS_BUCKET/runs/<iso>.json`); unset → `LocalRunStore` at `data/runs/`. Both implement the same `RunStore` protocol in `src/oracle/logger.py` — tests and local dev stay on the filesystem without needing GCP creds.

## Architecture

### Pipeline shape

`engine.run_forecast(day)` fans out to the three pillars **concurrently** via `asyncio.gather`, applies all fourteen rules to the snapshots, then aggregates verdicts with **severity-tiered** semantics: any HARD `NO_GO` wins; otherwise `SOFT_VETO_BAR` (=2) or more SOFT `NO_GO`s downgrade to `MAYBE`; else `GO` (a single soft veto no longer downgrades). Pressure and meteo are treated as critical; their failures propagate. Measurements are tolerant — Urfeld in particular is flaky. **Thunderstorm risk is *not* a verdict veto** — `atmospheric_stability` stays GREEN on storm days and the danger is surfaced as a separate Caution advisory (see "Thunderstorm advisory" below); a storm day is usually a strong thermal day until the gust front arrives.

Rules in `src/oracle/knowledge/rules.py` are pure functions: `pillar_snapshot → Verdict{rule, signal, reason_en, reason_de}`. Every rule emits **both** German and English reasons at evaluation time so the dashboard picks the language per visitor without post-hoc translation. When adding a rule: wire it in `engine.run_forecast`, add a test in `tests/test_rules.py`, and surface it in the dashboard's advanced panel + tooltip.

### Pillars

Each module in `src/oracle/pillars/` fetches one source and returns a typed snapshot:

| Module | Source | Critical? |
|---|---|---|
| `pressure.py` | Open-Meteo MSL pressure (Munich / Innsbruck / Bolzano) — drives `thermik` + `foehn_override` | yes |
| `meteo.py` | Open-Meteo hourly (cloud, solar, wind aloft, BLH, CAPE, LI, CIN, soil moisture, precip) — morning aggregates for the rules + **afternoon convective aggregates** (`afternoon_*`) for the storm classifier | yes |
| `measurements.py` | Bright Sky (DWD) + Addicted-Sports Urfeld scrape; `fetch_urfeld_day_curve` powers backfill. **Stored buoy curve** (`ground_truth.machine.samples`, gust+pressure, 2016–2026) is the storm classifier's training label — read locally, don't re-fetch (endpoint 429-limits) | no — one source may drop |

Note: Urfeld is flaky (webcam + anemometer share an outage mode). Don't treat a missing Urfeld reading as a bug.

**Addicted-Sports data use is under a partnership** agreed in person with co-owner Andy on 2026-06-23 (after an initial refusal on 2026-06-22 — see git history of the removal/restore commits). The buoy data + webcam are used with permission; in return their site will feature the forecast (integration details, possibly an API, still open). Don't remove the scrape on legal-risk grounds — that question is settled. If the integration lands an official API, migrate the scrape to it rather than dropping the source.

### Calibration log

`src/oracle/logger.py` writes one JSON per target day. `forecast_to_dict` is the canonical serialiser (used for both `--json` stdout and the stored file). `backfill_run` merges `ground_truth.machine` (Urfeld peak avg/gust, first ignition time, duration counts above 8 kt and 12 kt) into the existing record without touching `ground_truth.human` (hand-edited). The duration-metric thresholds (`_IGNITION_KT=8`, `_SESSION_KT=12`) are **intentionally separate** from `config.IGNITION_WIND_KNOTS` — tuning the forecaster's threshold must not silently rewrite historical metrics.

Preserving `ground_truth` across re-runs is important: `write_run` reads the existing record first and carries the block forward.

### Shadow ML classifier (experimental)

`src/oracle/ml_classifier.py` runs a distilled multinomial logistic regression (go/maybe/no_go) alongside the rules. It is **shadow only**: `forecast_to_dict` calls `classify(pressure, meteo)` and attaches an `ml_classifier` block to the record (verdict, class probabilities, top-3 feature contributions, DE/EN reason), but it **never feeds the aggregator** — `overall` is the 14-rule verdict, full stop. The dashboard shows it as a clearly-labelled "experimental" card under the headline. Purpose: accumulate a live ground-truth log so a future season can decide whether to promote it (it beats the rule in leave-one-year-out CV, mean Peirce +0.215 vs +0.114, but currently *loses* on the small live 2026 sample — shadow mode is how we find out).

The model is **pure data**: ~69 floats frozen in `src/oracle/knowledge/ml_coeffs.py` (auto-generated), scored in pure Python — **no sklearn/numpy/pandas in either prod image**. Retrain with `python scripts/export_ml_coeffs.py --csv data/replay_full.csv` (needs the `[ml]` extra); it rewrites the constant, after which `tests/test_ml_classifier.py`'s golden vector will flag the change. The 11 features are read from the same `inputs.pressure`/`inputs.meteo` keys the training CSV is built from, so train/serve can't drift. A `tests/test_ml_classifier.py` **shadow-invariant** test guards that the ML block can never change `overall`. Provenance: `docs/findings/ml-distill-cut{1,2,3}-2026-06-14.md` + `ml-shadow-classifier-design-2026-06-14.md`.

### Thunderstorm advisory (LI-decoupled, classifier-driven)

The "storm" signal is an **advisory**, not a veto: the LI ≤ −2 thunderstorm veto was decoupled from the verdict (a ~70 %-false-alarm signal shouldn't kill a rideable thermal — the day-ahead ground truth shows storm days mostly still fire before the gust front). It now drives only the dashboard Caution box, the strip's yellow storm border, and the calibration storm-day tally — **never `overall`**. `calibration.storm_suspected` (the single source of truth) and `atmospheric_stability` both delegate to `src/oracle/storm_classifier.py`.

`storm_classifier` is a **pure-Python logistic** over 9 afternoon (12–18 local) convective features — CAPE, LI, CIN, precip, deep shear, low-cloud + 3 interactions — computed by `pillars/meteo.py` and stored as `afternoon_*` keys on `MeteoSnapshot`. On 5 seasons (2021–2025) of **stored buoy gust-front ground truth** (`data/runs/<iso>.json` `ground_truth.machine.samples`: gust ≥ 22 kt ∧ MSL pressure jump ≥ 2 hPa) it scores leave-one-year-out POD 82 % / FAR 84 % / Peirce 0.431 vs the old LI flag's 0.178 — ~2× recall at a lower false-alarm ratio, at a deliberately recall-favouring operating point (it's a safety warning). When the afternoon features are absent (archive host / pre-2021 / legacy records) it **falls back to the LI ≤ −2 rule**.

Pure data, like the shadow classifier: coeffs frozen in `src/oracle/knowledge/storm_coeffs.py` (auto-generated), **no sklearn/numpy at serve time**. Retrain with `python scripts/export_storm_coeffs.py` (needs `[ml]`); it builds the training set through the pillar's *own* code (`fetch_hourly_range` → `snapshot_from_range` → `raw_from_snapshot`) so train/serve can't drift, then rewrites the constant — after which `tests/test_storm_classifier.py`'s golden vector flags the change. That file also guards the **shadow-invariant** (the advisory never touches `overall`) and the LI fallback. Caveats: small positive class (89 storms; recall bootstrap CI [74 %, 90 %]); CAPE/LI from Open-Meteo exist only 2021 + (hard ceiling; ERA5 archive lacks them); `precipitation_probability` is deliberately **excluded** — it is null on the historical-forecast archive used for training, so it would drift from the live API. Provenance: `docs/findings/li-decouple-2026-06-24.md`, `storm-ground-truth-spike-2026-06-24.md`, `thunderstorm-forecast-design-2026-06-24.md`.

### Thresholds

All threshold constants live in `src/oracle/config.py`. They are mixed: the main driver thresholds have been data-fitted against the Urfeld calibration log — `MIN_THERMIK_DELTA_HPA` (+2.5 → −1.0, n=10), `MAX_OVERNIGHT_CLOUD_COVER_PCT` (30 → 95, n=22), `MIN_DEW_POINT_SPREAD_C` (5.0 → 2.5, n=22) and `MAX_LIFTED_INDEX` (6 → 10, n=22) — and the aggregator was reworked to severity-tier/consensus semantics (a single soft veto no longer downgrades). The rest (Föhn trigger, synoptic override, ignition wind, BLH, soil/rain, solar) are still research-analogue guesses — identifiable as the constants lacking an `n=` note in config.py. (`MIN_LIFTED_INDEX` = −2 is no longer a verdict veto — it's the *fallback* threshold for the storm advisory when the classifier's afternoon features are absent; see "Thunderstorm advisory".) Use `oracle calibrate` to identify which rules are over-vetoing real session days before tuning the rest. Single-day evidence is not enough; demand the offender list from a sample of ≥10 ground-truthed days, then change one threshold per commit so the rescore-strip in the dashboard isolates the effect.

For large-sample tuning, the historical replay loop (see `docs/historical_forecasts.md`): `oracle replay --from/--to` once (archive-fed verdicts into `runs/replay/`), then `oracle calibrate --replayed` to score ~3,300 archived ground-truth days; after each threshold change, `oracle rescore --replayed` + `oracle calibrate --replayed --resimulated` re-evaluates from stored inputs with zero API traffic.

### ML ceiling spike (research)

The 14-rule heuristic + severity-tiered aggregator is the **production** classifier. The offline ceiling check (developed on the `ml-classifier` branch, **now merged to `main`**; the `oracle ml` CLI lives behind the `[ml]` extra, not installed in either Dockerfile) asks "is the rule baseline near the data ceiling?" — answer on 715 ICON-era holdout days: **no**. With 11 ICON-stable features (the 8 ICON-era-only features were dropped to avoid an era-boundary distribution shift in the train/test comparison — see the writeup's Setup section for the rationale), logistic regression beats the rule on Peirce, HSS, accuracy, and hard-error rate simultaneously, and HGB clears +0.142 Peirce (Δ from rule's +0.066) with McNemar p = 3.8 × 10⁻⁸. The cost matrix in `calibration._COST` is a per-rider knob, not a project constant — `scripts/cost_ratio_sweep.py` sweeps the missed-session / wasted-drive ratio r ∈ [0.25, 7.0] and shows **both** ML models dominating the rule across the full range (no crossover for either). Tier 1+2 hyperparam + class_weight tuning via `scripts/tune_ml.py` confirmed the doc's first-pass defaults were already near-optimal; the project's `class_weight='balanced'` beat all cost-sensitive dict variants. Empirical writeup: `docs/findings/ml-classifier-2026-06-13.md`. **No model *drives the verdict*** — but a **shadow classifier did ship** from this work (logged + shown alongside the rules; see "Shadow ML classifier" above and `docs/findings/ml-shadow-classifier-design-2026-06-14.md`). The original ship/no-ship call (interpretability, per-rider cost framing, "project doesn't dictate cost") still holds for *replacing* the rule layer; the shadow path threads it by shipping only an interpretable, per-rider-thresholdable logistic that never touches `overall`. Phase D distillation (`ml-distill-cut{1,2,3}`) found no new conjunctive rule to harvest — the fire decision is linear, HGB's edge is strength-grading — but did surface and ship the `overnight_cooling` veto removal. The `oracle ml` CLI is preserved so the experiment can be re-run after future threshold changes.

### Dashboard

`src/oracle/dashboard/main.py` (FastAPI + Jinja2, single `index.html`) reads from the same `RunStore` the CLI writes. Two in-process caches: 60 s per-day file, 5 min for the live-Urfeld panel — keep them in mind when changing endpoints, they mask staleness. The footer carries a friendly link to the windinfo.eu Wind-Wetter-Chat (login required) for users who want to read community chatter themselves; **do not reintroduce server-side scraping or republishing of that chat** — third-party user posts have DSGVO + § 87b UrhG (Datenbankschutz) implications, and the chat pillar was deliberately removed for that reason.

Language toggle (DE/EN) auto-detects via `Accept-Language`; `Verdict.reason` defaults to English for CLI and legacy JSON readers.

### Deployment

Two Docker images built from the same source tree via `cloudbuild.yaml` (`_DOCKERFILE` substitution):

- `Dockerfile.job` → `oracle-job:latest`, run as Cloud Run Jobs `oracle-forecast` (08:00 CET, `forecast --horizon=3`) and `oracle-backfill` (21:00 CET, `backfill`), both in `europe-west3`.
- `Dockerfile.dashboard` → `dashboard:latest`, run as Cloud Run service `walchi-oracle-dash` in `europe-west1` (region required for custom domain mapping to `walchensee.simon-stieber.de`).

The service account split (`walchi-oracle-job@` read/write runs bucket, `walchi-oracle-dash@` read-only) is intentional; keep it when adding new resources. (Historical: `windinfo-user` / `windinfo-pass` Secret Manager entries and Cloud Run job env bindings can be deleted manually — no in-tree code consumes them after the chat pillar removal.)

#### Deploy runbook (push-to-`main` workflow)

Two Cloud Build triggers fire on push to `main`: `dashboard-deploy-on-main` and `job-build-on-main` (both use `cloudbuild.yaml`). Watch them with `gcloud builds list`. Important gotchas, learned 2026-06-13:

- **The build updates the dashboard *service* but NOT the *jobs*.** `cloudbuild.yaml`'s deploy step only runs `gcloud run services update` (for `_DEPLOY_SERVICE`); the job trigger has `_DEPLOY_SERVICE=''`, so it builds+pushes `oracle-job:latest` but never re-points the jobs. Cloud Run jobs pin the digest at update time, so **after a build that changes rule logic you must re-pin both jobs** or the next scheduled forecast runs the old code:
  ```
  gcloud run jobs update oracle-forecast --region europe-west3 --image europe-west3-docker.pkg.dev/walchi-oracle-prod/walchi/oracle-job:latest
  gcloud run jobs update oracle-backfill --region europe-west3 --image .../oracle-job:latest
  ```
- **After any rule/threshold change, rescore the prod bucket** so the dashboard's stats panel (which reads `overall_resimulated`) reflects the deployed rules — otherwise it advertises rules that no longer run:
  ```
  RUNS_BUCKET=walchi-oracle-prod-runs oracle rescore --since <PROJECT_FIRST_DAY>   # back up the affected runs/ first
  ```
  The stats panel caches for 1 h (`_STATS_TTL_S`), so bounce the service to show it immediately: `gcloud run services update walchi-oracle-dash --region europe-west1 --update-env-vars STATS_CACHE_BUST=<sha>`.
- Today's on-page verdict only refreshes on the next scheduled job run; `gcloud run jobs execute oracle-forecast` regenerates it now if needed.
