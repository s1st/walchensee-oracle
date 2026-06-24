"""Fair HGB vs logistic comparison on the SAME session label.

The earlier compare used argmax=='go', which punishes a conservatively-calibrated
model and judges HGB on a label it wasn't trained for. Here we judge by signal,
threshold-free:
  - AUC of each model's probabilistic "wind score" vs the session label
    (score = P(go), and an ordinal score P(go)+0.5*P(maybe))
  - best-threshold Peirce: pick each model's GO cutoff to maximise Peirce on the
    session label (the 'operating point is a product knob' principle)

Rule has no probability -> shown as a 3-level ordinal (go=1/maybe=.5/no_go=0) for AUC.
Stored data only.
"""
from __future__ import annotations
import glob, json, os
from oracle.ml_classifier import classify
from oracle.calibration import actual_verdict_thermal  # official 11kt/6-sample, gated

REPLAY, RUNS = "data/runs/replay", "data/runs"
SEASON = {5, 6, 7, 8, 9}
MIN_YEAR = int(os.environ.get("MIN_YEAR", "2021"))


def auc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    # Mann-Whitney with 0.5 for ties
    wins = 0.0
    for p in pos:
        for q in neg:
            wins += 1.0 if p > q else 0.5 if p == q else 0.0
    return wins / (len(pos) * len(neg))


def best_peirce(scores, labels):
    best, best_t = -1.0, None
    for t in sorted(set(scores)):
        tp = sum(1 for s, y in zip(scores, labels) if s >= t and y)
        fp = sum(1 for s, y in zip(scores, labels) if s >= t and not y)
        fn = sum(1 for s, y in zip(scores, labels) if s < t and y)
        tn = sum(1 for s, y in zip(scores, labels) if s < t and not y)
        pod = tp / (tp + fn) if tp + fn else 0
        pofd = fp / (fp + tn) if fp + tn else 0
        if pod - pofd > best:
            best, best_t = pod - pofd, t
    return best, best_t


def main():
    L, hgb_go, hgb_ord, ml_go, ml_ord, rule_ord = [], [], [], [], [], []
    ord3 = {"go": 1.0, "maybe": 0.5, "no_go": 0.0}
    for path in sorted(glob.glob(f"{REPLAY}/*.json")):
        day = os.path.basename(path)[:-5]
        try:
            mo, yr = int(day[5:7]), int(day[:4])
        except ValueError:
            continue
        if mo not in SEASON or yr < MIN_YEAR:
            continue
        rep = json.load(open(path))
        h = rep.get("hgb_classifier") or {}
        hp = h.get("probabilities")
        if not hp:
            continue
        ml = classify(rep.get("inputs", {}).get("pressure"),
                      rep.get("inputs", {}).get("meteo"))
        if ml is None:
            continue
        gt = f"{RUNS}/{day}.json"
        if not os.path.exists(gt):
            continue
        m = (json.load(open(gt)).get("ground_truth") or {}).get("machine")
        av = actual_verdict_thermal(m)
        if av is None:
            continue
        L.append(av == "go")
        hgb_go.append(hp["go"])
        hgb_ord.append(hp["go"] + 0.5 * hp["maybe"])
        mp = ml.probabilities
        ml_go.append(mp["go"])
        ml_ord.append(mp["go"] + 0.5 * mp["maybe"])
        rule_ord.append(ord3[rep.get("overall_resimulated") or rep.get("overall")])

    n = len(L)
    print(f"n={n} (>= {MIN_YEAR}, in-season, storms excluded); session base={sum(L)/n:.0%}\n")
    print(f"{'model / score':<22} {'AUC':>7} {'bestPeirce':>11} {'@thr':>7}")
    for name, sc in [("HGB  P(go)", hgb_go), ("HGB  P(go)+.5P(maybe)", hgb_ord),
                     ("ML   P(go)", ml_go), ("ML   P(go)+.5P(maybe)", ml_ord),
                     ("rule ordinal", rule_ord)]:
        a = auc(sc, L)
        bp, bt = best_peirce(sc, L)
        print(f"{name:<22} {a:>7.3f} {bp:>+11.3f} {bt:>7.3f}")


if __name__ == "__main__":
    main()
