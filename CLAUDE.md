# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Walchi Thermic Oracle — a Python CLI + FastAPI dashboard that forecasts thermal wind conditions at Lake Walchensee (Bavaria). Global NWP models don't resolve the local thermal, so this project fuses four data "pillars" (pressure, meteo, live measurements, community chat) through twelve heuristic rules into a `GO` / `MAYBE` / `NO_GO` verdict. Deployed on GCP (project `walchi-oracle-prod`); the same package runs unmodified locally.

Read `docs/architecture.md` first for GCP layout, data flow, and component responsibilities. `docs/thermal-model.md` has the domain knowledge behind the rules.

## Commands

Environment uses `uv`; Python 3.11+, packaged with hatchling.

```bash
uv venv
uv pip install -e ".[dev,dashboard]"      # dev + dashboard extras
cp .env.example .env                      # needs WINDINFO_USER / WINDINFO_PASS for chat pillar

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
```

Storage backend is selected by env: `RUNS_BUCKET` set → `GCSRunStore` (writes `gs://$RUNS_BUCKET/runs/<iso>.json`); unset → `LocalRunStore` at `data/runs/`. Both implement the same `RunStore` protocol in `src/oracle/logger.py` — tests and local dev stay on the filesystem without needing GCP creds.

## Architecture

### Pipeline shape

`engine.run_forecast(day)` fans out to the four pillars **concurrently** via `asyncio.gather`, applies all twelve rules to the snapshots, then aggregates verdicts with strict semantics: any `NO_GO` wins; all `GO` required for overall `GO`; otherwise `MAYBE`. The chat pillar is wrapped in `_fetch_chat_tolerant` — it logs and returns `[]` on any exception so a windinfo.eu outage never takes out the forecast. Pressure and meteo are treated as critical; their failures propagate.

Rules in `src/oracle/knowledge/rules.py` are pure functions: `pillar_snapshot → Verdict{rule, signal, reason_en, reason_de}`. Every rule emits **both** German and English reasons at evaluation time so the dashboard picks the language per visitor without post-hoc translation. When adding a rule: wire it in `engine.run_forecast`, add a test in `tests/test_rules.py`, and surface it in the dashboard's advanced panel + tooltip.

### Pillars

Each module in `src/oracle/pillars/` fetches one source and returns a typed snapshot:

| Module | Source | Critical? |
|---|---|---|
| `pressure.py` | Open-Meteo MSL pressure (Munich / Innsbruck / Bolzano) — drives `thermik` + `foehn_override` | yes |
| `meteo.py` | Open-Meteo hourly (cloud, solar, wind aloft, BLH, CAPE, LI, soil moisture, precip) | yes |
| `measurements.py` | Bright Sky (DWD) + Addicted-Sports Urfeld scrape; `fetch_urfeld_day_curve` powers backfill | no — one source may drop |
| `chat.py` | windinfo.eu WP login → Wise Chat Pro AJAX (checksum scraped from page HTML) | no |

Note: Urfeld is flaky (webcam + anemometer share an outage mode). Don't treat a missing Urfeld reading as a bug.

### Calibration log

`src/oracle/logger.py` writes one JSON per target day. `forecast_to_dict` is the canonical serialiser (used for both `--json` stdout and the stored file). `backfill_run` merges `ground_truth.machine` (Urfeld peak avg/gust, first ignition time, duration counts above 8 kt and 12 kt) into the existing record without touching `ground_truth.human` (hand-edited). The duration-metric thresholds (`_IGNITION_KT=8`, `_SESSION_KT=12`) are **intentionally separate** from `config.IGNITION_WIND_KNOTS` — tuning the forecaster's threshold must not silently rewrite historical metrics.

Preserving `ground_truth` across re-runs is important: `write_run` reads the existing record first and carries the block forward.

### Thresholds

All threshold constants live in `src/oracle/config.py` and every one is marked `TODO(calibrate)`. Values are placeholders from research analogues (Garda + local kiter heuristics), not fitted to Walchensee data. Changing a threshold changes the forecast distribution; favour wiring a new rule/input over retuning existing numbers until the calibration log has enough sessions.

### Dashboard

`src/oracle/dashboard/main.py` (FastAPI + Jinja2, single `index.html`) reads from the same `RunStore` the CLI writes. Two in-process caches: 60 s per-day file, 5 min for the live-Urfeld panel — keep them in mind when changing endpoints, they mask staleness. Anonymisation happens here: the public HTML strips `author` and redacts `@handle` mentions (`_HANDLE_RE`) from chat bodies; the raw GCS logs keep full fields for private calibration. Don't leak chat authors into any user-facing path.

Per-day community-sentiment derivation uses `_infer_day_reference` to match messages to a specific date via `heute` / `morgen` / `übermorgen` / German weekday names. Extend the keyword lists (`_POS_KW`, `_NEG_KW`, `_DE_WEEKDAYS`) rather than introducing a general sentiment model.

Language toggle (DE/EN) auto-detects via `Accept-Language`; `Verdict.reason` defaults to English for CLI and legacy JSON readers.

### Deployment

Two Docker images built from the same source tree via `cloudbuild.yaml` (`_DOCKERFILE` substitution):

- `Dockerfile.job` → `oracle-job:latest`, run as Cloud Run Jobs `oracle-forecast` (08:00 CET, `forecast --horizon=3`) and `oracle-backfill` (21:00 CET, `backfill`), both in `europe-west3`.
- `Dockerfile.dashboard` → `dashboard:latest`, run as Cloud Run service `walchi-oracle-dash` in `europe-west1` (region required for custom domain mapping to `walchensee.simon-stieber.de`).
- `Dockerfile` is a separate OpenClaw-based image that bundles the `claw/walchi-oracle` skill — unrelated to the scheduled pipeline.

Secrets `windinfo-user` / `windinfo-pass` are in Secret Manager and injected into the job via env. The service account split (`walchi-oracle-job@` read/write runs bucket, `walchi-oracle-dash@` read-only) is intentional; keep it when adding new resources.
