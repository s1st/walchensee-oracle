# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Walchi Thermic Oracle — a Python CLI + FastAPI dashboard that forecasts thermal wind conditions at Lake Walchensee (Bavaria). Global NWP models don't resolve the local thermal, so this project fuses three data "pillars" (pressure, meteo, live measurements) through twelve heuristic rules into a `GO` / `MAYBE` / `NO_GO` verdict. Deployed on GCP (project `walchi-oracle-prod`); the same package runs unmodified locally.

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
```

To measure **actual visitors** to the live dashboard, use `scripts/dashboard_traffic.py` — do **not** hand-roll `gcloud logging read | sort -u` IP counts, which overcount ~4× (AI crawlers like GPTBot send browser-ish UAs; one-off scanner IPs read as visitors). The script pulls Cloud Run GET logs, drops bots / exploit-scanner paths / empty-UA probes, and dedupes IPv6 by /64 (flagging m-net's `2001:a61::/32`, which rotates the customer /48 so one person spans several /64s). Run it the day after each rollout step to see what a channel actually delivered.

Storage backend is selected by env: `RUNS_BUCKET` set → `GCSRunStore` (writes `gs://$RUNS_BUCKET/runs/<iso>.json`); unset → `LocalRunStore` at `data/runs/`. Both implement the same `RunStore` protocol in `src/oracle/logger.py` — tests and local dev stay on the filesystem without needing GCP creds.

## Architecture

### Pipeline shape

`engine.run_forecast(day)` fans out to the three pillars **concurrently** via `asyncio.gather`, applies all twelve rules to the snapshots, then aggregates verdicts with strict semantics: any `NO_GO` wins; all `GO` required for overall `GO`; otherwise `MAYBE`. Pressure and meteo are treated as critical; their failures propagate. Measurements are tolerant — Urfeld in particular is flaky.

Rules in `src/oracle/knowledge/rules.py` are pure functions: `pillar_snapshot → Verdict{rule, signal, reason_en, reason_de}`. Every rule emits **both** German and English reasons at evaluation time so the dashboard picks the language per visitor without post-hoc translation. When adding a rule: wire it in `engine.run_forecast`, add a test in `tests/test_rules.py`, and surface it in the dashboard's advanced panel + tooltip.

### Pillars

Each module in `src/oracle/pillars/` fetches one source and returns a typed snapshot:

| Module | Source | Critical? |
|---|---|---|
| `pressure.py` | Open-Meteo MSL pressure (Munich / Innsbruck / Bolzano) — drives `thermik` + `foehn_override` | yes |
| `meteo.py` | Open-Meteo hourly (cloud, solar, wind aloft, BLH, CAPE, LI, soil moisture, precip) | yes |
| `measurements.py` | Bright Sky (DWD) + Addicted-Sports Urfeld scrape; `fetch_urfeld_day_curve` powers backfill | no — one source may drop |

Note: Urfeld is flaky (webcam + anemometer share an outage mode). Don't treat a missing Urfeld reading as a bug.

### Calibration log

`src/oracle/logger.py` writes one JSON per target day. `forecast_to_dict` is the canonical serialiser (used for both `--json` stdout and the stored file). `backfill_run` merges `ground_truth.machine` (Urfeld peak avg/gust, first ignition time, duration counts above 8 kt and 12 kt) into the existing record without touching `ground_truth.human` (hand-edited). The duration-metric thresholds (`_IGNITION_KT=8`, `_SESSION_KT=12`) are **intentionally separate** from `config.IGNITION_WIND_KNOTS` — tuning the forecaster's threshold must not silently rewrite historical metrics.

Preserving `ground_truth` across re-runs is important: `write_run` reads the existing record first and carries the block forward.

### Thresholds

All threshold constants live in `src/oracle/config.py`. They are mixed: the main driver thresholds have been data-fitted against the Urfeld calibration log — `MIN_THERMIK_DELTA_HPA` (+2.5 → −1.0, n=10), `MAX_OVERNIGHT_CLOUD_COVER_PCT` (30 → 95, n=22), `MIN_DEW_POINT_SPREAD_C` (5.0 → 2.5, n=22) and `MAX_LIFTED_INDEX` (6 → 10, n=22) — and the aggregator was reworked to severity-tier/consensus semantics (a single soft veto no longer downgrades). The rest (Föhn trigger, synoptic override, ignition wind, BLH, soil/rain, solar) are still research-analogue guesses — identifiable as the constants lacking an `n=` note in config.py. Use `oracle calibrate` to identify which rules are over-vetoing real session days before tuning the rest. Single-day evidence is not enough; demand the offender list from a sample of ≥10 ground-truthed days, then change one threshold per commit so the rescore-strip in the dashboard isolates the effect.

For large-sample tuning, the historical replay loop (see `docs/historical_forecasts.md`): `oracle replay --from/--to` once (archive-fed verdicts into `runs/replay/`), then `oracle calibrate --replayed` to score ~3,300 archived ground-truth days; after each threshold change, `oracle rescore --replayed` + `oracle calibrate --replayed --resimulated` re-evaluates from stored inputs with zero API traffic.

### Dashboard

`src/oracle/dashboard/main.py` (FastAPI + Jinja2, single `index.html`) reads from the same `RunStore` the CLI writes. Two in-process caches: 60 s per-day file, 5 min for the live-Urfeld panel — keep them in mind when changing endpoints, they mask staleness. The footer carries a friendly link to the windinfo.eu Wind-Wetter-Chat (login required) for users who want to read community chatter themselves; **do not reintroduce server-side scraping or republishing of that chat** — third-party user posts have DSGVO + § 87b UrhG (Datenbankschutz) implications, and the chat pillar was deliberately removed for that reason.

Language toggle (DE/EN) auto-detects via `Accept-Language`; `Verdict.reason` defaults to English for CLI and legacy JSON readers.

### Deployment

Two Docker images built from the same source tree via `cloudbuild.yaml` (`_DOCKERFILE` substitution):

- `Dockerfile.job` → `oracle-job:latest`, run as Cloud Run Jobs `oracle-forecast` (08:00 CET, `forecast --horizon=3`) and `oracle-backfill` (21:00 CET, `backfill`), both in `europe-west3`.
- `Dockerfile.dashboard` → `dashboard:latest`, run as Cloud Run service `walchi-oracle-dash` in `europe-west1` (region required for custom domain mapping to `walchensee.simon-stieber.de`).

The service account split (`walchi-oracle-job@` read/write runs bucket, `walchi-oracle-dash@` read-only) is intentional; keep it when adding new resources. (Historical: `windinfo-user` / `windinfo-pass` Secret Manager entries and Cloud Run job env bindings can be deleted manually — no in-tree code consumes them after the chat pillar removal.)
