# Walchi Thermic Oracle

A proactive forecasting system for thermal wind conditions at Lake Walchensee
(Bavaria) — one of Germany's premier windsurfing lakes. Standard forecast
models (GFS, ECMWF) don't resolve the Walchensee thermal; this tool combines
local expert chat, pressure gradients, meteorological data, and live sensor
readings to fill the gap.

## Data pillars

1. **Oracle Chat** — scrapes windinfo.eu live chat for insider tips.
2. **Pressure Gradient** — real-time Munich − Innsbruck hPa delta (Alpenpumpe)
   and Bolzano − Innsbruck delta (Föhn detection).
3. **Meteorological Conditions** — overnight cooling, forecasted solar
   radiation, and synoptic wind aloft.
4. **Live Measurements** — wind speeds from Urfeld and the nearest DWD station.

Six heuristic rules (synoptic override, Föhn suppression, hPa threshold, …)
turn raw pillar data into a GO / MAYBE / NO_GO forecast verdict.

## Documentation

- **[docs/thermal-model.md](docs/thermal-model.md)** — domain knowledge:
  how the thermal works, triggering conditions, spatial progression,
  seasonal patterns, pressure pairs, and threshold calibration status.
- **[docs/future-factors.md](docs/future-factors.md)** — additional forecast
  factors not yet implemented (dew point, boundary layer height, soil
  moisture, lake temperature, etc.) with prioritization.

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
