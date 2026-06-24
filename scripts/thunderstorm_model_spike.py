"""First-pass calibrated thunderstorm advisory — beats the LI≤−2 flag ~2× on skill.

Per docs/findings/thunderstorm-forecast-design-2026-06-24.md. Builds the dataset
entirely from data we already have plus a free Open-Meteo fetch:

  LABEL  : stored buoy curve in data/runs/<iso>.json `ground_truth.machine.samples`
           (gust_kt + pressure_hpa, all years — NO partner re-fetch). A gust-front
           = afternoon max_gust ≥ 22 kt AND MSL pressure_range ≥ 2 hPa.
  FEATURES: afternoon (12–18 local) CAPE / lifted_index / CIN / precip /
           precip_probability / deep shear (700hPa−10m) / low cloud, from the
           Open-Meteo historical-forecast API (free; CAPE/LI only exist 2021→2025).

Dataset: 2021–2025 in-season, ~1067 days, 89 gust-front storms (8.3% base rate).
Leave-one-year-out result:
  LI≤−2 (current flag)   POD 44%  FAR 87%  Peirce 0.178
  logistic (7 features)  POD 63%  FAR 78%  Peirce 0.429   (≈2× skill)
  logistic @matched POD  POD 44%  FAR 75%               (same recall, fewer false alarms)

Storms are rare and hard, so FAR stays high even for the good model — but the
Peirce doubling is the real skill gain, and the advisory can pick its own
recall/FAR operating point (it's a warning, not a veto).

    uv pip install scikit-learn         # [ml] extra; not in either Dockerfile
    python3 scripts/thunderstorm_model_spike.py
"""
from __future__ import annotations

import calendar
import glob
import json
import os
from datetime import datetime

import httpx
import numpy as np
from sklearn.linear_model import LogisticRegression

from oracle.config import URFELD, OPEN_METEO_HISTORICAL_FORECAST_URL as HF

_AFT = range(12, 19)
_FEATS = ["cape", "lifted_index", "convective_inhibition", "precipitation",
          "precipitation_probability", "wind_speed_700hPa", "wind_speed_10m", "cloud_cover_low"]
_FK = ["cape_max", "li_min", "cin_min", "precip_sum", "precip_prob_max", "shear", "low_cloud_max"]
_CACHE = "/tmp/feats_2125.json"


def buoy_label(iso: str) -> dict | None:
    """Afternoon gust-front signal from the stored buoy curve (no fetch)."""
    f = f"data/runs/{iso}.json"
    if not os.path.exists(f):
        return None
    s = (json.load(open(f)).get("ground_truth") or {}).get("machine", {}).get("samples") or []
    aft = [r for r in s if 12 <= datetime.fromisoformat(r["t"]).hour <= 18]
    g = [r["gust_kt"] for r in aft if r.get("gust_kt") is not None]
    p = [r["pressure_hpa"] for r in aft if r.get("pressure_hpa") is not None]
    if len(g) < 3:
        return None
    return {"max_gust": max(g), "press_range": (max(p) - min(p)) if len(p) > 1 else 0.0}


def fetch_features() -> dict:
    if os.path.exists(_CACHE):
        return json.load(open(_CACHE))
    yms = [f"{y}-{m:02d}" for y in range(2021, 2026) for m in range(4, 11)]
    feats: dict = {}
    with httpx.Client(timeout=60, headers={"User-Agent": "walchi-oracle/0.1 (hobby)"}) as c:
        for ym in yms:
            y, mo = (int(x) for x in ym.split("-"))
            last = calendar.monthrange(y, mo)[1]
            h = c.get(HF, params={"latitude": URFELD.lat, "longitude": URFELD.lon,
                      "hourly": ",".join(_FEATS), "wind_speed_unit": "kn", "timezone": "Europe/Berlin",
                      "start_date": f"{ym}-01", "end_date": f"{ym}-{last:02d}"}).json().get("hourly", {})
            T = h.get("time", [])
            byd: dict = {}
            for i, t in enumerate(T):
                if int(t[11:13]) not in _AFT:
                    continue
                d = byd.setdefault(t[:10], {v: [] for v in _FEATS})
                for v in _FEATS:
                    val = h.get(v, [None] * len(T))[i]
                    if val is not None:
                        d[v].append(val)
            for day, dd in byd.items():
                mx = lambda k: max(dd[k]) if dd[k] else None
                mn = lambda k: min(dd[k]) if dd[k] else None
                feats[day] = {"cape_max": mx("cape"), "li_min": mn("lifted_index"),
                    "cin_min": mn("convective_inhibition"),
                    "precip_sum": round(sum(dd["precipitation"]), 1) if dd["precipitation"] else None,
                    "precip_prob_max": mx("precipitation_probability"),
                    "shear": (mx("wind_speed_700hPa") - mn("wind_speed_10m"))
                             if dd["wind_speed_700hPa"] and dd["wind_speed_10m"] else None,
                    "low_cloud_max": mx("cloud_cover_low")}
    json.dump(feats, open(_CACHE, "w"))
    return feats


def _metrics(pred, y):
    tp = (pred & y).sum(); fp = (pred & ~y).sum(); fn = (~pred & y).sum(); tn = (~pred & ~y).sum()
    pod = tp / (tp + fn); far = fp / (tp + fp) if tp + fp else 0.0
    return pod, far, pod - fp / (fp + tn)


def main() -> None:
    feats = fetch_features()
    days = [d for d in feats if buoy_label(d) and feats[d].get("cape_max") is not None]
    lab = {d: buoy_label(d) for d in days}
    y = np.array([lab[d]["max_gust"] >= 22 and lab[d]["press_range"] >= 2 for d in days])
    yr = np.array([int(d[:4]) for d in days])

    def col(k):
        v = np.array([feats[d][k] if feats[d][k] is not None else np.nan for d in days], float)
        v[np.isnan(v)] = np.nanmedian(v)
        return v

    X = np.column_stack([col(k) for k in _FK])
    print(f"dataset: {len(y)} days, {y.sum()} gust-front storms ({y.mean():.1%} base rate)\n")
    print(f"{'model':26} {'POD':>5} {'FAR':>5} {'Peirce':>7}")
    pod, far, pk = _metrics(col("li_min") <= -2, y)
    print(f"{'LI<=-2 (current flag)':26} {pod:5.0%} {far:5.0%} {pk:7.3f}")

    scores = np.zeros(len(y))
    for yy in np.unique(yr):
        tr, te = yr != yy, yr == yy
        m = LogisticRegression(class_weight="balanced", max_iter=1000).fit(X[tr], y[tr])
        scores[te] = m.predict_proba(X[te])[:, 1]
    best = max(np.unique(scores), key=lambda t: _metrics(scores >= t, y)[2])
    pod, far, pk = _metrics(scores >= best, y)
    print(f"{'logistic (7 feat) @best':26} {pod:5.0%} {far:5.0%} {pk:7.3f}")
    matched = min(np.unique(scores), key=lambda t: abs(_metrics(scores >= t, y)[0] - 0.44))
    pod, far, pk = _metrics(scores >= matched, y)
    print(f"{'logistic @matched POD':26} {pod:5.0%} {far:5.0%} {pk:7.3f}")


if __name__ == "__main__":
    main()
