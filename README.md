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
2. **Pressure Gradient** — real-time Munich − Innsbruck hPa delta (Alpenpumpe)
   and Bolzano − Innsbruck delta (Föhn detection).
3. **Meteorological Conditions** — overnight cooling, morning solar radiation,
   synoptic wind aloft, dew-point spread, boundary-layer height, soil
   moisture + recent precipitation.
4. **Live Measurements** — shore wind from the Addicted-Sports anemometer at
   Urfeld + the nearest DWD synoptic station (via Bright Sky).

Nine heuristic rules (synoptic override, Föhn suppression, hPa threshold,
overnight cooling, solar radiation, dew-point spread, boundary-layer height,
post-rain moisture, thermal ignition) turn raw pillar data into a
GO / MAYBE / NO_GO forecast verdict.

## Public dashboard

Deployed at **https://walchensee.simon-stieber.de** (Cloud Run + custom
domain via Cloudflare DNS). Shows today's verdict, the rule breakdown, a
30-day strip of verdict vs. actual Urfeld peak wind, and an anonymised
excerpt of recent Walchensee-mentioning chat messages.

**Privacy note on chat:** the raw log captured by the scheduled job contains
windinfo.eu usernames (used privately for calibration analysis of who tends
to call conditions correctly). The public dashboard strips all author fields
and redacts `@handle` mentions from message bodies so no windinfo identities
surface on the open web. The windinfo.eu operators were informed of the
polling before the dashboard went public. External polling is twice daily
(08:00 and 21:00 CET) to stay comfortably below any reasonable rate limit.

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
