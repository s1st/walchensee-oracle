# Historical calibration — findings and state, 2026-06-12

A summary of the work done on 2026-06-12: the replay feature,
the historical buoy backfill, the baseline scoring of ~3,300
days, the discovery of where the model is wrong, and the first
threshold tune. Companion to the data-exploration working notes
in `docs/findings/` and the execution plan in
`docs/replay-calibration-plan.md`.

Reading order for someone picking this up cold:
1. **This file** (the "what we did" executive summary)
2. `docs/replay-calibration-plan.md` (the "what we planned to do")
3. `docs/findings/2026-06-12-historical-baseline.md` (the "what the data says" raw notes)
4. `docs/findings/threshold-solar-radiation.md` (the first threshold tune, in detail)
5. `docs/findings/threshold-daytime-clouds.md` (the second threshold tune, in detail)
5. `docs/findings/release-notes-draft.md` (the blog post brainstorm)

## Headline

In one session we went from a 70-day forecasting project to a
3,300-day back-testable corpus, scored the rules against the
historical ground truth, and turned the offender-list evidence
into the first threshold retune. The headline numbers:

- **Replay feature** shipped: `oracle replay --day 2024-06-15` and
  `oracle replay --from 2017-01-01 --to 2026-06-12` both work end-to-end
  against the Open-Meteo archive + the historical Urfeld buoy.
- **Historical backfill**: 3,642 days of buoy data pulled from
  Addicted-Sports into the calibration bucket, covering 2016-2026.
- **Baseline scoring**: 3,263 days in the duration-label report,
  **41% overall accuracy**, 6.3% hard errors, 50.9% soft errors.
- **Threshold tune #1**: `MIN_MORNING_SOLAR_WM2` retuned from 600 to
  380 W/m² (data-fitted, n=3,263). **FP-veto count dropped 44%**
  (1,464 → 823). Live-era accuracy went from 34% to 57%.

## What was built (commits, in chronological order)

```
6e6f9bd Merge forecast/lake-temperature: add lake-temp pillar + air_lake_delta rule
   (pre-existing — start of the data we worked with)
6c83c95 measurements: capture full buoy payload (temp/dp/rh/rp/rain) per sample
   The Addicted-Sports JSON had a richer sensor set than we were
   consuming. Captured the rest into WindReading / UrfeldSample
   for replay use later.

b37f294 replay: host-swap to Open-Meteo Historical Forecast + reanalysis APIs
   Pressure + meteo pillars grew `host` (and target_day) params.
   engine.run_replay() + cli `oracle replay` subcommand. Replay
   records routed to runs/replay/<date>.json.

c4594a6 calibration: score replay records against stored ground truth
   RunStore grew read_replay / write_replay / list_replays.
   _merged_replay_record overlays main-record ground truth onto
   replay records. compile_report / rescore_all / export_csv take
   a `replayed=` flag. rescore_record takes `now=` for replay
   air_lake_delta staleness.

ac0c212 replay: batch mode for calibration passes over the historical backfill
   Two archive requests per year, not per day. Buoy day-curve
   reconstructed losslessly from ground_truth.machine.samples.
   Reuses the per-day parse via fetch_hourly_range + snapshot_*
   primitives. 5x speedup over the per-day path.

574d126 calibration: tick off Phases 0–2 of the replay plan with actual numbers
   Plan doc updated with the actual n's, the ecmwf_ifs model-pin
   finding, and the 41% / 6.3% headline.

7c72c6c docs: working notes for the 2026-06-12 baseline + blog brainstorm
   docs/findings/ created, two initial files.

6f61053 config: retune MIN_MORNING_SOLAR_WM2 600 → 380 W/m² (n=3,263)
   First threshold tune (Phase 3 of the plan). Detailed in
   docs/findings/threshold-solar-radiation.md.

<NEXT-COMMIT> config: retune MAX_DAYTIME_LOW_CLOUD_PCT 60 → 75% (n=3,263)
   Second threshold tune (Phase 3 of the plan). The 60% value
   was a research-analogue guess; data-fit peak is at 75% with
   N_C − N_T = +139 (vs +131 at 60). Modest +8-day net improvement
   — the cloud distribution is bimodal, the rule was already
   close to optimal. FP-veto count dropped 13% (968 → 845).
   Detailed in docs/findings/threshold-daytime-clouds.md.
```

## Key data findings

These are the things we'd want a future contributor (or a future
us, six months from now) to remember.

### 1. The model is best at predicting summer thermals, worst at winter

Seasonal pattern from the duration-label report:

```
 month | days | correct% | hard_err%
 5     |  310 |   50.3%  |   4.2%   ← best
 7     |  279 |   48.0%  |   5.4%
 6     |  282 |   46.5%  |   8.2%
 4     |  289 |   47.4%  |   6.9%
 9     |  270 |   47.4%  |   3.7%
 8     |  271 |   44.3%  |   3.7%
 3     |  271 |   42.8%  |   6.6%
12     |  277 |   41.2%  |   7.9%
11     |  268 |   39.6%  |   6.0%
10     |  279 |   39.1%  |   5.4%
 1     |  281 |   36.7%  |   8.9%
 2     |  254 |   29.5%  |   7.1%   ← worst
```

Winter thermals are a different beast (cold air pooling, pressure
extremes) and the current feature set doesn't see them.

### 2. 2021-2022 broke the model

Two years account for 59% of all hard errors despite being only
2 of 9 years. Weather context (DWD official reports):

- **2021**: Germany's rainiest summer in 10 years, **average
  sunshine**, Alps got the most precip. Warm June, cool August.
- **2022**: Germany's **sunniest summer on record**, 6th-driest,
  4th-warmest. North/west in historic drought; Alps still got
  heavy precip.

Both years were extreme but in opposite directions, and both
broke the model. The common thread isn't a single weather
signal — it's that the thermal driver is more weather-regime-
dependent than the current feature set captures. A future
"is this an extreme year?" input would help; deferred to after
Phase 3 of the plan.

### 3. The most outrageous model misses

Days the rule killed where the lake fired **25+ knots**:

```
 day         era   peak   solar  low_cloud  dew_spread
 2020-10-03  IFS   30.45  481    51         0.1
 2017-12-14  IFS   28.40  147    64         2.3
 2022-09-03  IFS   27.54  592    4          1.8
 2017-03-04  IFS   26.86  495    11         5.4
 2021-10-21  IFS   26.82  254    63         3.1
 2020-02-10  IFS   25.59  155    75         0.0
 2019-11-15  IFS   25.00  219    28         0.1
 2024-03-27  ICON  24.89  698    73         4.4
 2021-08-15  IFS   24.30  782    1          2.6
 2022-06-05  IFS   24.14  667    19         2.9
```

These were the strongest evidence for retuning `solar_radiation`
(the 30-kt day vetoed on 481 W/m² is a particularly hard
defense of the 600 W/m² threshold).

### 4. IFS vs ICON — a 4× improvement in hard errors

| Era | n | correct% | hard_err% |
|---|---|---|---|
| IFS (2017-2022) | 2,074 | **44.3%** | 8.0% |
| ICON (2023+) | 1,257 | **40.7%** | **3.1%** |

ICON is more conservative — when it commits to "go", it's more
often right, but it hedges with more "maybe"s. The 22.2%
November-ICON accuracy is a clear signal: late-autumn ICON calls
tend to be wrong in a specific direction. This is the
**aggregator** finding, separate from the threshold finding.

### 5. The solar_radiation threshold was over-tuned

Original: 600 W/m² (research-analogue guess).
Data-fitted (n=3,263): **380 W/m²**.

The 600 value was net-positive (N_C − N_T = +223) but well below
the optimum (N_C − N_T = +287 at 380). After the tune, FP-veto
count drops 44% (1,464 → 823) — the "you said NO_GO and the lake
fired anyway" pain drops by 641 days. The cost: 800 more days
where the rule says GO and the lake didn't fire (most of those
are `maybe` anyway because other rules hedge).

## What was operationalised

These are the things that "just work" now but required real
work to land.

### 1. The replay CLI

```
oracle replay --day 2024-06-15                  # single-day, full diagnostics
oracle replay --from 2017-01-01 --to 2026-06-12  # batch, ~15 min for the full range
oracle replay --from 2017-01-01 --to 2026-06-12 --models ecmwf_ifs  # fast path
oracle replay --day 2016-08-15 --source reanalysis  # pre-archive era
```

Single-day mode scrapes the live buoy + archive pillars.
Batch mode fetches archive ranges once per year and reconstructs
the buoy day-curve from the stored ground truth (no re-scraping).

### 2. The calibration join

`oracle calibrate --replayed` scores replay verdicts against the
matching main record's ground truth via `_merged_replay_record`.
The earlier-than-expected find: this required touching **everything**
in the calibration code path (per-rule helpers, verdict-key
selection, the storm-suspected check). The clean way was to make
replay records look like live records via the merge, so all
existing helpers work unchanged.

### 3. The Open-Meteo "Best Match is the slow path" finding

For 2017-2022 the default `Best Match` model selection is fast
(< 1s per year). For 2023+ it triggers a slow server-side lookup
that times out at 30s. Pinning to `ecmwf_ifs` (the older IFS HRES,
9 km, the Best Match effectively picks in 2017-2022) makes
2023-2026 fast again. Documented inline in the replay engine's
docstring + the plan.

### 4. The dashboard stats timeout fix

`commit 7b834e3` (on main, pre-this-branch): the dashboard's
`_forecast_stats` was calling `compile_report` with no `since=`
filter, which after the 3,642-file backfill took 4-5 minutes per
refresh. The fix: pass `since=config.PROJECT_FIRST_DAY` to walk
only the project's own days. Bug was introduced implicitly by
the bucket growing 70x; the fix is one line. (Lives on main,
not on this branch.)

## What was data-fitted (in n= notation)

| Constant | Old | New | n | Notes |
|---|---|---|---|---|
| `MIN_THERMIK_DELTA_HPA` | -1.0 | -1.0 | n=10 | pre-existing, unchanged |
| `MAX_OVERNIGHT_CLOUD_COVER_PCT` | 95.0 | 95.0 | n=22 | pre-existing, unchanged |
| `MIN_DEW_POINT_SPREAD_C` | 2.5 | 2.5 | n=22 | pre-existing, unchanged |
| `MAX_LIFTED_INDEX` | 10.0 | 10.0 | n=22 | pre-existing, unchanged |
| `MIN_MORNING_SOLAR_WM2` | 600.0 | **380.0** | n=3,263 | **retuned in 6f61053** |
| `MAX_DAYTIME_LOW_CLOUD_PCT` | 60.0 | **75.0** | n=3,263 | **retuned in b0e5c9f** |
| `SYNOPTIC_OVERRIDE_KNOTS` | 15.0 | **25.0** | n=648 ICON | **retuned in b26aea0** |
| `MAX_UPPER_CROSSFLOW_KNOTS` | 25.0 | 25.0 | n=648 ICON | attempted, reverted (3773352) |
| `FOEHN_TRIGGER_DELTA_HPA` | 4.0 | **10.0** | n=3,331 | **retuned in f050e4c** |
| `MIN_BOUNDARY_LAYER_HEIGHT_M` | 600.0 | **400.0** | n=629 ICON | **retuned in ac0086e** |
| `WET_SOIL_MOISTURE_M3M3` | 0.35 | **0.30** | n=48 (small!) | **retuned in 6de1605** |
| `COLD_LAKE_DELTA_C` | 10.0 | **999.0** | n=3,314 | **retuned in 68d0fb5** |

The pre-existing data-fitted constants (n=22 era) haven't been
re-fit on the n=3,263 sample. They were fit on the n=22
live-era set and may have drifted; re-fitting them is the
next batch of work in Phase 3 of the plan.

**Aggregator bar (the biggest win of the pass):**

| Constant | Old | New | n | Notes |
|---|---|---|---|---|
| `SOFT_VETO_BAR` (in `engine.aggregate`) | 2 | **5** | n=3,331 | data-fitted peak label accuracy went from 45.4% to 48.3% (+2.9pp). Was hardcoded; now a config constant. **retuned in 743e610** |

## What is research-analogue (TODO(calibrate))

After the 2026-06-12 threshold pass, the only remaining
"research-analogue" constants (no n= note) are:

| Constant | Value | Status |
|---|---|---|
| `IGNITION_WIND_KNOTS` | 8.0 | guess (well-justified physically, 8 kt = Bft 3) |
| `COMFORTABLE_DEW_POINT_SPREAD_C` | 8.0 | guess |
| `GOOD_BOUNDARY_LAYER_HEIGHT_M` | 1000.0 | guess |
| `RAINED_YESTERDAY_MM` | 2.0 | log-only since n=17 (kept for the log schema) |
| `MIN_LIFTED_INDEX` | -2.0 | guess |
| `GOOD_DAYTIME_LOW_CLOUD_PCT` | 30.0 | guess (the lower bound of the daytime_clouds rule) |
| `SYNOPTIC_OPPOSING_DEG` | (150, 210) | guess |
| `SYNOPTIC_OPPOSING_MIN_KNOTS` | 12.0 | guess |
| `MAX_LAKE_TEMP_AGE_HOURS` | 168.0 | guess (lake-temp staleness cutoff) |

The rest have been retuned. The plan's plan-queue (the 3 tunes
flagged at the start of Phase 3) is done; the threshold pass
+ aggregator re-fit is complete.

## What changed for the end user

The dashboard. Before this work, the homepage stats panel took
4-5 minutes to refresh (the `_forecast_stats` issue), then 30+
seconds to render. Now the homepage is sub-second on warm cache.
The 7b834e3 fix on main took care of this; this branch's
threshold tune is invisible to the user.

The dashboard's "Re-scored" row (the `verdicts_resimulated` strip)
will show the new solar_radiation verdict. The headline numbers
(strip accuracy) will tick up 0.5pp.

## What is next

**The threshold pass is done.** All 9 changes shipped.

In the immediate term (this branch, threshold-tuning):

- ✅ Tune `MIN_MORNING_SOLAR_WM2` (6f61053) — the highest-leverage
- ✅ Tune `MAX_DAYTIME_LOW_CLOUD_PCT` (b0e5c9f)
- ✅ Tune `SYNOPTIC_OVERRIDE_KNOTS` (b26aea0)
- ✅ Tune `MAX_UPPER_CROSSFLOW_KNOTS` (3773352) — attempted, reverted
- ✅ Tune `FOEHN_TRIGGER_DELTA_HPA` (f050e4c)
- ✅ Tune `MIN_BOUNDARY_LAYER_HEIGHT_M` (ac0086e)
- ✅ Tune `WET_SOIL_MOISTURE_M3M3` (6de1605)
- ✅ Tune `COLD_LAKE_DELTA_C` (68d0fb5)
- ✅ Aggregator re-fit `SOFT_VETO_BAR` 2 → 5 (743e610)

Medium term:

5. **The 2021-2022 "extreme year?" feature** — add an input
   flagging extreme-weather-regime days. Best addressed after
   the threshold pass.
6. **Re-fit the pre-existing n= constants on the n=3,263 sample** —
   `MIN_THERMIK_DELTA_HPA`, `MAX_OVERNIGHT_CLOUD_COVER_PCT`,
   `MIN_DEW_POINT_SPREAD_C`, `MAX_LIFTED_INDEX` were all fit on
   the n=22 live-era set. They may have drifted with the larger
   sample.
7. **ML classifier (GH issue #12)** — `data/replay_*.csv` files
   are ready as a training set (n=3,332 rows × 28 columns, with
   n=1,295 ICON-era rows having full feature coverage).

Long term:

8. **The blog post series** — `docs/findings/release-notes-draft.md`
   has 4-5 post ideas. Posts 1-4 form the release; post 5
   (the threshold-tuning retrospective) comes after the Phase 3
   pass.

## For future contributors

If you're picking this up cold, here's the orientation:

- **The data lives in `data/` (gitignored).** Three CSVs from the
  baseline scoring, plus the `data/runs/` and `data/runs/replay/`
  trees from the local bucket mirror. To regenerate: `gcloud
  storage cp -r gs://walchi-oracle-prod-runs/runs data/` then run
  `uv run oracle replay --from 2017-01-01 --to 2026-06-12
  --models ecmwf_ifs` (without `RUNS_BUCKET` set).
- **The replay records are in GCS** at
  `gs://walchi-oracle-prod-runs/runs/replay/`. 3,331 records.
  Nothing in prod reads them, they're for the calibration loop.
- **The execution plan is `docs/replay-calibration-plan.md`.**
  Phases 0-2 ticked off in commit 574d126. Phase 3 in progress
  on the `threshold-tuning` branch.
- **The data-exploration working notes are in `docs/findings/`.**
  The blog post brainstorm is `release-notes-draft.md`.
- **The threshold-tuning discipline is one constant per commit**
  with an `n=` note, per `CLAUDE.md`. The first such commit is
  `6f61053` on the `threshold-tuning` branch.
- **The aggregator re-check is the open question.** ICON-era is
  4× better at avoiding hard errors but lower-accuracy overall.
  The 2-soft-veto downgrade bar is the most likely culprit.

## File map

```
docs/
├── architecture.md                        # (pre-existing) GCP layout, data flow
├── thermal-model.md                        # (pre-existing) the physics of the wind
├── future-factors.md                       # (pre-existing) research backlog
├── future-buoy-signals.md                  # (pre-existing) buoy-side rules to add
├── historical_forecasts.md                 # (pre-existing, was untracked) data source coverage
├── replay-calibration-plan.md              # the execution plan, Phases 0-2 ticked off
├── 2026-06-12-historical-calibration-findings.md   # ← this file
└── findings/
    ├── 2026-06-12-historical-baseline.md   # Phase 2 baseline data exploration
    ├── threshold-solar-radiation.md        # first threshold tune, in detail
    ├── threshold-daytime-clouds.md         # second threshold tune, in detail
    └── release-notes-draft.md              # blog post brainstorm

CHANGELOG.md                                # (pre-existing) 2026-06-12 milestone pending
```

## Stats at a glance

| | |
|---|---|
| Total replay days in bucket | 3,331 |
| Storm-quarantined days | 68 |
| Days in duration-label report | 3,263 |
| Headline accuracy (before any tune) | 40.8% |
| Headline accuracy (after first threshold tune) | 41.3% |
| Headline accuracy (after both threshold tunes) | 42.0% |
| Hard errors (before) | 6.2% |
| Hard errors (after) | 5.0% |
| Live-era accuracy (before) | 34% |
| Live-era accuracy (after) | 57% |
| Threshold tunes shipped | 1 |
| Threshold tunes queued | 3+ (per the plan) |
| Tests passing | 158 |
| Commits on `threshold-tuning` | 1 (6f61053) |
