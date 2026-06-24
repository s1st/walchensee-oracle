"""Spike: how good is the LI ≤ MIN_LIFTED_INDEX flag at predicting *actual* storms?

The dashboard's dashed "storm" box is forecast-driven — it fires when the logged
`inputs.meteo.min_lifted_index` ≤ −2 (predicted convective instability). We have
no thunderstorm ground truth in the pipeline (only Urfeld *wind* is observed), so
storm-prediction skill has never been measured. This spike pulls DWD station
observations via Bright Sky (the source we already use) for every in-season
replay day that carries a forecast LI, and crosses the predicted-storm flag with:

  1. DWD `condition == "thunderstorm"`  (present-weather, the ideal label)
  2. afternoon precipitation             (a cheap convective proxy)

Finding (2021–2022, 428 days, see docs/findings/storm-ground-truth-spike-2026-06-24.md):
`condition` never reports thunderstorm — not at the 3.3 km automatic station nor
at the manned Hohenpeißenberg observatory — so Bright Sky's `condition` is not a
usable storm label. The precip proxy shows the LI flag over-warns heavily (~43%
of predicted-storm days were bone-dry). A proper label needs DWD present-weather
(ww) codes pulled from CDC directly, or lightning data (Blitzortung / DWD).

Run:  python3 scripts/storm_ground_truth_spike.py
Needs network (api.brightsky.dev) and the replay records in data/runs/replay/.
"""
from __future__ import annotations

import calendar
import glob
import json
import os
import time
from collections import defaultdict

import httpx

from oracle.calibration import storm_suspected
from oracle.config import URFELD

_UA = {"User-Agent": "walchi-oracle/0.1 (hobby; storm-ground-truth-spike)"}
_AFTERNOON = range(11, 22)  # 11:00–21:00 local — when Alpine convection fires
_REPLAY_GLOB = "data/runs/replay/*.json"


def _in_season(iso: str) -> bool:
    return 4 <= int(iso[5:7]) <= 10


def load_predicted() -> list[dict]:
    """Every in-season replay day that carries a forecast LI, with the flag."""
    rows = []
    for path in sorted(glob.glob(_REPLAY_GLOB)):
        iso = os.path.basename(path)[:-5]
        if not _in_season(iso):
            continue
        rec = json.load(open(path))
        li = (rec.get("inputs") or {}).get("meteo", {}).get("min_lifted_index")
        if li is None:
            continue
        rows.append({"iso": iso, "li": float(li), "pred_storm": storm_suspected(rec)})
    return rows


def fetch_observations(year_months: set[str]) -> dict[str, dict]:
    """One Bright Sky call per year-month; afternoon condition + precip per day."""
    obs: dict[str, dict] = defaultdict(
        lambda: {"cond": set(), "max_precip": 0.0, "tot_precip": 0.0, "dist": None}
    )
    with httpx.Client(timeout=60, headers=_UA) as client:
        for ym in sorted(year_months):
            year, month = (int(x) for x in ym.split("-"))
            last = calendar.monthrange(year, month)[1]
            url = (
                f"https://api.brightsky.dev/weather?lat={URFELD.lat}&lon={URFELD.lon}"
                f"&date={ym}-01T00:00&last_date={ym}-{last:02d}T23:00&tz=Europe/Berlin"
            )
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
            sources = {s["id"]: s for s in payload.get("sources", [])}
            for w in payload.get("weather", []):
                hour = int(w["timestamp"][11:13])
                if hour not in _AFTERNOON:
                    continue
                day = obs[w["timestamp"][:10]]
                if w.get("condition"):
                    day["cond"].add(w["condition"])
                precip = w.get("precipitation") or 0.0
                day["max_precip"] = max(day["max_precip"], precip)
                day["tot_precip"] += precip
                src = sources.get(w.get("source_id"))
                if src and src.get("distance"):
                    day["dist"] = max(day["dist"] or 0, src["distance"])
            time.sleep(0.3)  # be gentle to the public API
    return obs


def main() -> None:
    rows = load_predicted()
    year_months = {r["iso"][:7] for r in rows}
    print(f"in-season days w/ LI: {len(rows)} | predicted-storm (LI≤−2): "
          f"{sum(r['pred_storm'] for r in rows)} | months: {len(year_months)}")
    obs = fetch_observations(year_months)

    # 2×2: predicted storm vs observed condition==thunderstorm
    tp = fp = fn = tn = 0
    for r in rows:
        o = obs.get(r["iso"], {})
        observed = "thunderstorm" in o.get("cond", set())
        tp += r["pred_storm"] and observed
        fp += r["pred_storm"] and not observed
        fn += (not r["pred_storm"]) and observed
        tn += (not r["pred_storm"]) and not observed
    print("\n=== LI≤−2 (predicted) vs DWD condition==thunderstorm (observed) ===")
    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    observed_total = tp + fn
    print(f"  observed-thunderstorm base rate: {observed_total}/{len(rows)}")
    if observed_total == 0:
        print("  → `condition` never reports thunderstorm here; not a usable label "
              "(station-capability / Bright Sky derivation gap).")

    # Precip proxy on the predicted-storm days
    storm_rows = [r for r in rows if r["pred_storm"]]
    print(f"\n=== afternoon precip on the {len(storm_rows)} predicted-storm days ===")
    for thr in (0.1, 1.0, 5.0):
        wet = sum(obs.get(r["iso"], {}).get("max_precip", 0.0) >= thr for r in storm_rows)
        print(f"  max hourly precip ≥ {thr:>4} mm: {wet}/{len(storm_rows)}")
    dry = sum(obs.get(r["iso"], {}).get("max_precip", 0.0) == 0 for r in storm_rows)
    print(f"  bone-dry (0 mm all afternoon): {dry}/{len(storm_rows)} "
          f"→ {dry / len(storm_rows):.0%} clear false alarms")


if __name__ == "__main__":
    main()
