# Walchi Thermic Oracle

A proactive forecasting system for thermal wind conditions at Lake Walchensee
(Bavaria) — one of Germany's premier windsurfing lakes. Standard forecast
models (GFS, ECMWF) don't resolve the Walchensee thermal; this tool combines
local expert chat, pressure gradients, meteorological data, and live sensor
readings to fill the gap.

## Data pillars

1. **Oracle Chat** — authenticated read-only polling of the windinfo.eu
   Wind-Wetter-Chat (WordPress Wise Chat Pro) for messages mentioning
   Walchensee spots.
2. **Pressure Gradient** — real-time Munich − Innsbruck hPa delta (the
   community calls this "Thermik"; meteorologists call it "Alpenpumpe") and
   Bolzano − Innsbruck delta (Föhn detection).
3. **Meteorological Conditions** — overnight cooling, morning solar radiation,
   synoptic wind aloft, dew-point spread, boundary-layer height, soil
   moisture + recent precipitation.
4. **Live Measurements** — shore wind from the Addicted-Sports anemometer at
   Urfeld + the nearest DWD synoptic station (via Bright Sky).

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
- **Community sentiment badge** derived per-day from chat messages that
  explicitly reference that day (`heute` / `morgen` / `übermorgen` / weekday
  names in German).
- **30-day strip** split into two rows — the oracle's forecast verdict on
  top, the actual Urfeld peak wind below, same colour scale so forecast
  misses jump out visually.
- **Advanced panel** (checkbox-toggled) with the full rule table, `?`
  tooltips explaining each rule, and anonymised chat excerpts.
- **DE / EN language toggle** in the top right corner, with auto-detection
  via `Accept-Language`.

**Privacy note on chat:** the raw log captured by the scheduled job contains
windinfo.eu usernames (used privately for calibration analysis of who tends
to call conditions correctly). The public dashboard strips all author fields
and redacts `@handle` mentions from message bodies so no windinfo identities
surface on the open web. External polling is twice daily (08:00 and 21:00
CET) to stay comfortably below any reasonable rate limit. windinfo.eu
operators can reach out via the contact on the repository's Issues tab or
via my Impressum if they'd prefer the polling stopped.

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
