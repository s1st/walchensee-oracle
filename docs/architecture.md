# Architecture

The project has two clearly separated pieces — a scheduled data-collection
pipeline that runs twice a day, and a web dashboard that serves from the same
data store. Both pieces live in the same GCP project (`walchi-oracle-prod`).

## Data flow

```
                                                     ┌─────────────────────┐
                                                     │   Open-Meteo API    │ pressure, meteo
                                                     │   Bright Sky API    │ DWD synoptic wind
                                                     │   Addicted-Sports   │ Urfeld buoy (scrape)
                                                     └──────────┬──────────┘
                                                                │
  Cloud Scheduler         Cloud Run Jobs           ┌────────────▼────────────┐
  ─────────────           ─────────────            │  oracle CLI (Python)    │
    08:00 CET  ───────►  oracle-forecast  ───────► │  run_forecast(day)      │
      (--horizon=3)        writes today + 2       │   ├─ pillars/pressure   │
                           days forward to GCS    │   ├─ pillars/meteo      │
                                                  │   ├─ pillars/measurements│
                                                  │   └─ knowledge/rules    │
                                                  │                         │
                                                  └────────────┬────────────┘
                                                               │
                                                    ┌──────────▼───────────┐
                                                    │  GCS bucket          │
                                                    │  walchi-oracle-prod- │
                                                    │  runs/runs/YYYY-MM-  │
                                                    │  DD.json             │
                                                    └──────────┬───────────┘
                                                               │ read
                                                    ┌──────────▼───────────┐
  Cloudflare DNS                                    │  Cloud Run Service   │
  ──────────────                                    │  walchi-oracle-dash  │
  walchensee.simon-stieber.de  CNAME →  ghs.googlehosted.com                │
                                                    │  FastAPI + Jinja2    │
                                        ┌──────────►│  in-process 60 s     │
                                        │           │  cache per day       │
                                        │           └──────────────────────┘
                                        │
                                 your browser
```

## Components

### Pillars (`src/oracle/pillars/`)

Each pillar fetches one data source and returns a typed snapshot.

| Pillar | Source | Needs auth | Fails gracefully |
|---|---|---|---|
| `pressure` | Open-Meteo (MSL pressure for Munich / Innsbruck / Bolzano) | no | no — critical |
| `meteo` | Open-Meteo (hourly cloud, radiation, wind-aloft, BLH, CAPE, LI, soil moisture …) | no | no — critical |
| `measurements` | Bright Sky (DWD) — nearest synoptic station | no | yes — tolerant |

### Rules (`src/oracle/knowledge/rules.py`)

Fourteen heuristic rules turn pillar data into `Verdict{rule, signal, severity, reason_en, reason_de}` records. Aggregation is **severity-tiered**, not flat: any **HARD** `no_go` (Föhn, synoptic/upper-level wind, thunderstorm-risk LI, and `no_insolation` — heavy cloud + low sun) forces overall `no_go`; otherwise `SOFT_VETO_BAR` (=2) or more **SOFT** `no_go`s downgrade to `maybe`; else `go`. A single soft veto no longer downgrades. Rule reasons are bilingual; see the dashboard for how each language is picked per visitor. (Rule set + aggregator were reworked 2026-06-13 — see `docs/2026-06-13-corrected-methodology-rework.md`.)

### Logger (`src/oracle/logger.py`)

`RunStore` protocol with two implementations: `LocalRunStore` for development, `GCSRunStore` activated when `RUNS_BUCKET` is set in the environment. Same read/write/ground-truth semantics, different backend. Every scheduled run writes one JSON file per target day. `ground_truth.machine` in historical records contains Urfeld buoy data from the former backfill pipeline; no new machine ground truth is written (Addicted-Sports refused data permission 2026-06-22, backfill command and Cloud Scheduler job removed/paused).

### Dashboard (`src/oracle/dashboard/main.py`)

FastAPI + Jinja2 app reading the same `RunStore`, split into four routes:

- **`/`** — landing page: three-day tab picker, live webcam (pending Addicted-Sports permission for embed), verdict card with a bilingual one-line summary, and the experimental logistic-ML card. The live Urfeld wind panel is disabled (no data source).
- **`/history`** — 30-day strip with **four rows** on the shared go/maybe/no_go colour scale: rule-based forecast (re-scored), logistic ML (experimental), HGB ML (experimental black-box), and actual session outcome (≥ 1 h from Urfeld). Clicking a strip cell renders the selected day's wind chart + verdict inline on the same page.
- **`/stats`** — forecast quality metrics (accuracy, confusion matrix, sensitivity/specificity) for all three model layers (rule, logistic, HGB).
- **`/about`** — the 14 rules explained with `?` tooltips.

In-memory caches: 60 s per-day file, 1 h for stats, 12 h for Cloud Logging visitor counts.

**Shadow ML classifiers** — two models run alongside the rules in shadow mode (never drive the verdict):
- `ml_classifier` (logistic regression): distilled to ~69 pure-Python floats in `ml_coeffs.py`; scored at forecast time in `forecast_to_dict`; shown on the landing page and the history strip.
- `hgb_classifier` (HistGradientBoosting): scored offline via `oracle hgb-backfill` (requires `[ml]` extra, not in prod Docker images); shown on `/history` and `/stats` only.

The footer carries a friendly link to the windinfo.eu Wind-Wetter-Chat (login required at windinfo.eu) for users who want community context — but the project itself does **not** scrape, store, or republish that chat. A previous chat-pillar that did so was removed for DSGVO + § 87b UrhG (Datenbankschutz) reasons; do not reintroduce.

## GCP layout

All resources in project `walchi-oracle-prod`:

- **Artifact Registry** `europe-west3-docker.pkg.dev/walchi-oracle-prod/walchi/` — stores two images: `oracle-job:latest` and `dashboard:latest`, built via Cloud Build from `cloudbuild.yaml` with a `_DOCKERFILE` substitution.
- **Cloud Run Jobs** (region `europe-west3`): `oracle-forecast` (`forecast --horizon=3`) binds to service account `walchi-oracle-job@…` with `storage.objectAdmin` on the runs bucket. `oracle-backfill` job still exists but is paused (Cloud Scheduler `oracle-backfill-daily` paused 2026-06-22 — no data source).
- **Cloud Run Service** (region `europe-west1`, required for custom-domain mappings): `walchi-oracle-dash`. Scales to zero, ingress `all`, service account `walchi-oracle-dash@…` with read-only `storage.objectViewer`.
- **Cloud Scheduler** (region `europe-west3`): two HTTP jobs, 08:00 and 21:00 Europe/Berlin, invoking the Cloud Run Jobs via the Run Admin API. Uses a dedicated `walchi-scheduler@…` service account with `run.invoker` per-job.
- **Cloud Storage** `gs://walchi-oracle-prod-runs/runs/YYYY-MM-DD.json`.
- **Custom domain** `walchensee.simon-stieber.de` → CNAME to `ghs.googlehosted.com` (DNS at Cloudflare), Cloud Run domain mapping attached to the dashboard service.

## Local development

The same package runs locally without GCP. `RUNS_BUCKET` unset → `LocalRunStore` writes `data/runs/`. `oracle forecast` works identically to the cloud job. The dashboard can be run with `uvicorn oracle.dashboard.main:app` for local testing.
