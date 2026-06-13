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
    21:00 CET  ───────►  oracle-backfill  ───────►│   └─ knowledge/rules    │
      (backfill today)     merges Urfeld peak     │                         │
                           ground truth to GCS    └────────────┬────────────┘
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
| `measurements` | Bright Sky (DWD) + Addicted-Sports scrape (Urfeld) | no | yes — one source dropping is ok |

### Rules (`src/oracle/knowledge/rules.py`)

Fourteen heuristic rules turn pillar data into `Verdict{rule, signal, severity, reason_en, reason_de}` records. Aggregation is **severity-tiered**, not flat: any **HARD** `no_go` (Föhn, synoptic/upper-level wind, thunderstorm-risk LI, and `no_insolation` — heavy cloud + low sun) forces overall `no_go`; otherwise `SOFT_VETO_BAR` (=2) or more **SOFT** `no_go`s downgrade to `maybe`; else `go`. A single soft veto no longer downgrades. Rule reasons are bilingual; see the dashboard for how each language is picked per visitor. (Rule set + aggregator were reworked 2026-06-13 — see `docs/2026-06-13-corrected-methodology-rework.md`.)

### Logger (`src/oracle/logger.py`)

`RunStore` protocol with two implementations: `LocalRunStore` for development, `GCSRunStore` activated when `RUNS_BUCKET` is set in the environment. Same read/write/ground-truth semantics, different backend. Every scheduled run writes one JSON file per target day; the 21:00 backfill merges `ground_truth.machine` (Urfeld peak / first ignition / duration above thresholds) into the existing file.

### Dashboard (`src/oracle/dashboard/main.py`)

FastAPI app reading the same `RunStore`. In-memory 60 s cache per-day file and a 5-minute cache for the live-Urfeld panel so visitor traffic doesn't thrash GCS or the Addicted-Sports endpoint. Renders:

- a three-day tab picker (today + two forecast days)
- live webcam + current/last-hour/trend panel
- the selected day's verdict card with a bilingual one-line summary
- 30-day forecast-vs-actual strip with the same go/maybe/no_go colour scale on both rows
- advanced panel (checkbox-toggled) with the full rule table

The footer carries a friendly link to the windinfo.eu Wind-Wetter-Chat (login required at windinfo.eu) for users who want community context — but the project itself does **not** scrape, store, or republish that chat. A previous chat-pillar that did so was removed for DSGVO + § 87b UrhG (Datenbankschutz) reasons; do not reintroduce.

## GCP layout

All resources in project `walchi-oracle-prod`:

- **Artifact Registry** `europe-west3-docker.pkg.dev/walchi-oracle-prod/walchi/` — stores two images: `oracle-job:latest` and `dashboard:latest`, built via Cloud Build from `cloudbuild.yaml` with a `_DOCKERFILE` substitution.
- **Cloud Run Jobs** (region `europe-west3`): `oracle-forecast` (`forecast --horizon=3`) and `oracle-backfill` (`backfill`). Both bind to service account `walchi-oracle-job@…` with least-privilege IAM (`storage.objectAdmin` on the runs bucket).
- **Cloud Run Service** (region `europe-west1`, required for custom-domain mappings): `walchi-oracle-dash`. Scales to zero, ingress `all`, service account `walchi-oracle-dash@…` with read-only `storage.objectViewer`.
- **Cloud Scheduler** (region `europe-west3`): two HTTP jobs, 08:00 and 21:00 Europe/Berlin, invoking the Cloud Run Jobs via the Run Admin API. Uses a dedicated `walchi-scheduler@…` service account with `run.invoker` per-job.
- **Cloud Storage** `gs://walchi-oracle-prod-runs/runs/YYYY-MM-DD.json`.
- **Custom domain** `walchensee.simon-stieber.de` → CNAME to `ghs.googlehosted.com` (DNS at Cloudflare), Cloud Run domain mapping attached to the dashboard service.

## Local development

The same package runs locally without GCP. `RUNS_BUCKET` unset → `LocalRunStore` writes `data/runs/`. `oracle forecast` and `oracle backfill` commands work identically to the cloud jobs. The dashboard can be run with `uvicorn oracle.dashboard.main:app` for local testing.
