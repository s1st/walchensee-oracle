"""Triangulate a storm ground-truth label for the LI≤−2 flag from every signal.

Closes the loop the dashboard's forecast-only "storm" box left open (see
docs/findings/storm-ground-truth-spike-2026-06-24.md). No single source is a
clean thunderstorm label at Walchensee resolution, so we triangulate four
independent signals on the LI-predicted-storm days and report the false-alarm
rate they converge on.

Signals (best → weakest):
  1. Buoy gust + pressure jump  (Addicted-Sports Urfeld, lake-local, AUTHORITATIVE)
     A gust front = gust spike + a sharp MSL-pressure jump (dP). Catches fronts the
     rain/camera miss (e.g. 2021-06-29: gust 43kt, +7.2 hPa, yet 2mm rain, CLIP 0).
     Buoy rain gauge unusable (reads 0 everywhere) — wind+pressure only.
  2. DWD afternoon precipitation  (Bright Sky / Jachenau-Obernach 3.3 km)
     Convective wet proxy. Note DWD storms 3 km away often MISS the lake (buoy
     gust/rain << DWD on the same day) — DWD over-reads lake storms.
  3. CLIP zero-shot on webcam _hd frames  (visual; high precision, LOW recall —
     only fires on blatant rain-on-lens). Pretrained weather CNNs FAIL here
     (domain shift from automotive/close-up training data).
  4. Webcam frame density  (weak free aux: capture sometimes ramps 6→12/hr on big
     wind days, but noisy — confirmed on one gust-front day, not the other).

Reads the intermediates written by the upstream probes, in this order:
    python3 scripts/storm_ground_truth_spike.py      # -> /tmp/storm_days.json /tmp/obs.json
    python3 scripts/webcam_weather_classify.py        # -> /tmp/webcam_clip_labels.json  (vision venv)
    python3 /tmp/buoy_fetch.py                         # -> /tmp/buoy.json (buoy curves; see git log)
    python3 scripts/storm_label_multisignal.py
"""
from __future__ import annotations

import json


def _load():
    rows = json.load(open("/tmp/storm_days.json"))
    obs = json.load(open("/tmp/obs.json"))
    buoy = json.load(open("/tmp/buoy.json"))
    clip = {r["iso"]: r["clip_storm_prob"] for r in json.load(open("/tmp/webcam_clip_labels.json"))}
    pred = [r["iso"] for r in rows if r["pred_storm"]]
    return pred, obs, buoy, clip


def _is_storm(iso, obs, buoy, clip) -> bool:
    """Composite OR: any independent signal says a real storm hit the lake."""
    b = buoy.get(iso, {})
    gust_front = b.get("n") and b["max_gust"] >= 22 and (b.get("press_range") or 0) >= 2
    heavy_rain = obs.get(iso, {}).get("maxprecip", 0) >= 5      # DWD heavy convective rain
    visible = clip.get(iso, 0) >= 0.5                            # CLIP blatant rain
    return bool(gust_front or heavy_rain or visible)


def main() -> None:
    pred, obs, buoy, clip = _load()
    storm = [i for i in pred if _is_storm(i, obs, buoy, clip)]
    n = len(pred)
    print(f"LI≤−2 predicted-storm days: {n}")
    print(f"  real storm (any signal): {len(storm)}")
    print(f"  NOT a real storm:        {n - len(storm)}")
    print(f"  → false-alarm ratio = {(n - len(storm)) / n:.0%}")
    # buoy-only view (lake-local, where covered)
    bd = [i for i in pred if buoy.get(i, {}).get("n")]
    bstorm = [i for i in bd if buoy[i]["max_gust"] >= 22 and (buoy[i].get("press_range") or 0) >= 2]
    print(f"\n  buoy-covered days: {len(bd)} | lake gust-front signature: {len(bstorm)} "
          f"({(len(bd) - len(bstorm)) / len(bd):.0%} false alarm, lake-local)")
    print(f"  storm days: {sorted(storm)}")


if __name__ == "__main__":
    main()
