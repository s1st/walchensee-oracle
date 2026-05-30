# Changelog

Milestone history for the Walchi Thermic Oracle, organised around the two things
that matter most: the **forecast model** (rules, thresholds, aggregator) and how
we **measure the outcome** (ground truth, calibration, the dashboard scoreboard).
Not every commit — just the changes that moved the needle. Commit hashes in
parentheses.

## Forecast model

### Foundations
- Config and types seeded from Walchensee thermal-wind research (`b3e5dc7`)
- **Pressure pillar** (Open-Meteo MSL) with the Föhn-override rule (`f5eef68`)
- **Meteo pillar** (Open-Meteo) with overnight-cooling and solar-radiation rules (`b877cad`)
- **Measurements pillar** — Bright Sky (DWD) plus the Urfeld anemometer (`4111eec`, `2ac0665`)
- Three high-priority meteo rules: dew-point spread, boundary-layer height, rain history (`ca11c12`)
- Three medium-priority meteo factors (`2f500e4`)
- Renamed `alpenpumpe` → `thermik` to match community vocabulary (`a354ba7`)

### Aggregator semantics
- **Severity-tier veto** — only *hard* blockers stop the forecast; soft signals no longer fatal (`acbdf0e`)
- **Consensus semantics** — a single soft veto no longer downgrades the verdict (`503ca31`)

### Data-fitted threshold retunes
One threshold per commit, so each effect is isolated in the rescore strip.
- **thermik delta +2.5 → −1.0 hPa** (`33b482c`) — the first threshold moved off a research placeholder onto real Urfeld data
- overnight_cooling cloud-cover veto → 95% (`9ea86d7`)
- atmospheric_stability cap-arm lifted-index → 10 (`19b6c06`)
- dew_point_spread veto → 2.5 °C (`f5013c1`)

## Measuring the outcome

### Capturing ground truth
- **Calibration logger** — one JSON per day plus machine ground truth scraped from Urfeld (peak avg/gust, ignition time, duration counts) (`09ed0e7`)
- 30-day strip split into **forecast vs. actual** rows (`d18e266`)
- **`oracle calibrate`** — confusion matrix scoring forecasts against Urfeld peak truth (`0602b85`)
- **Third row** — forecasts re-scored under the *current* aggregator ("Neu berechnet") (`5ef2f23`)

### From peak to duration
The shift in *what counts as a session*.
- `calibrate --csv` — flat feature + ground-truth export for offline ML (`13a9c1a`)
- `calibrate --resimulated` — score the current rule layer against history (`05e6f9e`)
- **Duration-based ground-truth label** — sustained wind, not just a transient peak (`36ba93e`)
- Actual row switched to the **duration label (Session ≥ 1 h)** (`49313ba`)
- **Session GO bar lowered avg 12 → 11 kt** — stop labelling genuine 11-kt-with-gusts Walchi sessions as MAYBE; fitted on n=34 backfilled days, 6 days flipped MAYBE→GO with no NO_GO disturbed (`f6b5cb9`)

## The throughline

The project moved from research-placeholder thresholds → logging real Urfeld
outcomes → scoring forecasts against them → progressively redefining "a real
session" from *peak wind* to *sustained duration* to the realistic *11-kt Walchi
bar*.

Note on balance of work so far: most of the above is **scoreboard** — the
forecast rules themselves have only had a handful of thresholds data-fitted
(`config.py` still flags most as placeholders). The natural next step is using
the now-corrected 11-kt Actual labels to retune the *forecast* rules against
them.
