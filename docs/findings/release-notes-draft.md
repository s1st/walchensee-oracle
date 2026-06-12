# Release-notes draft — 2026-06-12

Brainstorming notes for a "release" / blog post series around the
historical-calibration work. The actual posts come later — this
file is the table of contents, the dependency graph, and the
ordering.

**Audience:** data-interested people (the FiveThirtyEight / Pudding
crowd). Not the windsurfing community specifically — they'll
encounter the dashboard and can read a short pointer there.

**Tone:** "Here's a question I had about some data I was already
collecting. Here's what I found. Here's the data so you can check."

**Format:** Probably 3-5 short posts (1500-2500 words each) over
a few weeks, released as a series. Not a single long-form essay —
the threads are different enough that grouping them forces a
"sponsored content" feel that doesn't suit the audience.

## Posts

### 1. "How we got 9 years of back-testable thermal data for a single Bavarian lake"

**Angle (engineering/data-pipeline):** The capability story. The
project has been writing one forecast per day to GCS since 2026-04-22.
That gave us 70 days of "what did the oracle say, what did the lake
do" pairs. Then we discovered the buoy archive on the Addicted-Sports
server goes back to 2016-06, the Open-Meteo Historical Forecast API
goes back to 2017-01, and the join was a one-day project. Net result:
3,300 day-curve samples + 3,300 forecast-as-issued pairs spanning
~7 years. Plus the design decision that made it usable: per-day
ground truth + per-day forecast verdict → re-scoring under new
thresholds with zero API calls.

**Material I have:** commit history of the replay feature, the
discovery that the buoy server has 10 years of data, the
batch-mode "two archive requests per year, not per day" trick.

**Dependency:** none — the standalone story of the pipeline.

### 2. "Where the model is wrong: a 9-year look at thermal forecasting accuracy"

**Angle (data-viz / exploratory):** The headline finding. 3,300
days of forecasts, scored against actual. 41% accuracy overall, but
the interesting stuff is the shape of the error — when, where,
in what direction.

**Material I have:** the seasonal pattern (Feb 30% vs May 50%),
the per-year anomaly (2021-2022 collectively account for 59% of
hard errors), the IFS-vs-ICON era split. Good chart fodder: a
calendar heatmap of correct/incorrect, a per-year accuracy line
chart, a confusion-matrix-per-month grid.

**Dependency:** post 1 (so the reader has the data-pipeline
context for what "replay" means). Can be post 1 in a different
form if needed.

### 3. "The years that broke the model: 2021-2022 in the data"

**Angle (single-finding deep-dive):** The 30-kt day the rule
vetoed on 481 W/m². The 65 hard errors in 2022 alone. The fact that
2 of 9 years account for 59% of all hard errors. The weather
context: 2021 was the rainiest summer in a decade, 2022 was the
sunniest summer in German history. Both broke the model but in
opposite directions. The interesting conclusion: the model
implicitly assumes "normal summer conditions" and the thermal
driver is more weather-regime-dependent than the current feature
set captures.

**Material I have:** the 2021-2022 hard-error breakdown, the
specific outlier days with the live data, the DWD summer reports
for both years. Good chart fodder: a bar chart of hard errors per
year with 2021-2022 highlighted, an annotated timeline showing
the weather context.

**Dependency:** post 2 (the per-year anomaly is a subset of
the per-year breakdown).

### 4. "IFS HRES vs DWD ICON: a 9-year thermal forecast bake-off"

**Angle (model comparison / methodology):** The era split. The
two forecast models have different error profiles. IFS is more
accurate overall (44.3% vs 40.7%) but has 4× the hard-error rate
(8.0% vs 3.1%). ICON is more conservative — when it commits to
"go", it's more often right, but it hedges with more "maybe"s.
The 22.2% November-ICON accuracy is a particularly interesting
signal: late-autumn ICON calls tend to be wrong in a specific
direction (calling "go" when the lake doesn't fire).

**Material I have:** the per-era confusion matrices, the per-era
per-month breakdown, the "most outrageous miss" tables. Chart
fodder: side-by-side confusion matrices for the two eras, a
per-month line chart with both eras overlaid, the November-ICON
outlier list.

**Dependency:** post 2 (the era structure only makes sense after
the per-year breakdown is in place).

### 5. "The threshold-tuning loop, run 1" (retrospective, after Phase 3)

**Angle (process retrospective):** The discipline of one threshold
per commit. The offender-list evidence. The rescore-strip in the
dashboard isolating the effect. What we changed, what the data
said, what we'd do differently. Not really a "release" post —
more of a "we did this and learned X" writeup for our future
selves and anyone who'll maintain the rules in 2027.

**Material I have:** nothing yet — depends on the threshold
tuning in Phase 3 of the plan.

**Dependency:** Phase 3 of `docs/replay-calibration-plan.md`
needs to complete. Estimated 1-2 hours of work for the first
threshold (`MIN_MORNING_SOLAR_WM2`); full pass of the 5 research-
analogue constants is probably a day of work over a few days.

## Sequencing

Posts 1-4 form the release series. Post 5 comes later, as a
"by the way, here's what we did with the data" follow-up.

Suggested release cadence: one post per week, starting with the
"how we got the data" (sets up the rest), then 2-3-4 in
roughly that order. Skip weeks when the data demands a deeper
follow-up.

## Cross-posting

The windsurfing community gets a short pointer in the dashboard
itself — the existing 30-day strip already tells them the
forecast accuracy. A link in the footer of the dashboard page
to the "Where the model is wrong" post is enough; the deeper
posts (1, 3, 4) are for the data crowd and the
`docs/findings/` working notes.

## Open questions

- Where do these posts live? Self-hosted blog (Hugo on Cloud Run
  would be consistent with the rest of the project)? Substack?
  The Pudding-style single-article-per-page site? The
  infrastructure choice affects the format of any embedded
  charts (SVG vs interactive).
- Do we chart anything? The findings have a lot of
  "would-be-nice-as-a-bar-chart" content. Worth doing? Cheap if
  we use a single SVG approach and inline it in the post.
- Do we publish the CSVs? The `data/replay_*.csv` files are
  in the project's gitignored `data/` directory. Worth pushing
  them somewhere public (GCS, GitHub gist, or a proper data
  release) so readers can verify the findings.
