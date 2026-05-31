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

Domain-specific forecaster for the thermal wind at Lake Walchensee. It fuses
cross-Alps pressure, meteorological conditions, and live shore-wind readings —
signals that global models (Windy, Windguru) can't resolve — into a single
`GO` / `MAYBE` / `NO_GO` call for the day. Full data-source and rule list lives
in the project README; don't re-document it here.

## ✅ When to use

Invoke when the user asks about:

- Wind / thermal / Thermik conditions **at** Walchensee, Walchi, Urfeld,
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
- Historical research older than ~seven days — the tool forecasts today and
  near-future days only.

## How to run it

```
oracle forecast --json                    # today, machine-readable
oracle forecast --day 2026-05-15 --json   # specific ISO date
oracle forecast --horizon 3 --json        # today + next 2 days
```

The JSON is self-describing: an `overall` verdict plus one entry per rule, each
carrying `signal`, `reason_en`, and `reason_de`. Read what the command returns
rather than relying on a schema copied here — the README has the canonical rule
list and CLI reference.

## How to respond to the user

1. Summarise the **`overall`** verdict in one short German sentence — don't
   just echo `"no_go"`. Phrase it as a session-planning call.
   - `go` → "Heute läuft der Walchi — Thermik sieht solide aus."
   - `maybe` → "Grenzwertig am Walchensee — könnte reichen, eher Plan B."
   - `no_go` → "Heute kein Walchi-Tag — [top two blocking reasons in Stichworten]."
2. List the **blocking verdicts** (signal = `no_go`) as short bullets,
   translating the reason to a human tone. Keep it terse.
3. Keep the reply under ~8 lines — the user is deciding whether to drive 80 km,
   not reading a report.

## Notes

- **Partly calibrated** — the main driver rules are fitted against logged Urfeld
  sessions; the rest are still research-analogue guesses. A `go` is improving
  but not yet fully proven, so flag uncertainty when the numbers are borderline.
- `thermal_ignition` stays `maybe` until a shore station actually reads ≥ 8 kt;
  in the morning, before the thermal has fired, that's expected, not a failure.
- Time zone is Europe/Berlin.
