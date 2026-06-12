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
   Urfeld + the nearest DWD synoptic station (via Bright Sky).

> An earlier fourth pillar that scraped the windinfo.eu Wind-Wetter-Chat was
> removed for DSGVO + § 87b UrhG (Datenbankschutz) reasons. The dashboard now
> only links to that chat; nothing is scraped, stored, or fed into the verdict.

Twelve heuristic rules — `thermik`, `foehn_override`, `overnight_cooling`,
`solar_radiation`, `dew_point_spread`, `boundary_layer_height`,
`post_rain_moisture`, `atmospheric_stability`, `daytime_clouds`,
`upper_level_wind`, `synoptic_override`, `thermal_ignition` — turn raw
pillar data into a GO / MAYBE / NO_GO forecast verdict. Each rule emits
both a German and an English reason so the dashboard can render in either
language without post-hoc translation.

## Public dashboard

Deployed at **https://walchensee.simon-stieber.de** (Cloud Run + custom
domain via Cloudflare DNS). Shows:

- **Live webcam + wind panel** pinned on top — embedded Addicted-Sports
  Urfeld webcam, current wind speed, peak gust, last-hour average, and a
  trend indicator.
- **Three-day picker** (today / tomorrow / day after). Each tab shows a
  colour dot for its verdict. The scheduled job writes all three days'
  forecasts every morning.
- **Verdict card** with a single-line summary (top blocking reason for
  NO_GO, counter of green rules for GO).
- **30-day strip** split into two rows — the oracle's forecast verdict on
  top, the actual Urfeld peak wind below, same colour scale so forecast
  misses jump out visually.
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
  already shipped and those still to add (lake temperature, snow cover,
  Kesselberg channeling, seasonal threshold calibration, …) with
  prioritisation.

## Setup

```bash
# install uv if needed: curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv
uv pip install -e ".[dev]"
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
└── cli.py             # entry point
```
