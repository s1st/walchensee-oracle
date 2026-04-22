---
name: walchi-oracle
description: Forecast thermal wind conditions at Lake Walchensee (Bavaria). Use when the user asks whether it'll be windy / thermal / surfable / kitable at Walchensee, Urfeld, Galerie, Sachenbach, Wiese, Zwergern, Herzogstand, or when planning a kite/windsurf trip to the Bavarian pre-Alps.
homepage: https://github.com/s1st/walchensee-oracle
emoji: 🌬️
requires:
  - oracle           # the walchensee-oracle CLI, installed via pip from source
bins:
  - oracle
id: walchi-oracle
kind: domain-forecast
---

# Walchi Thermic Oracle

Domain-specific forecaster for the thermal wind at Lake Walchensee. Combines
four live data sources the user's models cannot see together:

- Cross-Alps pressure gradient Munich–Innsbruck ("Thermik") and Föhn
  detection Bolzano–Innsbruck (Open-Meteo MSL pressure).
- Meteorological conditions — overnight cooling, morning solar radiation,
  dew-point spread, boundary-layer height, soil moisture + recent rain,
  lifted index, daytime low-cloud development, 850 hPa wind
  direction + 700 hPa crossflow (all Open-Meteo).
- Live shore wind — Addicted-Sports Urfeld anemometer + nearest DWD
  synoptic station via Bright Sky.
- Community chat — windinfo.eu Wind-Wetter-Chat (filtered to
  Walchensee-mentioning messages).

## ✅ When to use

Invoke when the user asks about:

- Wind / thermal / Thermik / Wind conditions **at** Walchensee, Walchi, Urfeld,
  Galerie, Sachenbach, Nordufer, Zwergern, Wiese, Einsiedl, Kesselberg,
  Herzogstand, or Jochberg.
- Whether it's worth driving from Munich to the lake today / tomorrow.
- A kite, windsurf, wing, or sailing session at Walchensee.
- "Walchi-Check", "Windcheck Walchensee", "Thermikcheck".

Also invoke proactively in the morning (if the agent has a schedule concept)
between April and September, when the user has previously asked about Walchensee.

## ❌ When NOT to use

- Wind / weather questions about any *other* lake or spot. Use the generic
  `weather` skill instead.
- Generic pressure / radiation questions unrelated to Walchensee.
- Historical research, climate science, or anything requiring past-date data
  older than seven days — the tool only forecasts today and near-future days.

## Commands

Run the CLI with `--json` to get structured output:

```
oracle forecast --json                    # today
oracle forecast --day 2026-05-15 --json   # specific ISO date
oracle forecast --horizon 3               # loop over today + next 2 days
oracle backfill --day 2026-05-15          # merge actual Urfeld wind curve
```

The JSON shape (12 rules, each verdict carries bilingual reasons):

```
{
  "day": "2026-04-23",
  "overall": "go" | "maybe" | "no_go",
  "verdicts": [
    {"rule": "thermik", "signal": "go|maybe|no_go",
     "reason_en": "…", "reason_de": "…"},
    // … 11 more: foehn_override, overnight_cooling, solar_radiation,
    //           dew_point_spread, boundary_layer_height, post_rain_moisture,
    //           atmospheric_stability, daytime_clouds, upper_level_wind,
    //           synoptic_override, thermal_ignition
  ],
  "chat_messages": [
    {"posted_at": "...", "author": "...", "channel": "...", "text": "..."}
  ]
}
```

## How to respond to the user

1. Summarise the **`overall`** verdict in one short German sentence — don't
   just echo `"no_go"`. Phrase it as a session-planning call.
   - `go` → "Heute läuft der Walchi — Thermik sieht solide aus."
   - `maybe` → "Grenzwertig am Walchensee — könnte reichen, eher Plan B."
   - `no_go` → "Heute kein Walchi-Tag — [top two blocking reasons in Stichworten]."
2. Then list the **blocking verdicts** (signal = no_go) as short bullets,
   translating the `reason` field to a human tone. Keep it terse.
3. If there are **chat_messages**, include the most recent 1–2 in quotes with
   the author's name as attribution. The chat is often the deciding signal
   when the numerics are borderline.
4. Keep the full reply under ~8 lines. The user is deciding whether to drive
   80 km, not reading a report.

## Notes

- **Thresholds are placeholders** calibrated from Garda analogues, not from
  logged Walchensee sessions. A `go` verdict is still experimental — flag
  uncertainty when the numerics are borderline.
- The `thermal_ignition` rule returns `maybe` before ~10:30 local time
  because the thermal hasn't had a chance to start. That's expected, not a
  failure.
- The `windinfo.eu` chat source requires `WINDINFO_USER` / `WINDINFO_PASS`
  env vars. If they're missing the `chat_messages` array will be empty; still
  produce a forecast from the numeric signals.
- Time zone is Europe/Berlin.
