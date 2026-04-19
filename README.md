# Walchi Thermic Oracle

A proactive forecasting system for thermal wind conditions at Lake Walchensee (Bavaria).
Combines local expert chat, pressure gradients, meteorological data, and live sensor
readings to outperform standard weather models for this notoriously tricky spot.

## Data pillars

1. **Oracle Chat** — scrapes windinfo.eu live chat for insider tips.
2. **Pressure Gradient** — real-time Munich − Innsbruck hPa delta.
3. **Meteorological Conditions** — overnight cooling + forecasted solar radiation.
4. **Live Measurements** — wind speeds from Urfeld, Galerie, Sachenbach.

A knowledge base of heuristics (synoptic override, hPa threshold, …) turns raw
pillar data into a forecast verdict.

## Setup

```bash
# install uv if needed: curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv
uv pip install -e ".[dev]"
cp .env.example .env
```

## Usage

```bash
oracle              # forecast for today
oracle --day 2026-05-15   # forecast for a specific day
```

## Layout

```
src/oracle/
├── config.py          # stations, thresholds, endpoints
├── pillars/           # one module per data source
├── knowledge/rules.py # heuristics
├── engine.py          # aggregates pillars → forecast
└── cli.py             # entry point
```
