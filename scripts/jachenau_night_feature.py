"""Re-test the warm-night hypothesis with a TRUE overnight minimum from DWD
Jachenau-Obernach (station 02660, ~1.8 km from the lake, hourly incl. night),
instead of the daytime-only buoy proxy.

Pulls Bright Sky per season-year (cached to /tmp), builds:
  tmin_night = min temp 00:00-07:00 local  (real overnight low)
  tmax_day   = max temp 11:00-18:00 local
  range_true = tmax_day - tmin_night
joins the buoy session label, and reruns the cut.
"""
from __future__ import annotations
import json, os, urllib.request
from collections import defaultdict
from datetime import date
from oracle.calibration import actual_verdict_thermal  # official 11kt/6-sample, gated

STATION = "02660"  # Jachenau-Obernach (precip-only; lat/lon merge supplies temp)
LAT, LON = 47.58, 11.32  # Walchensee / Jachenau
CACHE = "/tmp/jachenau_ll"
RUNS = "data/runs"
os.makedirs(CACHE, exist_ok=True)


def fetch_year(y: int) -> list[dict]:
    cf = f"{CACHE}/{y}.json"
    if os.path.exists(cf):
        return json.load(open(cf))["weather"]
    url = (f"https://api.brightsky.dev/weather?lat={LAT}&lon={LON}"
           f"&date={y}-05-01&last_date={y}-09-30&tz=Europe/Berlin")
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    json.dump(data, open(cf, "w"))
    return data["weather"]


def session_label(day: str) -> bool | None:
    p = f"{RUNS}/{day}.json"
    if not os.path.exists(p):
        return None
    m = (json.load(open(p)).get("ground_truth") or {}).get("machine")
    av = actual_verdict_thermal(m)
    return None if av is None else av == "go"


def main():
    perday = defaultdict(dict)  # day -> {hour: temp}
    for y in range(2016, 2026):
        try:
            w = fetch_year(y)
        except Exception as e:
            print(f"  {y}: fetch failed ({e})")
            continue
        for h in w:
            ts = h.get("timestamp", "")
            t = h.get("temperature")
            if not ts or t is None:
                continue
            day, hh = ts[:10], int(ts[11:13])
            perday[day][hh] = t

    rows = []
    for day, hrs in perday.items():
        night = [hrs[h] for h in range(0, 8) if h in hrs]
        dayh = [hrs[h] for h in range(11, 19) if h in hrs]
        if len(night) < 4 or len(dayh) < 4:
            continue
        sess = session_label(day)
        if sess is None:
            continue
        tmin = min(night)
        tmax = max(dayh)
        rows.append({"day": day, "tmin_night": tmin, "tmax_day": tmax,
                     "range": tmax - tmin, "session": sess})

    n = len(rows)
    if not n:
        print("no joined days"); return
    base = sum(r["session"] for r in rows) / n
    print(f"n joined days (DWD night + buoy session) = {n}; base session = {base:.0%}\n")

    def rate(sub):
        return (sum(x["session"] for x in sub) / len(sub), len(sub)) if sub else (0, 0)

    print("== session rate by TRUE overnight min (00-07h, Jachenau) ==")
    for lbl, lo, hi in [("<8", -99, 8), ("8-11", 8, 11), ("11-14", 11, 14),
                        ("14-17", 14, 17), ("17+", 17, 99)]:
        sr, c = rate([r for r in rows if lo <= r["tmin_night"] < hi])
        if c: print(f"  night {lbl:<6} n={c:<4} session={sr:.0%}")

    print("\n== session rate by TRUE day-night range ==")
    for lbl, lo, hi in [("<8", -99, 8), ("8-12", 8, 12), ("12-16", 12, 16),
                        ("16-20", 16, 20), ("20+", 20, 99)]:
        sr, c = rate([r for r in rows if lo <= r["range"] < hi])
        if c: print(f"  range {lbl:<6} n={c:<4} session={sr:.0%}")

    def peirce(rows, pred):
        tp = sum(1 for r in rows if pred(r) and r["session"])
        fp = sum(1 for r in rows if pred(r) and not r["session"])
        fn = sum(1 for r in rows if not pred(r) and r["session"])
        tn = sum(1 for r in rows if not pred(r) and not r["session"])
        pod = tp / (tp + fn) if tp + fn else 0
        pofd = fp / (fp + tn) if fp + tn else 0
        return pod - pofd

    print("\n== standalone skill: warm night -> predict flaute ==")
    for thr in (14, 15, 16, 17):
        print(f"  night >= {thr}C -> flaute : Peirce = {peirce(rows, lambda r,t=thr: r['tmin_night']>=t):+.3f}")
    print("== standalone skill: small range -> predict flaute ==")
    for thr in (8, 10, 12):
        print(f"  range < {thr}C  -> flaute : Peirce = {peirce(rows, lambda r,t=thr: r['range']<t):+.3f}")


if __name__ == "__main__":
    main()
