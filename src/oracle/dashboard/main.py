"""FastAPI dashboard for the Walchi Oracle.

Reads per-day records from the same store the scheduled job writes
(local disk in dev, GCS in production via $RUNS_BUCKET). Shows:

- Today's verdict + rule breakdown
- Recent Walchensee chat snippets
- 30-day strip of forecast vs. actual peak wind
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from oracle.logger import default_store

# @handle tags embedded in message bodies also identify authors — strip those
# so the public HTML never ships a windinfo.eu username. Match Unicode word
# chars so German-style names with umlauts stay intact in the final text.
_HANDLE_RE = re.compile(r"@[\w\-]+", re.UNICODE)

# Simple keyword lists for community-sentiment derivation. Hand-curated from
# reading a week of Walchensee chat — not a general German-sentiment model.
_POS_KW = (
    "läuft", "geht", "bläst", "weht", "thermik", "legt los", "kabbelwasser",
    "nordwind", "brise", "session", "gut", "solide", "top", "passt",
)
_NEG_KW = (
    "tot", "flau", "flaute", "nix los", "nichts", "nicht gelohnt",
    "kein wind", "lohnt nicht", "abgeraten", "plan b", "pennt", "pennen",
    "kommt nicht", "bleibt aus", "absagen",
)

app = FastAPI(title="Walchi Oracle")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Simple 60s TTL cache so a page load hits GCS at most once per minute per day.
_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[dict | None, float]] = {}


def _cached_read(iso_day: str) -> dict | None:
    value, expires_at = _cache.get(iso_day, (None, 0.0))
    if time.time() < expires_at and iso_day in _cache:
        return value
    store = default_store()
    fresh = store.read(iso_day)
    _cache[iso_day] = (fresh, time.time() + _CACHE_TTL_S)
    return fresh


def _most_recent(today: date) -> dict | None:
    """Find the latest available record within the past week."""
    for i in range(8):
        record = _cached_read((today - timedelta(days=i)).isoformat())
        if record is not None:
            return record
    return None


def _history(today: date, days: int = 30) -> list[dict]:
    """30-day strip: one entry per day, oldest first."""
    items: list[dict] = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        record = _cached_read(d.isoformat())
        peak = None
        if record:
            machine = (record.get("ground_truth") or {}).get("machine") or {}
            peak = machine.get("peak_avg_knots")
        items.append({
            "iso": d.isoformat(),
            "day": d.strftime("%a %d.%m"),
            "verdict": record.get("overall") if record else None,
            "peak_avg_knots": peak,
        })
    return items


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _summary_line(record: dict) -> str:
    """One-liner reason shown under the verdict headline."""
    overall = record.get("overall")
    verdicts = record.get("verdicts", [])
    if overall == "no_go":
        blocker = next((v for v in verdicts if v["signal"] == "no_go"), None)
        return blocker["reason"] if blocker else "—"
    if overall == "go":
        go_count = sum(1 for v in verdicts if v["signal"] == "go")
        return f"{go_count} von {len(verdicts)} Regeln grün."
    maybe = next((v for v in verdicts if v["signal"] == "maybe"), None)
    return maybe["reason"] if maybe else "Gemischte Signale."


def _chat_sentiment(record: dict) -> dict:
    """Derive a go/no_go/quiet/mixed signal from the raw chat messages."""
    messages = record.get("chat_messages") or []
    pos = neg = 0
    for m in messages:
        text = (m.get("text") or "").lower()
        if any(kw in text for kw in _POS_KW):
            pos += 1
        if any(kw in text for kw in _NEG_KW):
            neg += 1

    if pos == 0 and neg == 0:
        return {"code": "quiet", "label": "ruhig", "arrow": "·", "count": len(messages)}
    if pos >= neg * 1.5 and pos > 0:
        return {"code": "positive", "label": "positiv", "arrow": "↑", "count": len(messages)}
    if neg >= pos * 1.5 and neg > 0:
        return {"code": "negative", "label": "skeptisch", "arrow": "↓", "count": len(messages)}
    return {"code": "mixed", "label": "gemischt", "arrow": "↕", "count": len(messages)}


def _public_view(record: dict | None) -> dict | None:
    """Strip personal data (chat authors, channel names) before rendering.

    Raw logs in GCS keep the full fields for calibration — only this
    projection is what ends up in HTML served at the public custom domain.
    """
    if record is None:
        return None
    projection = dict(record)
    projection["chat_messages"] = [
        {
            "posted_at": m.get("posted_at"),
            "text": _HANDLE_RE.sub("@…", m.get("text") or ""),
        }
        for m in record.get("chat_messages", [])
    ]
    return projection


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    today = date.today()
    raw = _most_recent(today)
    summary = _summary_line(raw) if raw else ""
    sentiment = _chat_sentiment(raw) if raw else None
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "current": _public_view(raw),
            "summary": summary,
            "sentiment": sentiment,
            "history": _history(today),
            "today_iso": today.isoformat(),
        },
    )
