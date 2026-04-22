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
from fastapi.responses import HTMLResponse, Response
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
        "footer_outline": "Outline = Urfeld-Böe ≥ 12 kt (Session-würdig). Schwellwerte noch Schätzungen auf Basis von Lake-Garda-Analoga — Kalibrierung läuft.",
        "footer_urfeld": "Urfeld-Wind: © Panoramahotel Karwendelblick, via",
        "footer_dwd": "DWD-Synoptik via",
        "footer_openmeteo": "Druck & Meteorologie via",
        "footer_chat": "Community-Signal aus anonymisierten Auszügen des",
        "footer_chat_suffix": "-Chats.",
    },
    "en": {
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
        "footer_outline": "Outline = Urfeld gust ≥ 12 kt (session-worthy). Thresholds still guesses from Lake Garda analogues — calibration in progress.",
        "footer_urfeld": "Urfeld wind: © Panoramahotel Karwendelblick, via",
        "footer_dwd": "DWD synoptic via",
        "footer_openmeteo": "Pressure & meteorology via",
        "footer_chat": "Community signal from anonymised excerpts of the",
        "footer_chat_suffix": ".",
    },
}


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
def index(request: Request) -> Response:
    lang = _resolve_lang(request)
    today = date.today()
    raw = _most_recent(today)
    summary = _summary_line(raw) if raw else ""
    sentiment = _chat_sentiment(raw) if raw else None
    # Pre-resolve per-rule tooltip string in the chosen language.
    tooltips = {name: _rule_tooltip(name, lang) for name in _RULE_DESCRIPTIONS}

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "current": _public_view(raw),
            "summary": summary,
            "sentiment": sentiment,
            "history": _history(today),
            "today_iso": today.isoformat(),
            "rule_descriptions": tooltips,
            "t": _UI[lang],
            "lang": lang,
        },
    )
    # If the visitor picked a language via ?lang=, remember it for a year.
    q = request.query_params.get("lang")
    if q in _UI:
        response.set_cookie("lang", q, max_age=365 * 24 * 3600, samesite="lax")
    return response
