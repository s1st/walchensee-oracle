"""Validate a 'heat period' feature against the session label, before we wire it
into either model. Hypothesis (windinfo lore): a long warm spell / warm nights
weaken the thermal even under otherwise great conditions.

Two forecast-time-available features, built from the stored buoy curves (2016+,
no CAPE dependency):
  F1 hot_day_streak  = consecutive PRIOR days with daily max air temp >= 28 C
  F2 morning_low_c   = today's lowest buoy air temp (warm-night proxy)

Outputs: session rate by feature bin, standalone Peirce skill, and -- the useful
one -- whether the feature flags the rule's FALSE GO days.
Stored data only, no API traffic.
"""
from __future__ import annotations
import glob, json, os
from datetime import date, timedelta
from oracle.calibration import actual_verdict_thermal  # official 11kt/6-sample, gated

RUNS = "data/runs"
REPLAY = "data/runs/replay"
SEASON = {5, 6, 7, 8, 9}
HOT_C = 28.0


def day_record(path):
    d = json.load(open(path))
    m = (d.get("ground_truth") or {}).get("machine")
    if not m:
        return None
    s = m.get("samples") or []
    airs = [x["air_temp_c"] for x in s if x.get("air_temp_c") is not None]
    if not airs:
        return None
    av = actual_verdict_thermal(m)
    if av is None:
        return None
    return {"max_air": max(airs), "min_air": min(airs),
            "session": av == "go", "peak_avg": m.get("peak_avg_knots")}


def main():
    # build the per-day map (all years, in-season)
    rec = {}
    for path in sorted(glob.glob(f"{RUNS}/*.json")):
        day = os.path.basename(path)[:-5]
        try:
            y, mo, dd = int(day[:4]), int(day[5:7]), int(day[8:10])
        except ValueError:
            continue
        if mo not in SEASON:
            continue
        r = day_record(path)
        if r:
            rec[date(y, mo, dd)] = r

    # F1 = consecutive preceding CALENDAR days with max_air >= HOT_C
    rows = []
    for dt, r in rec.items():
        streak = 0
        k = dt - timedelta(days=1)
        while k in rec and rec[k]["max_air"] >= HOT_C:
            streak += 1
            k -= timedelta(days=1)
        rows.append({"date": dt, "streak": streak, "min_air": r["min_air"],
                     "session": r["session"]})

    n = len(rows)
    base = sum(r["session"] for r in rows) / n
    print(f"n in-season days (2016+) = {n}; base session rate = {base:.0%}\n")

    def rate(sub):
        return (sum(x["session"] for x in sub) / len(sub), len(sub))

    print("== session rate by hot-day streak (prior days >=28C) ==")
    for lbl, lo, hi in [("0", 0, 1), ("1-2", 1, 3), ("3-4", 3, 5), ("5+", 5, 999)]:
        sub = [r for r in rows if lo <= r["streak"] < hi]
        if sub:
            sr, c = rate(sub)
            print(f"  streak {lbl:<4} n={c:<4} session={sr:.0%}")

    print("\n== session rate by today's min air temp (warm-night proxy) ==")
    for lbl, lo, hi in [("<12", -99, 12), ("12-15", 12, 15), ("15-18", 15, 18),
                        ("18+", 18, 99)]:
        sub = [r for r in rows if lo <= r["min_air"] < hi]
        if sub:
            sr, c = rate(sub)
            print(f"  min {lbl:<6} n={c:<4} session={sr:.0%}")

    def peirce(rows, pred):
        tp = sum(1 for r in rows if pred(r) and r["session"])
        fp = sum(1 for r in rows if pred(r) and not r["session"])
        fn = sum(1 for r in rows if not pred(r) and r["session"])
        tn = sum(1 for r in rows if not pred(r) and not r["session"])
        pod = tp / (tp + fn) if tp + fn else 0
        pofd = fp / (fp + tn) if fp + tn else 0
        return pod - pofd

    print("\n== standalone skill of a simple heat veto (predict NO session) ==")
    for thr in (3, 4, 5):
        p = peirce(rows, lambda r, t=thr: r["streak"] >= t)
        print(f"  'streak >= {thr} -> flaute' : Peirce = {p:+.3f}")
    for thr in (15, 16, 17):
        p = peirce(rows, lambda r, t=thr: r["min_air"] >= t)
        print(f"  'min_air >= {thr}C -> flaute': Peirce = {p:+.3f}")

    # the useful test: does the feature flag the RULE's false GO days?
    rule = {}
    for path in glob.glob(f"{REPLAY}/*.json"):
        day = os.path.basename(path)[:-5]
        try:
            y, mo, dd = int(day[:4]), int(day[5:7]), int(day[8:10])
        except ValueError:
            continue
        if mo not in SEASON:
            continue
        d = json.load(open(path))
        rule[date(y, mo, dd)] = d.get("overall_resimulated") or d.get("overall")

    go = [r for r in rows if rule.get(r["date"]) == "go"]
    print(f"\n== among RULE=GO days (n={len(go)}), does heat flag the false alarms? ==")
    sr_all, _ = rate(go)
    print(f"  overall session rate on GO days: {sr_all:.0%}")
    for lbl, lo, hi in [("streak 0-2", 0, 3), ("streak 3-4", 3, 5), ("streak 5+", 5, 999)]:
        sub = [r for r in go if lo <= r["streak"] < hi]
        if sub:
            sr, c = rate(sub)
            print(f"    {lbl:<11} n={c:<4} session={sr:.0%}")
    for lbl, lo, hi in [("min <15", -99, 15), ("min 15-18", 15, 18), ("min 18+", 18, 99)]:
        sub = [r for r in go if lo <= r["min_air"] < hi]
        if sub:
            sr, c = rate(sub)
            print(f"    {lbl:<11} n={c:<4} session={sr:.0%}")


if __name__ == "__main__":
    main()
