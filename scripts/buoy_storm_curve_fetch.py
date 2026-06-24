import asyncio, json, datetime as dt
from oracle.pillars.measurements import fetch_urfeld_day_curve
rows=[r for r in json.load(open("/tmp/storm_days.json")) if r["pred_storm"]]
sem=asyncio.Semaphore(3)
async def one(r):
    iso=r["iso"]
    async with sem:
        try:
            s=await fetch_urfeld_day_curve(dt.date.fromisoformat(iso))
        except Exception as e:
            return iso, {"err": repr(e)[:80]}
    aft=[x for x in s if 11<=x.measured_at.hour<=21]
    if not aft: return iso, {"n":0}
    g=[x.gust_knots for x in aft]; a=[x.avg_knots for x in aft]
    pr=[x.pressure_hpa for x in aft if x.pressure_hpa is not None]
    rn=[x.rain_mm for x in aft if x.rain_mm is not None]
    return iso, {"n":len(aft),"max_gust":round(max(g),1),"max_avg":round(max(a),1),
                 "gustiness":round(max(g)/max(a),2) if max(a)>0.5 else None,
                 "press_range":round(max(pr)-min(pr),1) if len(pr)>1 else None,
                 "rain_max":round(max(rn),2) if rn else None}
async def main():
    res=dict(await asyncio.gather(*[one(r) for r in rows]))
    json.dump(res, open("/tmp/buoy.json","w"))
    ok=[v for v in res.values() if v.get("n")]
    print(f"buoy fetched: {len(ok)}/{len(rows)} days with afternoon samples")
asyncio.run(main())
