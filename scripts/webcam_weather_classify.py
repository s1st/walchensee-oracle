"""CLIP zero-shot storm label from Urfeld webcam frames → score the LI flag.

Why CLIP and not a pretrained weather CNN: on this wide-angle alpine-lake domain
the popular HF weather classifiers (prithivMLmods/Weather-Image-Classification,
dima806/weather_types_image_detection) don't separate storm from dry-overcast
(rain prob ~0.3 vs ~0.3, ~0.71 vs ~0.73) — domain shift from their
automotive/close-up training data; they'd need fine-tuning on lake-cam frames we
don't have labels for. CLIP zero-shot (ViT-B-32, laion2b) with lens-drop-aware
prompts on `_hd` frames separates cleanly (storm 0.95 vs dry 0.01 on the two
hand-validated days 2022-06-24 / 2021-06-20).

Runs in the throwaway py3.12 vision venv (torch 2.2.2 + open_clip), NOT the prod
env. Reads the day list from /tmp/storm_days.json (written by
storm_ground_truth_spike.py). Fetches `_hd` afternoon frames, takes the max
storm-probability over the afternoon (storms are transient), thresholds, and
prints the LI≤−2 vs observed-storm contingency + a DWD-precip cross-check.

    python3 scripts/webcam_weather_classify.py
"""
from __future__ import annotations

import io
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import open_clip
import torch
from PIL import Image

_UA = {"User-Agent": "walchi-oracle/0.1 (hobby; storm-ground-truth; addicted-sports partnership)"}
_BASE = "https://www.addicted-sports.com/fileadmin/webcam/walchensee"
_HOURS = (12, 14, 16, 18)          # afternoon convection window, _hd for lens-drop cue
_STORM_THRESH = 0.5
_N_CONTROL = 100
_SEED = 24

_PROMPTS = {
    "clear": "a clear sunny day with blue sky over a lake",
    "cloudy": "a dry overcast grey day over a lake, mountains visible",
    "rain": "heavy rain over a lake with water drops on the camera lens",
    "thunderstorm": "a dark thunderstorm with heavy clouds over a lake",
    "fog": "fog over a lake",
}
_STORM_CLASSES = ("rain", "thunderstorm")


def _day_sets() -> list[dict]:
    rows = json.load(open("/tmp/storm_days.json"))
    storm = [r for r in rows if r["pred_storm"]]
    nonstorm = [r for r in rows if not r["pred_storm"]]
    random.Random(_SEED).shuffle(nonstorm)
    return storm + nonstorm[:_N_CONTROL]


def _fetch_frames(iso: str) -> list[Image.Image]:
    y, m, d = iso.split("-")
    out = []
    with httpx.Client(timeout=30, headers=_UA) as c:
        for hh in _HOURS:
            try:
                r = c.get(f"{_BASE}/{y}/{m}/{d}/{hh:02d}00_hd.jpg")
            except Exception:
                continue
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                try:
                    out.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
                except Exception:
                    pass
            time.sleep(0.1)
    return out


def main() -> None:
    days = _day_sets()
    print(f"CLIP storm-labelling {len(days)} days "
          f"({sum(d['pred_storm'] for d in days)} predicted-storm + control)…")

    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="laion2b_s34b_b79k")
    model.eval()
    tok = open_clip.get_tokenizer("ViT-B-32")
    keys = list(_PROMPTS)
    with torch.no_grad():
        tf = model.encode_text(tok([_PROMPTS[k] for k in keys]))
        tf /= tf.norm(dim=-1, keepdim=True)
    storm_idx = [keys.index(k) for k in _STORM_CLASSES]

    # Fetch frames concurrently, classify sequentially (CPU).
    with ThreadPoolExecutor(max_workers=4) as ex:
        frames_by_day = list(ex.map(lambda r: (r, _fetch_frames(r["iso"])), days))

    labels = []
    for i, (row, frames) in enumerate(frames_by_day, 1):
        max_storm = 0.0
        for img in frames:
            with torch.no_grad():
                e = model.encode_image(preprocess(img).unsqueeze(0))
                e /= e.norm(dim=-1, keepdim=True)
                p = (100 * e @ tf.T).softmax(-1)[0]
            max_storm = max(max_storm, float(sum(p[j] for j in storm_idx)))
        labels.append({**row, "n_frames": len(frames),
                       "clip_storm_prob": round(max_storm, 3),
                       "obs_storm": max_storm >= _STORM_THRESH})
        if i % 20 == 0:
            print(f"  {i}/{len(days)}")
    json.dump(labels, open("/tmp/webcam_clip_labels.json", "w"))

    scored = [r for r in labels if r["n_frames"]]
    tp = sum(r["pred_storm"] and r["obs_storm"] for r in scored)
    fp = sum(r["pred_storm"] and not r["obs_storm"] for r in scored)
    fn = sum((not r["pred_storm"]) and r["obs_storm"] for r in scored)
    tn = sum((not r["pred_storm"]) and not r["obs_storm"] for r in scored)
    print(f"\n=== LI≤−2 (predicted) vs CLIP-observed storm (n={len(scored)}) ===")
    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    if tp + fp:
        print(f"  false-alarm ratio = {fp/(tp+fp):.0%} of predicted storms had no visible storm")
    if tp + fn:
        print(f"  hit rate (of CLIP-observed storms predicted) = {tp/(tp+fn):.0%}")

    # Cross-check CLIP vs DWD precip on predicted-storm days (sanity, no hand labels).
    obs = json.load(open("/tmp/obs.json"))
    ps = [r for r in scored if r["pred_storm"]]
    agree_wet = sum(r["obs_storm"] and obs.get(r["iso"], {}).get("maxprecip", 0) >= 1 for r in ps)
    agree_dry = sum((not r["obs_storm"]) and obs.get(r["iso"], {}).get("maxprecip", 0) == 0 for r in ps)
    print(f"\nCLIP×DWD agreement on {len(ps)} predicted-storm days: "
          f"both-storm(≥1mm)={agree_wet}  both-clear(0mm)={agree_dry}")


if __name__ == "__main__":
    main()
