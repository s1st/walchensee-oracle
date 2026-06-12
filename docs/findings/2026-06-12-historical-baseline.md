# Historical calibration baseline — 2026-06-12

Working notes for the data exploration that produced the Phase 2 baseline
in `docs/replay-calibration-plan.md`. Material here is what eventually
becomes the blog posts sketched in `release-notes-draft.md` (sister
file in this directory).

Authoring stance: the audience for the eventual blog posts is
**data-interested people** — folks who read [FiveThirtyEight](https://fivethirtyeight.com/),
follow [The Pudding](https://pudding.cool/), and care more about
"what did you find in the data" than "what's your tech stack".
So the writeup will lean on the data, not the engineering. These notes
keep the engineering context in for traceability.

## Setup

- 3,331 days of historical forecasts replayed against the Open-Meteo
  archive (commits `ac0c212`, `b37f294` for the replay feature;
  commit `c4594a6` for the join to ground truth).
- 3,642 buoy days backfilled from the Addicted-Sports archive
  (commit `6c83c95` made the schema capture the full sensor set).
- 3,263 days in the duration-label report (68-day storm quarantine).
- All numbers in this file come from `data/replay_full.csv` and the
  on-disk replay records under `data/runs/replay/`. The CSVs are
  gitignored; the replays are also in GCS at
  `gs://walchi-oracle-prod-runs/runs/replay/`.
- All analysis below is reproducible from the CSVs in two
  `uv run python -c` snippets; snippets are inlined so the next
  person can re-derive without re-discovering.

## Headline numbers

| Metric | Value |
|---|---|
| Total replay days | 3,331 |
| Storm-quarantined days (excluded from scoring) | 68 |
| Days in the duration-label report | 3,263 |
| **Overall accuracy (forecast = actual)** | **41%** |
| Hard errors (go↔no_go) | 205 (6.3%) |
| Soft errors (anything involving maybe) | 1,697 (52.0%) |
| Correct | 1,361 (41.7%) |

The 41% is *worse* than the n=22 live-era figure of 52% that was in
the project history — expected given (a) the broader sample and
(b) the live-era figure was hand-validated on known sessions
whereas this is blind scoring against the buoy outcome.

The 6.3% hard-error rate is the most actionable number. These are
days the rules said **go** when the lake actually didn't fire, or
vice versa. The 1,697 soft errors are days the rules landed on
`maybe` and the lake was a confident `go` or `no_go` — much less
catastrophic, mostly just noisy for the user.

## The seasonal pattern

Forecast accuracy is sharply worse in winter and best in peak summer:

```
 month | days | correct% | hard_err%
 1     |  281 |   36.7%  |   8.9%
 2     |  254 |   29.5%  |   7.1%   ← worst (deep winter, lake barely fires)
 3     |  271 |   42.8%  |   6.6%
 4     |  289 |   47.4%  |   6.9%
 5     |  310 |   50.3%  |   4.2%   ← best (peak season)
 6     |  282 |   46.5%  |   8.2%
 7     |  279 |   48.0%  |   5.4%
 8     |  271 |   44.3%  |   3.7%
 9     |  270 |   47.4%  |   3.7%
10     |  279 |   39.1%  |   5.4%
11     |  268 |   39.6%  |   6.0%
12     |  277 |   41.2%  |   7.9%
```

**Why this matters for the post:** the Walchensee thermal is a summer
phenomenon (slope heating on Herzogstand/Jochberg) and the model
captures the physics reasonably when the driver is in season.
Winter thermals are a different beast (cold air pooling, pressure
extremes) and the current feature set doesn't see them. A winter
"GO" call that fires is luck; a winter "NO_GO" call that fires is
a blind spot.

## The 2021-2022 anomaly

Two years account for **59% of all hard errors** despite being only
2 of 9:

```
 year |  n  | correct% | hard_err%
 2017 | 364 |   48.1%  |   3.3% hard
 2018 | 355 |   45.6%  |   3.9% hard
 2019 | 266 |   44.4%  |   5.6% hard
 2020 | 366 |   50.0%  |   3.0% hard     ← best year
 2021 | 358 |   40.8%  |  13.4% hard     ← bad year (47 hard errors)
 2022 | 365 |   36.7%  |  18.1% hard     ← worst year (65 hard errors)
 2023 | 364 |   42.9%  |   1.6% hard     ← ICON kicks in
 2024 | 366 |   37.7%  |   5.5% hard
 2025 | 364 |   38.2%  |   1.9% hard
 2026 | 163 |   47.9%  |   3.7% hard
```

What was true weather-wise (DWD official reports, via web search
2026-06-12):

- **2021**: Germany's rainiest summer in 10 years (+30% precip). Warm
  June, cool August, **average sunshine** (615 hrs). The Alps got
  the *most* precip (>700 l/m² in places). The Bernd floods in
  mid-July were the catastrophic event — but those hit western
  Germany, not Bavaria.
- **2022**: Germany's **sunniest summer on record** (820 hrs, +35%
  above 1961-1990 average). 6th-driest, 4th-warmest. 40.1°C in
  Hamburg. North/west in historic drought; **the Alps still got
  heavy precip** (>500 l/m²) with 114 mm in 24h on Aug 19.

So both years were extreme but in **opposite** directions (2021
wet-balanced, 2022 dry-extreme), and both broke the model. The
common thread isn't a single weather signal — it's that **the
thermal driver is more weather-regime-dependent than the current
feature set captures**.

This is the most novel finding and probably the lead of one of
the blog posts. The model implicitly assumes "normal summer
conditions". When the regime shifts (mega-drought, persistent
wet), the lake-thermal response also shifts in ways the model
doesn't see.

## IFS vs ICON — a 4× improvement in hard errors

The forecast engine pulls from the Open-Meteo `Best Match` model by
default, which for 2017-2022 is ECMWF IFS HRES and for 2023+ is
DWD ICON-EU / ICON-D2 (which became available in the archive
on 2022-11-24). Era split:

| Era | n | correct% | hard_err% | Story |
|---|---|---|---|---|
| IFS (2017-2022) | 2,074 | **44.3%** | 8.0% | Higher accuracy, more catastrophic misses |
| ICON (2023+) | 1,257 | **40.7%** | **3.1%** | Lower accuracy, 4× fewer hard errors |

**ICON is more conservative.** When the ICON model says "go", it's
more often right. But it hedges with more "maybe" verdicts, which
hurts the headline accuracy. The trade: IFS picks more winners
but loses more catastrophically.

Per-month the seasonal pattern is the same in both eras, but
ICON is notably worse in late autumn:

```
  ICON  October  33.3%   (vs IFS October 41.9%)
  ICON  November  22.2%   (vs IFS November 48.3%)  ← biggest gap
```

ICON's 22.2% in November is a clear signal: in late autumn the
ICON model produces something (a pressure pattern, a cloud cover
signal) that the rules interpret as "this looks like a thermal
day", but the lake actually doesn't fire. The most likely culprit
is a feature that means "thermally favourable" in summer (when
the rules were implicitly calibrated) but means "just grey and
cool" in late autumn.

This is the **aggregator** finding, separate from the **threshold**
finding. The thresholds were fitted on the IFS-era data; the
ICON-era shows a different error pattern.

## The most outrageous misses

Days the rule killed where the lake fired **25+ knots**:

```
 day         era   peak   solar  synop  low_cloud  dew_spread
 2020-10-03  IFS   30.45  481    NaN    51         0.1
 2017-12-14  IFS   28.40  147    NaN    64         2.3
 2022-09-03  IFS   27.54  592    4.2    4          1.8
 2017-03-04  IFS   26.86  495    NaN    11         5.4
 2021-10-21  IFS   26.82  254    5.5    63         3.1
 2020-02-10  IFS   25.59  155    NaN    75         0.0
 2019-11-15  IFS   25.00  219    NaN    28         0.1
 2024-03-27  ICON  24.89  698    NaN    73         4.4
 2021-08-15  IFS   24.30  782    3.3    1          2.6
 2022-06-05  IFS   24.14  667    6.4    19         2.9
```

People were definitely sailing on these days. The 30.45-kt day
(2020-10-03) is particularly striking — that was a real storm-of-a-
thermal and the rule vetoed because morning solar was 481 W/m²
(under the 600 W/m² threshold).

The current threshold of 600 W/m² for `MIN_MORNING_SOLAR_WM2` is
clearly over-conservative. The threshold-tuning work (Phase 3 of
the plan) will find a better value; preliminary data exploration
suggests the rule is net-negative at the current threshold (more
wrong vetoes than right vetoes — see `threshold-solar-radiation.md`
when it's written).

## Reproducing the analysis

Two `uv run python -c` snippets, copy-pastable:

```python
# Re-derive the headline numbers
import pandas as pd
df = pd.read_csv('data/replay_full.csv')
df['correct'] = df['forecast_overall'] == df['actual_verdict']
print(f'correct: {df["correct"].mean()*100:.1f}%')
hard = df['forecast_overall'].isin(['go', 'no_go']) & df['actual_verdict'].isin(['go', 'no_go']) & ~df['correct']
print(f'hard error: {hard.mean()*100:.1f}%')
```

```python
# Per-month / per-year / per-era breakdown
import pandas as pd
df = pd.read_csv('data/replay_full.csv')
df['date'] = pd.to_datetime(df['day'])
df['year'] = df['date'].dt.year
df['month'] = df['date'].dt.month
df['era'] = df['year'].apply(lambda y: 'IFS' if y < 2023 else 'ICON')
df['hard_error'] = (df['forecast_overall'].isin(['go','no_go'])) & (df['actual_verdict'].isin(['go','no_go'])) & (df['forecast_overall'] != df['actual_verdict'])
print(df.groupby('month').agg(n=('day','count'), correct=('correct','mean'), hard=('hard_error','mean')))
```

## What's next (links to the plan)

- **Threshold tuning (Phase 3 of the plan)**: `MIN_MORNING_SOLAR_WM2` is
  the first move. Working notes for that will land at
  `docs/findings/threshold-solar-radiation.md` when the data is
  re-fit and the rescore-strip before/after is captured.
- **ML classifier (GH issue #12)**: the `data/replay_*.csv` files
  are now ready as a training set. Was waiting for n≥50; we have
  n=3,263 (n=2,964 with full ICON-era feature coverage).
- **The 2021-2022 anomaly**: needs a deliberate "is this an extreme
  year?" input feature. Best addressed after the threshold pass
  settles the base rate.

## File-rotation policy

Findings files in this directory are working notes — raw data
exploration, not press releases. Once a post is published, the
published version lives on the blog platform and a short summary
stays in this directory pointing at it. Pre-publication findings
are intentionally detailed (the audience is future-us, not the
public).
