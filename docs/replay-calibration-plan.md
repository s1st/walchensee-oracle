# Historical calibration — execution plan

Status as of 2026-06-12: all tooling is in place (`replay-calibration`
branch). The runs bucket holds ~3,600 in-season ground-truth days
(2016–2026, full buoy payload); the batch replay + scoring join are
implemented and smoke-tested (n=3). What remains is running the actual
calibration and acting on the results.

Background: `docs/historical_forecasts.md` (data coverage per era),
`CLAUDE.md` → Thresholds (tuning discipline).

## Phase 0 — merge + local mirror

- [x] Merged `replay-calibration` into `main` as `a7516f9` (no-ff
      merge commit, the branch's narrative is worth keeping).
- [x] Mirrored the bucket locally: 3,699 files (99 MB) in `data/runs/`
      via `gcloud storage cp -r gs://walchi-oracle-prod-runs/runs
      data/`, took 24s. Saved the pre-existing 41 local files to
      `data/runs.localbackup/` (irrelevant now that the mirror is
      authoritative).
- [x] Ran everything without `RUNS_BUCKET`; replays wrote to
      `data/runs/replay/` (3,331 records). Did not sync back to GCS
      — nothing in prod reads them per the plan.

## Phase 1 — the one-time batch replay (~15 min, ~20 archive requests)

- [x] `oracle replay --from 2017-01-01 --to 2026-06-12` (2026-06-12 is
      today's backfilled stub; the plan said 06-11, included the
      backfill day)
- [x] Review the skipped list: archive holes are expected, but a whole
      skipped *year* means something structural (request error, model
      gap) worth checking before trusting the sample.
      **Finding:** the default "Best Match" model path is the slow one
      for 2023-2026 (Best Match picks a model that triggers a slow
      server-side lookup). Re-running with `--models ecmwf_ifs` (the
      older IFS HRES) made 2023-2026 fetch in 0.5-1s for full-year
      requests. Used `ecmwf_ifs` for the second pass.
- [x] Sanity-check coverage: `oracle calibrate --replayed --label duration`
      reports **n = 3,263** (after 68-day storm quarantine). Era split:
      IFS-era (2017-01-01 → 2022-11-23) **n = 1,968**; ICON-era
      (2022-11-24 → 2026-06-12) **n = 1,295**.

Decision taken: pin to `ecmwf_ifs` for the entire 2017-2026 replay
(both the original 2017-2022 pass at the natural Best Match and the
retry). Cross-era comparability of the thermik/Föhn pressure signal
was the rationale; the live pipeline effectively uses Best Match
too, so era-disjoint pinning means the replayed deltas match what
the live deltas would have been at the time.

Caveat: ICON-era days (2022-11-24+) had IFS HRES still be the
highest-resolution model in the historical-forecast API for the
2-3 month window between ICON-D2 launching (2022-11-24) and
IFS 0.25° joining (2024-02-03). For 2024-02-03 onward the live
pipeline may be using IFS 0.25° via Best Match — that era is
slightly under-modeled in the replay. A future `--models
ecmwf_ifs025` retry for 2024-02-03 → today would close it, but
the size of the calibration sample (n=3,263) is plenty for the
threshold-tuning questions.

## Phase 2 — first real scoring pass

- [x] `oracle calibrate --replayed --label duration` — baseline matrix
      + per-rule offender table recorded below.
- [x] Score the two eras separately:
      - `--until 2022-11-23` (IFS-era: ~7 effective rules, no BLH / soil
        moisture) — **n=1,968, accuracy 43%**
      - `--since 2022-11-24` (ICON-era: all 13 rules) — **n=1,295, accuracy 37%**
- [x] Export the ML dataset: `oracle calibrate --replayed --csv
      data/replay_full.csv` (3,332 rows × 28 columns) + per-era splits
      `data/replay_ifs_era.csv` and `data/replay_icon_era.csv` for
      GH issue #12 (ML classifier, was waiting for n≥50).
- [x] Save the three report outputs (recorded in the dated section
      below) — they are the baseline every threshold tune is compared
      against.

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
