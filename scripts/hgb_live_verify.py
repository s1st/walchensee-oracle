"""Recompute the HGB live (fixed go/no_go mapping) vs the STORED replay block,
on the official thermal label. If the stored block was backfilled with the old
swapped map, stored P(go) is inverted (AUC<0.5) while live P(go) is correct.

Note: the bundle HGB is year-blocked (trained <=2022), so 2021-22 are in-sample;
the fair read is the >=2023 holdout. The shipped logistic (ml_classifier) trained
on the full replay, shown for context.
"""
from __future__ import annotations
import glob, json, os
from oracle.ml_classifier import classify
from oracle.hgb_shadow import classify_hgb
from oracle.calibration import actual_verdict_thermal

REPLAY, RUNS = "data/runs/replay", "data/runs"
SEASON = {5, 6, 7, 8, 9}


def auc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


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
    rows = []
    for path in sorted(glob.glob(f"{REPLAY}/*.json")):
        day = os.path.basename(path)[:-5]
        try:
            mo, yr = int(day[5:7]), int(day[:4])
        except ValueError:
            continue
        if mo not in SEASON or yr < 2021:
            continue
        rep = json.load(open(path))
        p = rep.get("inputs", {}).get("pressure")
        m_in = rep.get("inputs", {}).get("meteo")
        live = classify_hgb(p, m_in)
        ml = classify(p, m_in)
        stored = rep.get("hgb_classifier")
        if not live or not ml or not stored:
            continue
        gt = f"{RUNS}/{day}.json"
        if not os.path.exists(gt):
            continue
        av = actual_verdict_thermal((json.load(open(gt)).get("ground_truth") or {}).get("machine"))
        if av is None:
            continue
        rows.append({
            "yr": yr, "go": av == "go",
            "live_go": live["probabilities"]["go"],
            "live_verdict": live["verdict"],
            "stored_go": stored["probabilities"]["go"],
            "stored_nogo": stored["probabilities"]["no_go"],
            "ml_go": ml.probabilities["go"],
        })

    def report(sub, tag):
        n = len(sub)
        L = [r["go"] for r in sub]
        print(f"\n== {tag}  n={n}  GO base={sum(L)/n:.0%} ==")
        print(f"  {'score':<22}{'AUC':>7}{'bestPeirce':>12}")
        for name, key in [("HGB live P(go)", "live_go"),
                          ("HGB stored P(go)", "stored_go"),
                          ("HGB stored P(no_go)", "stored_nogo"),
                          ("ML logistic P(go)", "ml_go")]:
            sc = [r[key] for r in sub]
            print(f"  {name:<22}{auc(sc, L):>7.3f}{best_peirce(sc, L):>+12.3f}")
        # live HGB argmax skill
        tp = sum(1 for r in sub if r["live_verdict"] == "go" and r["go"])
        fp = sum(1 for r in sub if r["live_verdict"] == "go" and not r["go"])
        fn = sum(1 for r in sub if r["live_verdict"] != "go" and r["go"])
        tn = sum(1 for r in sub if r["live_verdict"] != "go" and not r["go"])
        pod = tp / (tp + fn) if tp + fn else 0
        pofd = fp / (fp + tn) if fp + tn else 0
        print(f"  HGB live argmax=='go' Peirce = {pod - pofd:+.3f}")

    report(rows, "ALL 2021+ (2021-22 IN-SAMPLE for HGB)")
    report([r for r in rows if r["yr"] >= 2023], "HOLDOUT 2023+ (fair for HGB)")


if __name__ == "__main__":
    main()
