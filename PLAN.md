# Plan: split dashboard into landing + subpages

## Goal
One landing page (`/`) = verdict thesis (hero + webcam + live + day tabs).
Supporting content on subpages via a plain top link row (NOT a burger menu).

## Routes
- `/`           — hero + webcam + live + day tabs + ML card. (today's content minus strip/stats)
- `/history`    — 30-day strip (all 3 rows) + legend + original-verdict strip.
- `/stats`      — visitors + forecast quality + confusion + sensitivity/specificity.
- `/about`      — 14 rules explained (long-form of the `?` tooltips) + data sources.

## Templates
- `_base.html`  — `<head>`, top nav row (Home/History/Stats/About + lang toggle + GH), footer, `{% block content %}`.
- `index.html`  — inherits base; keeps hero/webcam/live/day-tabs/ML.
- `history.html`, `stats.html`, `about.html` — inherit base.

## main.py
- Extract `_base_context(request)` → {lang, t, nav, show_github, active}.
- `/` keeps existing logic but drops the strip + stats + advanced-panel.
- `/history`, `/stats`, `/about` new routes reusing existing helpers (_history, _forecast_stats, _fetch_page_views, _RULE_I18N).
- Day-switch stays server-rendered `<a>` navigation (drop the DOMParser/pushState JS — simpler, fixes focus-loss bug).

## Out of scope (this pass)
- Touch-target/contrast floor fixes (separate task, ui-craft skill).
- The `/about` long-form copy (scaffold the page + wire rules list; full prose later).

## Verify
`uvicorn oracle.dashboard.main:app --reload`, click all 4 routes + day tabs in Playwright.
