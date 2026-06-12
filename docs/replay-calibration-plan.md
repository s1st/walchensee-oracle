# Historical calibration — execution plan

Status as of 2026-06-12: all tooling is in place (`replay-calibration`
branch). The runs bucket holds ~3,600 in-season ground-truth days
(2016–2026, full buoy payload); the batch replay + scoring join are
implemented and smoke-tested (n=3). What remains is running the actual
calibration and acting on the results.

Background: `docs/historical_forecasts.md` (data coverage per era),
`CLAUDE.md` → Thresholds (tuning discipline).

## Phase 0 — merge + local mirror

- [ ] Merge `replay-calibration` into `main` (PR or fast-forward).
- [ ] Mirror the bucket locally so the iterate loop doesn't pay GCS
      latency on every pass (~7,000 reads otherwise):
      `gcloud storage cp -r gs://walchi-oracle-prod-runs/runs data/`
      then run everything **without** `RUNS_BUCKET`. Replays written
      locally can be synced back to the bucket at the end
      (`gcloud storage cp -r data/runs/replay gs://walchi-oracle-prod-runs/runs/`)
      — or kept local-only; nothing in prod reads them.

## Phase 1 — the one-time batch replay (~15 min, ~20 archive requests)

- [ ] `oracle replay --from 2017-01-01 --to 2026-06-11`
      (2016 has no forecast archive — IFS HRES starts 2017-01-01; skip
      or revisit with `--source reanalysis` later, see Phase 4.)
- [ ] Review the skipped list: archive holes are expected, but a whole
      skipped *year* means something structural (request error, model
      gap) worth checking before trusting the sample.
- [ ] Sanity-check coverage: `oracle calibrate --replayed --label duration`
      should report n in the low thousands. Record n in this file.

Decision deliberately taken: default Best Match model (no `--models`
pin) for the first pass — highest-resolution model per era, which is
what the live pipeline effectively uses. Pin a model only if a later
analysis needs cross-era comparability of a specific variable.

## Phase 2 — first real scoring pass

- [ ] `oracle calibrate --replayed --label duration` — the headline
      confusion matrix + per-rule offender table.
- [ ] Score the two eras separately (variable coverage differs — five
      rules emit no-signal MAYBE pre-2021/2022):
      - `--until 2022-11-23` (IFS-era: ~7 effective rules)
      - `--since 2022-11-24` (ICON-era: all 13 rules)
- [ ] Export the ML dataset while at it:
      `oracle calibrate --replayed --csv data/replay_full.csv`
- [ ] Save the three report outputs (paste into a dated section below or
      commit alongside) — they are the baseline every threshold tune is
      compared against.

Interpretation cautions:
- **Storm quarantine is blind pre-2021** (no lifted index in the
  archive): some gust-front days will sit in the matrix as false
  "sessions". If the pre-2021 FP numbers look wild, check a few
  offender days by hand before blaming a threshold.
- **Replay ≈ lead-time-0 forecast** (first hours of each model run).
  Accuracy here is an upper bound on what the 08:00 day-of forecast can
  do, not a measure of day+1/day+2 skill.

## Phase 3 — threshold tuning loop (the payoff)

Per `CLAUDE.md`: **one threshold per commit**, offender-list evidence
first. Now with n≈3,000 instead of n≈22. The research-analogue
constants (no `n=` note in `config.py`) are the queue, roughly in
expected-impact order from the calibration backlog + the n=3 smoke:

- [ ] `MIN_MORNING_SOLAR_WM2` (600) — `solar_radiation` was an offender
      even at n=3; cloud-era Walchensee may fire below 600 W/m².
- [ ] `MAX_DAYTIME_LOW_CLOUD_PCT` (60) — `daytime_clouds` likewise.
- [ ] `SYNOPTIC_OVERRIDE_KNOTS` (15) — backlog already suspects
      HARD→SOFT or a higher bar (open item from 2026-06-11 pass).
- [ ] `MAX_UPPER_CROSSFLOW_KNOTS` (25) — crossflow veto was 0/2 in the
      live log; the replay sample finally gives it a real denominator.
- [ ] `FOEHN_TRIGGER_DELTA_HPA` (4.0), `MIN_BOUNDARY_LAYER_HEIGHT_M`
      (600, ICON-era only), `WET_SOIL_MOISTURE_M3M3` (0.35, ICON-era
      only), `COLD_LAKE_DELTA_C` (10).

Loop per threshold:
1. Change the constant in `config.py` (with an `n=` note).
2. `oracle rescore --replayed` (seconds locally, no API).
3. `oracle calibrate --replayed --resimulated --label duration` — compare
   against the Phase 2 baseline (FP-vetos down, FN-greens not up).
4. Also re-check the live-era strip as before:
   `oracle rescore --since 2026-04-22 && oracle calibrate --resimulated --since 2026-04-22`
   so a historically-better threshold doesn't degrade the current season.
5. Commit (one threshold per commit), note old → new + n in the message.

## Phase 4 — optional extensions (only if Phases 1–3 leave questions)

- [ ] **2016 + pre-2017 via reanalysis**: `oracle replay --from 2016-06-01
      --to 2016-12-31 --source reanalysis`. Note: replay records key on
      day only — a reanalysis pass *overwrites* a historical-forecast
      replay of the same day. Keep sources era-disjoint or accept the
      overwrite.
- [ ] **ML classifier (GH issue #12)**: `data/replay_full.csv` from
      Phase 2 is the training set the issue was waiting for (it wanted
      n≥50; this is n≈3,000 with ~1,200 full-feature ICON-era rows).
      Update the issue with the dataset location + era caveats.
- [ ] **Aggregator re-fit**: with thousands of days, the severity-tier /
      consensus semantics themselves (not just thresholds) can be
      evaluated — e.g. is the 2-soft-veto downgrade bar right?

## Out of scope (decided)

- Dashboard surfacing of replay stats — CLI-only until the tuning loop
  proves out.
- Parallel GCS reads — the local mirror covers it.
- Lead-time skill evaluation (day+1/day+2 forecasts) — would need the
  Previous Runs API and a different replay shape entirely.
