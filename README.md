# Walchi Thermic Oracle

A proactive forecasting system for thermal wind conditions at Lake Walchensee
(Bavaria) — one of Germany's premier windsurfing lakes. Standard forecast
models (GFS, ECMWF) don't resolve the Walchensee thermal; this tool combines
pressure gradients, meteorological data, and live sensor readings to fill the
gap.

## Data pillars

1. **Pressure Gradient** — real-time Munich − Innsbruck hPa delta (the
   community calls this "Thermik"; meteorologists call it "Alpenpumpe") and
   Bolzano − Innsbruck delta (Föhn detection).
2. **Meteorological Conditions** — overnight cooling, morning solar radiation,
   synoptic wind aloft, dew-point spread, boundary-layer height, soil
   moisture + recent precipitation.
3. **Live Measurements** — shore wind from the Addicted-Sports anemometer at
   Urfeld + lake water temperature from the buoy, plus the nearest DWD
   synoptic station (via Bright Sky).

> An earlier fourth pillar that scraped the windinfo.eu Wind-Wetter-Chat was
> removed for DSGVO + § 87b UrhG (Datenbankschutz) reasons. The dashboard now
> only links to that chat; nothing is scraped, stored, or fed into the verdict.

Fourteen heuristic rules — `thermik`, `foehn_override`, `overnight_cooling`,
`solar_radiation`, `dew_point_spread`, `boundary_layer_height`,
`post_rain_moisture`, `atmospheric_stability`, `daytime_clouds`,
`no_insolation`, `upper_level_wind`, `synoptic_override`, `thermal_ignition`,
`air_lake_delta` — turn raw pillar data into a GO / MAYBE / NO_GO forecast
verdict via a severity-tiered aggregator (any hard blocker wins; two or more
soft vetoes downgrade to MAYBE). Each rule emits both a German and an English
reason so the dashboard can render in either language without post-hoc
translation.

## Public dashboard

Deployed at **https://walchensee.simon-stieber.de** (Cloud Run + custom
domain via Cloudflare DNS). Split into four routes:

- **`/` (landing)** — live webcam + wind panel, three-day picker (today /
  tomorrow / day after, each tab colour-coded by verdict), verdict card with
  a single-line summary, and the experimental logistic-ML card.
- **`/history`** — 30-day strip with four rows on a shared GO/MAYBE/NO_GO
  colour scale: rule-based forecast (re-scored under current rules), logistic
  ML forecast (experimental), HistGradientBoosting ML forecast (experimental
  black-box), and actual session outcome (≥ 1 h from the Urfeld wind curve).
  Clicking any cell shows the selected day's verdict and wind chart inline.
- **`/stats`** — forecast quality metrics (accuracy, confusion matrix,
  sensitivity/specificity) for all three model layers side by side.
- **`/about`** — the 14 rules explained.
- **Experimental ML classifier card** — a distilled logistic regression
  (pure-Python scorer, no extra runtime deps) runs alongside the rules in
  *shadow mode* on the landing page: logged, shown for comparison, but it
  **never drives the official verdict**.
- **Advanced panel** (checkbox-toggled) with the full rule table and `?`
  tooltips explaining each rule.
- **DE / EN language toggle** in the top right corner, with auto-detection
  via `Accept-Language`.
- Footer link to the windinfo.eu Wind-Wetter-Chat (login required there) for
  visitors who want community context. The dashboard does not scrape, store,
  or republish that chat — a previous version that did was removed for DSGVO
  + § 87b UrhG (Datenbankschutz) reasons. Do not reintroduce.

## Documentation

- **[docs/architecture.md](docs/architecture.md)** — the GCP layout, data
  flow, and component responsibilities. Start here to understand the
  repository.
- **[docs/thermal-model.md](docs/thermal-model.md)** — domain knowledge:
  how the Walchensee thermal works, triggering conditions, spatial
  progression, seasonal patterns, pressure pairs, and threshold
  calibration status.
- **[docs/future-factors.md](docs/future-factors.md)** — forecast factors
  already shipped (e.g. lake temperature) and those still to add (snow
  cover, Kesselberg channeling, seasonal threshold calibration, …) with
  prioritisation.

## Setup

```bash
# install uv if needed: curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv
uv pip install -e ".[dev,dashboard]"   # add ,ml for the research `oracle ml` CLI
cp .env.example .env
```

## Usage

```bash
oracle forecast                      # today's forecast, also logged to data/runs/
oracle forecast --day 2026-05-15     # specific day
oracle forecast --json               # machine-readable output for agents
oracle backfill                      # merge today's Urfeld wind curve as ground truth
oracle backfill --day 2026-05-15     # backfill a specific past day
oracle rescore                       # re-run the rule layer on logged records under the current aggregator
oracle calibrate                     # score logged forecasts against Urfeld ground truth
oracle hgb-backfill                  # score the HGB model on logged records and write hgb_classifier blocks (requires [ml] extra)
```

Each forecast writes `data/runs/<YYYY-MM-DD>.json` with the raw inputs, the
verdict, and an empty `ground_truth` block. `backfill` fills in
`ground_truth.machine` (Urfeld peak / ignition time / duration above
thresholds). Edit `ground_truth.human` by hand for subjective notes.

## Layout

```
src/oracle/
├── config.py          # stations, thresholds, endpoints
├── pillars/           # one module per data source
├── knowledge/rules.py # heuristics
├── engine.py          # aggregates pillars → forecast
├── ml_classifier.py   # logistic shadow classifier (pure-Python scorer, in prod)
├── hgb_shadow.py      # HGB shadow classifier (requires [ml] extra, backfill only)
├── ml/                # offline ML research (train/evaluate; behind the [ml] extra)
└── cli.py             # entry point
```
