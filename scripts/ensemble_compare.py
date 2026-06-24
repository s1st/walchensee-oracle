"""Compare rule / rule+fix / logistic-ML / HGB / gradient-ensembles on the session
label. The ensemble switches on the synoptic gradient: use the learned model when
the gradient opposes the thermal (< SWITCH), else the rule.

rule+fix = faithful HARD-thermik variant: a HARD NO_GO always wins aggregation, so
forcing no_go when gradient <= -3 is exactly what 'make thermik bite at <=-3' does.

Stored data only.
"""
from __future__ import annotations
import glob, json, os
from oracle.ml_classifier import classify
from oracle.hgb_shadow import classify_hgb  # LIVE HGB (fixed mapping; stored block is swapped)
from oracle.calibration import actual_verdict_thermal  # official 11kt/6-sample, gated

REPLAY, RUNS = "data/runs/replay", "data/runs"
SEASON = {5, 6, 7, 8, 9}
MIN_YEAR = int(os.environ.get("MIN_YEAR", "2021"))
SWITCH = -1.0          # below this gradient, trust the learned model
HARD_AT = -3.0         # rule+fix: thermik becomes HARD NO_GO at/below this


def main():
    rows = []
    for path in sorted(glob.glob(f"{REPLAY}/*.json")):
        day = os.path.basename(path)[:-5]
        try:
            mo, yr = int(day[5:7]), int(day[:4])
        except ValueError:
            continue
        if mo not in SEASON or yr < MIN_YEAR:
            continue
        rep = json.load(open(path))
        g = rep.get("inputs", {}).get("pressure", {}).get("thermik_delta_hpa")
        if g is None:
            continue
        rule = rep.get("overall_resimulated") or rep.get("overall")
        p_in, m_in = rep.get("inputs", {}).get("pressure"), rep.get("inputs", {}).get("meteo")
        hgb_live = classify_hgb(p_in, m_in)         # recompute; never trust stored block
        ml = classify(p_in, m_in)
        if ml is None or hgb_live is None:
            continue
        hgb = hgb_live["verdict"]
        gt = f"{RUNS}/{day}.json"
        if not os.path.exists(gt):
            continue
        m = (json.load(open(gt)).get("ground_truth") or {}).get("machine")
        av = actual_verdict_thermal(m)
        if av is None:
            continue
        rule_fix = "no_go" if g <= HARD_AT else rule
        rows.append({
            "g": g, "session": av == "go",
            "rule": rule, "rule_fix": rule_fix,
            "ml": ml.verdict, "hgb": hgb,
            "ens_ml": ml.verdict if g < SWITCH else rule,
            "ens_hgb": hgb if g < SWITCH else rule,
            "ens_hgb_fix": hgb if g < SWITCH else rule_fix,
        })

    n = len(rows)
    base = sum(r["session"] for r in rows) / n
    print(f"n={n} (>= {MIN_YEAR}, in-season, thermal label = sustained 11kt+ gated); "
          f"base GO={base:.0%}\n")

    models = ["rule", "rule_fix", "ml", "hgb", "ens_ml", "ens_hgb", "ens_hgb_fix"]

    def peirce(sub, key):
        tp = sum(1 for r in sub if r[key] == "go" and r["session"])
        fp = sum(1 for r in sub if r[key] == "go" and not r["session"])
        fn = sum(1 for r in sub if r[key] != "go" and r["session"])
        tn = sum(1 for r in sub if r[key] != "go" and not r["session"])
        pod = tp / (tp + fn) if tp + fn else 0
        pofd = fp / (fp + tn) if fp + tn else 0
        acc = (tp + tn) / len(sub) if sub else 0
        return pod - pofd, acc

    print(f"{'model':<13} {'Peirce':>8} {'acc':>6}")
    for mdl in models:
        p, a = peirce(rows, mdl)
        print(f"{mdl:<13} {p:>+8.3f} {a:>6.2f}")

    print("\n-- Peirce by gradient regime --")
    bins = [(-99, -3, "oppose<=-3"), (-3, -1, "oppose-3..-1"),
            (-1, 0.5, "favor-1..0.5"), (0.5, 99, "favor>0.5")]
    hdr = "".join(f"{m[:9]:>11}" for m in models)
    print(f"{'regime':<14}{'n':>5} {hdr}")
    for lo, hi, lbl in bins:
        sub = [r for r in rows if lo <= r["g"] < hi]
        if not sub:
            continue
        cells = "".join(f"{peirce(sub, m)[0]:>+11.3f}" for m in models)
        print(f"{lbl:<14}{len(sub):>5} {cells}")


if __name__ == "__main__":
    main()
