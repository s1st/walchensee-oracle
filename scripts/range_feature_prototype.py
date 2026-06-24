"""Prototype: does the day-night temperature range earn its place as a model
feature? Train a binary GO-vs-rest logistic with the current 13 features vs.
13+range, year-holdout (train <=2022, test >=2023), official thermal label.

The range here is the OBSERVED DWD Jachenau range (daytime max - overnight min),
so this is the *value ceiling* — production would use the Open-Meteo tmax/tmin
forecast, which is noisier. If it doesn't help even observed, drop it; if it does,
the next step is a forecast-range retrain.

Run with .venv/bin/python (needs sklearn).
"""
from __future__ import annotations
import glob, json, os
from collections import defaultdict

import numpy as np
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

from oracle.ml_classifier import ML_MODEL, _feature_value
from oracle.calibration import actual_verdict_thermal

FEATURES = ML_MODEL["features"]
SEASON = {5, 6, 7, 8, 9}
CACHE = "/tmp/jachenau_ll"


def dwd_range_by_day() -> dict[str, float]:
    perday: dict[str, dict[int, float]] = defaultdict(dict)
    for y in range(2016, 2026):
        path = f"{CACHE}/{y}.json"
        if not os.path.exists(path):
            continue
        for h in json.load(open(path))["weather"]:
            ts, t = h.get("timestamp", ""), h.get("temperature")
            if ts and t is not None:
                perday[ts[:10]][int(ts[11:13])] = t
    out = {}
    for day, hrs in perday.items():
        night = [hrs[h] for h in range(0, 8) if h in hrs]
        dayh = [hrs[h] for h in range(11, 19) if h in hrs]
        if len(night) >= 4 and len(dayh) >= 4:
            out[day] = max(dayh) - min(night)
    return out


def auc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    return sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg) / (len(pos) * len(neg))


def best_peirce(scores, labels):
    best = -1.0
    for t in sorted(set(scores)):
        tp = sum(1 for s, y in zip(scores, labels) if s >= t and y)
        fp = sum(1 for s, y in zip(scores, labels) if s >= t and not y)
        fn = sum(1 for s, y in zip(scores, labels) if s < t and y)
        tn = sum(1 for s, y in zip(scores, labels) if s < t and not y)
        pod = tp / (tp + fn) if tp + fn else 0
        pofd = fp / (fp + tn) if fp + tn else 0
        best = max(best, pod - pofd)
    return best


def main():
    ranges = dwd_range_by_day()
    rows = []
    for path in sorted(glob.glob("data/runs/replay/*.json")):
        day = os.path.basename(path)[:-5]
        try:
            mo, yr = int(day[5:7]), int(day[:4])
        except ValueError:
            continue
        if mo not in SEASON or day not in ranges:
            continue
        rep = json.load(open(path))
        p, m = rep.get("inputs", {}).get("pressure"), rep.get("inputs", {}).get("meteo")
        if not p or not m:
            continue
        gt = f"data/runs/{day}.json"
        if not os.path.exists(gt):
            continue
        av = actual_verdict_thermal((json.load(open(gt)).get("ground_truth") or {}).get("machine"))
        if av is None:
            continue
        feats = [(_feature_value(n, p, m) if _feature_value(n, p, m) is not None else np.nan)
                 for n in FEATURES]
        rows.append((yr, feats, ranges[day], av == "go"))

    train = [r for r in rows if r[0] <= 2022]
    test = [r for r in rows if r[0] >= 2023]
    print(f"n train(<=2022)={len(train)}  test(>=2023)={len(test)}  "
          f"test GO base={sum(r[3] for r in test)/len(test):.0%}\n")

    yL_tr = [r[3] for r in train]
    yL_te = [r[3] for r in test]

    def fit_eval(with_range: bool, tag: str):
        Xtr = np.array([r[1] + ([r[2]] if with_range else []) for r in train])
        Xte = np.array([r[1] + ([r[2]] if with_range else []) for r in test])
        pipe = make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(class_weight="balanced", max_iter=2000),
        )
        pipe.fit(Xtr, yL_tr)
        sc = pipe.predict_proba(Xte)[:, 1].tolist()
        print(f"  {tag:<22} holdout AUC={auc(sc, yL_te):.3f}  best Peirce={best_peirce(sc, yL_te):+.3f}")

    print("binary GO-vs-rest logistic, train<=2022 / test>=2023:")
    fit_eval(False, "13 features (current)")
    fit_eval(True, "13 + day-night range")


if __name__ == "__main__":
    main()
