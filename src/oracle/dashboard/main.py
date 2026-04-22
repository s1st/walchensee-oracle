"""FastAPI dashboard for the Walchi Oracle.

Reads per-day records from the same store the scheduled job writes
(local disk in dev, GCS in production via $RUNS_BUCKET). Shows:

- Today's verdict + rule breakdown
- Recent Walchensee chat snippets
- 30-day strip of forecast vs. actual peak wind
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from oracle.logger import default_store
from oracle.pillars.measurements import UrfeldSample, fetch_urfeld_day_curve

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

# German weekday names → isoweekday index (Monday=0 … Sunday=6).
_DE_WEEKDAYS = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonntag": 6,
}
_MORGEN_RE = re.compile(r"\bmorgen\b", re.IGNORECASE)
_UEBERMORGEN_RE = re.compile(r"\büber[- ]?morgen\b", re.IGNORECASE)
_HEUTE_RE = re.compile(r"\bheute\b", re.IGNORECASE)


def _infer_day_reference(message: dict) -> date | None:
    """If the message body clearly references one day, return that date.

    Resolution order: übermorgen → morgen → heute → next-upcoming weekday
    by name (from the posting date). Returns None when the message has no
    unambiguous day reference.
    """
    try:
        posted = datetime.fromisoformat(message["posted_at"]).date()
    except (KeyError, ValueError):
        return None
    text = message.get("text") or ""

    if _UEBERMORGEN_RE.search(text):
        return posted + timedelta(days=2)
    if _MORGEN_RE.search(text) and not _UEBERMORGEN_RE.search(text):
        return posted + timedelta(days=1)
    if _HEUTE_RE.search(text):
        return posted

    low = text.lower()
    for name, idx in _DE_WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            days_ahead = (idx - posted.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # next-occurrence semantics, never the posting day itself
            return posted + timedelta(days=days_ahead)

    return None


def _messages_for_day(
    messages: list[dict], target_day: date, today: date
) -> list[dict]:
    """Filter chat messages down to those talking about `target_day`.

    Messages without a clear day reference fall into "today" as the default —
    they're most likely general current-conditions chatter.
    """
    out: list[dict] = []
    for m in messages:
        ref = _infer_day_reference(m)
        if ref is None:
            if target_day == today:
                out.append(m)
        elif ref == target_day:
            out.append(m)
    return out

# Rule descriptions — one short sentence each, keyed by rule and language.
# Shown as `?` hover tooltips in the Advanced panel.
_RULE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "thermik": {
        "de": "Luftdruck-Differenz München − Innsbruck. Positiver Delta ≥ 2.5 hPa treibt die Nord-Süd-Thermik an.",
        "en": "Munich − Innsbruck pressure delta. ≥ 2.5 hPa drives the north-to-south thermal pump.",
    },
    "foehn_override": {
        "de": "Luftdruck-Differenz Bozen − Innsbruck. Positiver Delta ≥ 4 hPa signalisiert Föhn — zerstört die Thermik.",
        "en": "Bolzano − Innsbruck pressure delta. ≥ 4 hPa signals Föhn — kills the local thermal.",
    },
    "overnight_cooling": {
        "de": "Mittlere Bewölkung 22:00 Vortag bis 06:00. Über 30 % schwächt die nächtliche Abkühlung und damit den Tagesgang.",
        "en": "Mean cloud cover 22:00 previous day to 06:00. Above 30 % weakens radiative cooling and the diurnal delta.",
    },
    "solar_radiation": {
        "de": "Peak-Sonneneinstrahlung zwischen 09:00 und 13:00. Unter 600 W/m² reicht die Hangheizung nicht für eine saubere Thermik.",
        "en": "Peak shortwave radiation 09:00–13:00. Below 600 W/m² the slopes don't heat enough for a clean thermal.",
    },
    "dew_point_spread": {
        "de": "Kleinster (T − Td)-Abstand 09:00–13:00. Unter 5 °C geht Sonnenenergie in Verdunstung statt Lufterwärmung.",
        "en": "Smallest (T − Td) gap 09:00–13:00. Below 5 °C solar energy goes into evaporation, not sensible heating.",
    },
    "boundary_layer_height": {
        "de": "Grenzschicht-Höhe 09:00–13:00. Unter 600 m bleibt die Thermik gedeckelt; ab 1000 m tiefe Durchmischung.",
        "en": "Boundary-layer height 09:00–13:00. Below 600 m the thermal is capped; above 1000 m = deep mixing.",
    },
    "post_rain_moisture": {
        "de": "Nasser Boden (Regen gestern ≥ 2 mm oder Bodenfeuchte > 0.35 m³/m³) leitet Sonnenenergie in Verdunstung statt in Hangheizung.",
        "en": "Wet ground (≥ 2 mm rain yesterday or soil moisture > 0.35 m³/m³) diverts solar energy to evaporation.",
    },
    "atmospheric_stability": {
        "de": "Lifted Index 09:00–13:00. LI ≥ +6 = Atmosphäre zu stabil (Thermik gedeckelt); LI ≤ −2 = Gewittergefahr.",
        "en": "Lifted index 09:00–13:00. LI ≥ +6 = atmosphere too stable (capped); LI ≤ −2 = thunderstorm risk.",
    },
    "daytime_clouds": {
        "de": "Max. tiefe Bewölkung 09:00–13:00. Über 60 % beschattet Herzogstand/Jochberg-Hänge und stoppt die Hangheizung.",
        "en": "Max low-cloud cover 09:00–13:00. Above 60 % shades the Herzogstand/Jochberg slopes and stops slope heating.",
    },
    "upper_level_wind": {
        "de": "850 hPa Windrichtung am Morgen-Peak + 700 hPa Querströmung. SSE-Flow 150–210° widerspricht der N-Thermik; > 25 kt in 700 hPa entkoppelt das Tal.",
        "en": "850 hPa direction at the morning peak + 700 hPa crossflow. SSE 150–210° opposes the N-thermal; > 25 kt at 700 hPa decouples the valley.",
    },
    "synoptic_override": {
        "de": "Wind in 850 hPa (~1500 m) 09:00–13:00. Über 15 kt zerstört oder deformiert die lokale Thermikzelle.",
        "en": "Wind at 850 hPa (~1500 m) 09:00–13:00. Above 15 kt destroys or deforms the local thermal cell.",
    },
    "thermal_ignition": {
        "de": "Aktuelle Messwerte Urfeld-Anemometer + DWD-Station. Ab 8 kt Mittelwind gilt die Thermik als gezündet.",
        "en": "Live readings from the Urfeld buoy + nearest DWD station. ≥ 8 kt mean wind = thermal ignited.",
    },
}


# Static-UI translation dict. Rule REASON strings from the engine are not
# translated here — they're currently a mix of English and German and would
# need a proper refactor of the rules module to emit bilingual reasons.
_UI: dict[str, dict[str, str]] = {
    "de": {
        "strip_forecast": "Vorhersage",
        "strip_actual": "Realität (Urfeld-Peak)",
        "strip_legend_go": "Wind (≥ 12 kt)",
        "strip_legend_maybe": "marginal (8–12 kt)",
        "strip_legend_no_go": "kein Wind (< 8 kt)",
        "strip_legend_empty": "keine Daten",
        "live_header": "Aktuell an Urfeld",
        "live_now": "jetzt",
        "live_gust_label": "Böe",
        "live_last_hour": "Schnitt letzte Stunde",
        "live_trend_up": "steigend",
        "live_trend_down": "fallend",
        "live_trend_flat": "stabil",
        "live_unavailable": "Urfeld-Sensor gerade nicht erreichbar.",
        "chart_aria": "Urfeld-Wind, letzte 6 Stunden",
        "chart_legend_avg": "Mittelwind",
        "chart_legend_gust": "Böe",
        "chart_legend_ignition": "Zündung (8 kt)",
        "chart_legend_session": "Session (12 kt)",
        "webcam_label": "Webcam Urfeld",
        "lead": "Geht heute Thermik am Walchensee?",
        "verdict_go": "GEHT",
        "verdict_maybe": "GRENZWERTIG",
        "verdict_no_go": "NIX",
        "for_day": "Für",
        "community_prefix": "Community",
        "sentiment_positive": "positiv",
        "sentiment_negative": "skeptisch",
        "sentiment_mixed": "gemischt",
        "sentiment_quiet": "ruhig",
        "last_30_days": "Letzte 30 Tage",
        "no_data": "Keine Daten — warte auf den nächsten morgendlichen Forecast (08:00 MEZ).",
        "no_data_headline": "—",
        "advanced_label": "Advanced — alle 12 Regeln & Chat-Auszüge",
        "col_rule": "Regel",
        "col_signal": "Signal",
        "col_reason": "Begründung",
        "chat_header_advanced": "Chat-Auszüge (anonymisiert)",
        "chat_footer": "Anonymisiert. Quelle:",
        "windinfo_label": "windinfo.eu Community-Chat",
        "footer_outline": "Schwellwerte noch Schätzungen auf Basis von Lake-Garda-Analoga — Kalibrierung läuft.",
        "footer_urfeld": "Urfeld-Wind: © Panoramahotel Karwendelblick, via",
        "footer_dwd": "DWD-Synoptik via",
        "footer_openmeteo": "Druck & Meteorologie via",
        "footer_chat": "Community-Signal aus anonymisierten Auszügen des",
        "footer_chat_suffix": "-Chats.",
    },
    "en": {
        "strip_forecast": "Forecast",
        "strip_actual": "Actual (Urfeld peak)",
        "strip_legend_go": "wind (≥ 12 kt)",
        "strip_legend_maybe": "marginal (8–12 kt)",
        "strip_legend_no_go": "no wind (< 8 kt)",
        "strip_legend_empty": "no data",
        "live_header": "Live at Urfeld",
        "live_now": "now",
        "live_gust_label": "gust",
        "live_last_hour": "last-hour average",
        "live_trend_up": "rising",
        "live_trend_down": "dropping",
        "live_trend_flat": "steady",
        "live_unavailable": "Urfeld sensor not reachable right now.",
        "chart_aria": "Urfeld wind, last 6 hours",
        "chart_legend_avg": "avg wind",
        "chart_legend_gust": "gust",
        "chart_legend_ignition": "ignition (8 kt)",
        "chart_legend_session": "session (12 kt)",
        "webcam_label": "Urfeld webcam",
        "lead": "Will the thermal blow at Walchensee today?",
        "verdict_go": "GO",
        "verdict_maybe": "MAYBE",
        "verdict_no_go": "NO GO",
        "for_day": "For",
        "community_prefix": "Community",
        "sentiment_positive": "positive",
        "sentiment_negative": "skeptical",
        "sentiment_mixed": "mixed",
        "sentiment_quiet": "quiet",
        "last_30_days": "Last 30 days",
        "no_data": "No data yet — next scheduled forecast runs at 08:00 CET.",
        "no_data_headline": "—",
        "advanced_label": "Advanced — all 12 rules & chat excerpts",
        "col_rule": "Rule",
        "col_signal": "Signal",
        "col_reason": "Reason",
        "chat_header_advanced": "Chat excerpts (anonymised)",
        "chat_footer": "Anonymised. Source:",
        "windinfo_label": "windinfo.eu community chat",
        "footer_outline": "Thresholds still guesses from Lake Garda analogues — calibration in progress.",
        "footer_urfeld": "Urfeld wind: © Panoramahotel Karwendelblick, via",
        "footer_dwd": "DWD synoptic via",
        "footer_openmeteo": "Pressure & meteorology via",
        "footer_chat": "Community signal from anonymised excerpts of the",
        "footer_chat_suffix": ".",
    },
}


_HORIZON_LABELS = {
    "de": ["Heute", "Morgen", "Übermorgen"],
    "en": ["Today", "Tomorrow", "Day after"],
}

_DE_WEEKDAY_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
_EN_WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_date(d: date | str, lang: str, style: str) -> str:
    """Locale-aware date formatter. Styles:
      short — "23.4." (de) / "Apr 23" (en)          — day-tab date, etc.
      full  — "23.04.2026" (de) / "Apr 23, 2026" (en) — verdict meta
      strip — "Di 24.04." (de) / "Tue Apr 24" (en)    — 30-day strip labels
    """
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if style == "short":
        return f"{d.day}.{d.month}." if lang == "de" else f"{d.strftime('%b')} {d.day}"
    if style == "full":
        if lang == "de":
            return f"{d.day:02d}.{d.month:02d}.{d.year}"
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    if style == "strip":
        names = _DE_WEEKDAY_SHORT if lang == "de" else _EN_WEEKDAY_SHORT
        wd = names[d.weekday()]
        if lang == "de":
            return f"{wd} {d.day:02d}.{d.month:02d}."
        return f"{wd} {d.strftime('%b')} {d.day}"
    return d.isoformat()


def _horizon_days(today: date, lang: str, selected_iso: str) -> list[dict]:
    """Return today + next 2 days with label, verdict (if logged), selection flag."""
    labels = _HORIZON_LABELS.get(lang, _HORIZON_LABELS["en"])
    out: list[dict] = []
    for i, label in enumerate(labels):
        d = today + timedelta(days=i)
        record = _cached_read(d.isoformat())
        out.append({
            "iso": d.isoformat(),
            "label": label,
            "short_date": _fmt_date(d, lang, "short"),
            "verdict": record.get("overall") if record else None,
            "selected": d.isoformat() == selected_iso,
        })
    return out


def _resolve_lang(request: Request) -> str:
    """Priority: ?lang= → cookie → Accept-Language header → 'de'."""
    q = request.query_params.get("lang")
    if q in _UI:
        return q
    cookie = request.cookies.get("lang")
    if cookie in _UI:
        return cookie
    accept = (request.headers.get("accept-language") or "").lower()
    if accept.startswith("en"):
        return "en"
    return "de"


def _rule_tooltip(rule_name: str, lang: str) -> str:
    entry = _RULE_DESCRIPTIONS.get(rule_name)
    if not entry:
        return ""
    return entry.get(lang) or entry.get("de") or ""

app = FastAPI(title="Walchi Oracle")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Simple 60s TTL cache so a page load hits GCS at most once per minute per day.
_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[dict | None, float]] = {}

# Urfeld live wind cache — 5 min TTL to match the anemometer's ~10 min sample
# cadence without hammering Addicted-Sports on every page load.
_URFELD_LIVE_TTL_S = 300.0
_urfeld_live: dict | None = None
_urfeld_live_at: float = 0.0


async def _fetch_urfeld_live() -> dict:
    """Return a snapshot of the latest Urfeld samples: current / 1h avg / trend
    plus an inline-SVG chart of the last 6 hours.

    Result shape (keys absent on failure):
      {available: True, latest_avg_kt, latest_gust_kt, latest_at,
       last_hour_avg, prev_hour_avg, trend: "up"|"down"|"flat",
       chart_svg: "<svg …>" (empty when too few samples)}
    """
    global _urfeld_live, _urfeld_live_at
    now = time.time()
    if _urfeld_live is not None and now - _urfeld_live_at < _URFELD_LIVE_TTL_S:
        return _urfeld_live

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            samples = await fetch_urfeld_day_curve(date.today(), client=client)
    except Exception as exc:  # network, CSRF, Addicted-Sports down — degrade gracefully
        _urfeld_live = {"available": False, "error": str(exc)}
        _urfeld_live_at = now
        return _urfeld_live

    if not samples:
        _urfeld_live = {"available": False, "error": "no samples"}
        _urfeld_live_at = now
        return _urfeld_live

    # Sort newest → oldest for easy windowing.
    samples = sorted(samples, key=lambda s: s.measured_at, reverse=True)
    latest = samples[0]
    last_hour = [s for s in samples if (latest.measured_at - s.measured_at).total_seconds() <= 3600]
    prev_hour = [
        s for s in samples
        if 3600 < (latest.measured_at - s.measured_at).total_seconds() <= 7200
    ]
    chart_window = [
        s for s in samples
        if (latest.measured_at - s.measured_at).total_seconds() <= 6 * 3600
    ]

    last_hour_avg = sum(s.avg_knots for s in last_hour) / len(last_hour)
    prev_hour_avg = (
        sum(s.avg_knots for s in prev_hour) / len(prev_hour) if prev_hour else None
    )
    if prev_hour_avg is None:
        trend = "flat"
    elif last_hour_avg > prev_hour_avg + 1.0:
        trend = "up"
    elif last_hour_avg < prev_hour_avg - 1.0:
        trend = "down"
    else:
        trend = "flat"

    _urfeld_live = {
        "available": True,
        "latest_avg_kt": round(latest.avg_knots, 1),
        "latest_gust_kt": round(latest.gust_knots, 1),
        "latest_at": latest.measured_at.isoformat(),
        "last_hour_avg": round(last_hour_avg, 1),
        "prev_hour_avg": round(prev_hour_avg, 1) if prev_hour_avg is not None else None,
        "trend": trend,
        "chart_svg": _wind_chart_svg(list(reversed(chart_window))),
    }
    _urfeld_live_at = now
    return _urfeld_live


def _wind_chart_svg(samples: list[UrfeldSample], width: int = 720, height: int = 120) -> str:
    """Render the last-N-hours wind curve as inline SVG (no JS / chart lib).

    Expects `samples` ordered oldest → newest. Returns "" when there's nothing
    meaningful to draw (< 2 samples). Y-axis scales so calm days still show
    the 8 / 12 kt reference lines legibly.
    """
    if len(samples) < 2:
        return ""

    gust_peak = max(s.gust_knots for s in samples)
    y_max = max(15.0, gust_peak * 1.1)

    pad_l, pad_r, pad_t, pad_b = 24, 8, 6, 16
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b

    t0 = samples[0].measured_at.timestamp()
    t_span = max(samples[-1].measured_at.timestamp() - t0, 1.0)

    def x(ts: float) -> float:
        return pad_l + (ts - t0) / t_span * inner_w

    def y(kt: float) -> float:
        return pad_t + inner_h - min(kt, y_max) / y_max * inner_h

    avg_pts = [(x(s.measured_at.timestamp()), y(s.avg_knots)) for s in samples]
    gust_pts = [(x(s.measured_at.timestamp()), y(s.gust_knots)) for s in samples]
    base_y = y(0)
    gust_poly = [(gust_pts[0][0], base_y), *gust_pts, (gust_pts[-1][0], base_y)]

    def pts_str(pts: list[tuple[float, float]]) -> str:
        return " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)

    y8, y12 = y(8), y(12)
    start_label = samples[0].measured_at.strftime("%H:%M")
    end_label = samples[-1].measured_at.strftime("%H:%M")

    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'class="wind-chart" role="img" aria-label="Urfeld wind, last 6 hours">'
        f'<polygon points="{pts_str(gust_poly)}" fill="#8b949e" fill-opacity="0.18" />'
        f'<line x1="{pad_l}" y1="{y8:.1f}" x2="{width - pad_r}" y2="{y8:.1f}" '
        f'stroke="#d29922" stroke-opacity="0.55" stroke-dasharray="3 4" stroke-width="1" />'
        f'<line x1="{pad_l}" y1="{y12:.1f}" x2="{width - pad_r}" y2="{y12:.1f}" '
        f'stroke="#2ea043" stroke-opacity="0.55" stroke-dasharray="3 4" stroke-width="1" />'
        f'<polyline points="{pts_str(avg_pts)}" fill="none" stroke="#c9d1d9" '
        f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" />'
        f'<text x="{pad_l - 4:.0f}" y="{y8 + 3:.1f}" fill="#8b949e" font-size="10" '
        f'text-anchor="end">8</text>'
        f'<text x="{pad_l - 4:.0f}" y="{y12 + 3:.1f}" fill="#8b949e" font-size="10" '
        f'text-anchor="end">12</text>'
        f'<text x="{pad_l}" y="{height - 3}" fill="#8b949e" font-size="10">{start_label}</text>'
        f'<text x="{width - pad_r}" y="{height - 3}" fill="#8b949e" font-size="10" '
        f'text-anchor="end">{end_label}</text>'
        f'</svg>'
    )


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


def _actual_verdict(peak_avg_kt: float | None) -> str | None:
    """Categorise the Urfeld-peak ground truth onto the same go/maybe/no_go scale.

    ≥ 12 kt = session-worthy (go); 8–12 kt = ignited but marginal (maybe);
    < 8 kt = didn't fire (no_go). None when no ground truth was logged.
    """
    if peak_avg_kt is None:
        return None
    if peak_avg_kt >= 12:
        return "go"
    if peak_avg_kt >= 8:
        return "maybe"
    return "no_go"


def _history(today: date, lang: str, days: int = 30) -> list[dict]:
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
            "day": _fmt_date(d, lang, "strip"),
            "verdict": record.get("overall") if record else None,
            "peak_avg_knots": peak,
            "actual": _actual_verdict(peak),
        })
    return items


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _summary_line(record: dict, lang: str) -> str:
    """One-liner reason shown under the verdict headline."""
    overall = record.get("overall")
    verdicts = record.get("verdicts", [])

    def _reason(v: dict) -> str:
        # Prefer the language-specific reason; fall back to legacy 'reason'
        # for records written before bilingual reasons shipped.
        return v.get(f"reason_{lang}") or v.get("reason") or ""

    if overall == "no_go":
        blocker = next((v for v in verdicts if v["signal"] == "no_go"), None)
        return _reason(blocker) if blocker else "—"
    if overall == "go":
        go_count = sum(1 for v in verdicts if v["signal"] == "go")
        if lang == "en":
            return f"{go_count} of {len(verdicts)} rules green."
        return f"{go_count} von {len(verdicts)} Regeln grün."
    maybe = next((v for v in verdicts if v["signal"] == "maybe"), None)
    if maybe:
        return _reason(maybe)
    return "Mixed signals." if lang == "en" else "Gemischte Signale."


def _chat_sentiment(messages: list[dict]) -> dict:
    """Derive a go/no_go/quiet/mixed signal from a list of chat messages."""
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


def _public_view(record: dict | None, messages: list[dict] | None = None) -> dict | None:
    """Strip personal data (chat authors, channel names) before rendering.

    Raw logs in GCS keep the full fields for calibration — only this
    projection is what ends up in HTML served at the public custom domain.
    `messages` overrides the record's chat_messages (used when filtering by
    day-reference); if not passed, falls back to the record's full list.
    """
    if record is None:
        return None
    projection = dict(record)
    source = messages if messages is not None else record.get("chat_messages", [])
    projection["chat_messages"] = [
        {
            "posted_at": m.get("posted_at"),
            "text": _HANDLE_RE.sub("@…", m.get("text") or ""),
        }
        for m in source
    ]
    return projection


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    lang = _resolve_lang(request)
    today = date.today()

    # Which day to show? ?day=YYYY-MM-DD (within [today, today+2]); else today.
    selected_day = today
    requested = request.query_params.get("day")
    if requested:
        try:
            parsed = date.fromisoformat(requested)
            if timedelta(0) <= parsed - today <= timedelta(days=2):
                selected_day = parsed
        except ValueError:
            pass

    # Fall back to the most-recent-available record only when today's isn't yet
    # written (early in the morning before the scheduled job has run).
    raw = _cached_read(selected_day.isoformat())
    if raw is None and selected_day == today:
        raw = _most_recent(today)

    summary = _summary_line(raw, lang) if raw else ""
    # Filter chat messages by day-reference heuristic so the badge + Advanced
    # panel both reflect what the community said ABOUT the selected day.
    all_messages = (raw or {}).get("chat_messages", []) or []
    day_messages = _messages_for_day(all_messages, selected_day, today)
    sentiment = _chat_sentiment(day_messages) if raw else None
    tooltips = {name: _rule_tooltip(name, lang) for name in _RULE_DESCRIPTIONS}
    horizon = _horizon_days(today, lang, selected_day.isoformat())
    # Live wind + webcam always shown — it's "current state at the lake",
    # independent of which forecast day is selected.
    live = await _fetch_urfeld_live()

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "current": _public_view(raw, messages=day_messages),
            "summary": summary,
            "sentiment": sentiment,
            "history": _history(today, lang),
            "selected_date_label": _fmt_date(selected_day, lang, "full"),
            "today_iso": today.isoformat(),
            "selected_iso": selected_day.isoformat(),
            "horizon": horizon,
            "rule_descriptions": tooltips,
            "live": live,
            "t": _UI[lang],
            "lang": lang,
        },
    )
    q = request.query_params.get("lang")
    if q in _UI:
        response.set_cookie("lang", q, max_age=365 * 24 * 3600, samesite="lax")
    return response
