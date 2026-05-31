"""FastAPI dashboard for the Walchi Oracle.

Reads per-day records from the same store the scheduled job writes
(local disk in dev, GCS in production via $RUNS_BUCKET). Shows:

- Today's verdict + rule breakdown
- Live Urfeld wind panel + 6-hour curve
- 30-day strip of forecast vs. actual peak wind
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from oracle.calibration import actual_verdict_duration as _actual_verdict_duration
from oracle.knowledge.rules import Severity, Signal
from oracle.logger import RunStore, default_store
from oracle.pillars.measurements import UrfeldSample, fetch_urfeld_day_curve


@lru_cache(maxsize=1)
def _store() -> RunStore:
    """Process-lifetime store handle. The dashboard is a long-running server, so
    re-selecting the backend (and rebuilding the GCS `storage.Client` in prod) on
    every cache miss is pure waste — the backend can't change without a restart."""
    return default_store()

# Per-rule UI strings. `label` is the short header in the Advanced panel;
# `description` is the `?` hover tooltip. One entry per rule keeps the two
# from drifting when rules are added or renamed.
@dataclass(frozen=True)
class RuleI18n:
    label: dict[str, str]
    description: dict[str, str]


_RULE_I18N: dict[str, RuleI18n] = {
    "thermik": RuleI18n(
        label={
            "de": "Druckgradient (München − Innsbruck)",
            "en": "Pressure gradient (Munich − Innsbruck)",
        },
        description={
            "de": "Luftdruck-Differenz München − Innsbruck. Positives Delta ≥ −1.0 hPa treibt die Nord-Süd-Thermik an.",
            "en": "Munich − Innsbruck pressure delta. ≥ −1.0 hPa drives the north-to-south thermal pump.",
        },
    ),
    "foehn_override": RuleI18n(
        label={"de": "Föhn (Bozen − Innsbruck)", "en": "Föhn (Bolzano − Innsbruck)"},
        description={
            "de": "Luftdruck-Differenz Bozen − Innsbruck. Positives Delta ≥ 4 hPa signalisiert Föhn — zerstört die Thermik.",
            "en": "Bolzano − Innsbruck pressure delta. ≥ 4 hPa signals Föhn — kills the local thermal.",
        },
    ),
    "overnight_cooling": RuleI18n(
        label={"de": "Nächtliche Abkühlung", "en": "Overnight cooling"},
        description={
            "de": "Mittlere Bewölkung 22:00 Vortag bis 06:00. Über 30 % schwächt die nächtliche Abkühlung und damit den Tagesgang.",
            "en": "Mean cloud cover 22:00 previous day to 06:00. Above 30 % weakens radiative cooling and the diurnal delta.",
        },
    ),
    "solar_radiation": RuleI18n(
        label={"de": "Sonneneinstrahlung", "en": "Solar radiation"},
        description={
            "de": "Peak-Sonneneinstrahlung zwischen 09:00 und 13:00. Unter 600 W/m² reicht die Hangheizung nicht für eine saubere Thermik.",
            "en": "Peak shortwave radiation 09:00–13:00. Below 600 W/m² the slopes don't heat enough for a clean thermal.",
        },
    ),
    "dew_point_spread": RuleI18n(
        label={"de": "Taupunkt-Abstand", "en": "Dew-point spread"},
        description={
            "de": "Kleinster (T − Td)-Abstand 09:00–13:00. Unter 2.5 °C geht Sonnenenergie in Verdunstung statt Lufterwärmung.",
            "en": "Smallest (T − Td) gap 09:00–13:00. Below 2.5 °C solar energy goes into evaporation, not sensible heating.",
        },
    ),
    "boundary_layer_height": RuleI18n(
        label={"de": "Grenzschicht-Höhe", "en": "Boundary-layer height"},
        description={
            "de": "Grenzschicht-Höhe 09:00–13:00. Unter 600 m bleibt die Thermik gedeckelt; ab 1000 m tiefe Durchmischung.",
            "en": "Boundary-layer height 09:00–13:00. Below 600 m the thermal is capped; above 1000 m = deep mixing.",
        },
    ),
    "post_rain_moisture": RuleI18n(
        label={"de": "Bodenfeuchte / Regen", "en": "Ground moisture / rain"},
        description={
            "de": "Nasser Boden (Regen gestern ≥ 2 mm oder Bodenfeuchte > 0.35 m³/m³) leitet Sonnenenergie in Verdunstung statt in Hangheizung.",
            "en": "Wet ground (≥ 2 mm rain yesterday or soil moisture > 0.35 m³/m³) diverts solar energy to evaporation.",
        },
    ),
    "atmospheric_stability": RuleI18n(
        label={"de": "Atmosphärische Stabilität", "en": "Atmospheric stability"},
        description={
            "de": "Lifted Index 09:00–13:00. LI ≥ +10 = Atmosphäre zu stabil (Thermik gedeckelt); LI ≤ −2 = Gewittergefahr.",
            "en": "Lifted index 09:00–13:00. LI ≥ +10 = atmosphere too stable (capped); LI ≤ −2 = thunderstorm risk.",
        },
    ),
    "daytime_clouds": RuleI18n(
        label={"de": "Tagesbewölkung", "en": "Daytime cloud cover"},
        description={
            "de": "Max. tiefe Bewölkung 09:00–13:00. Über 60 % beschattet Herzogstand/Jochberg-Hänge und stoppt die Hangheizung.",
            "en": "Max low-cloud cover 09:00–13:00. Above 60 % shades the Herzogstand/Jochberg slopes and stops slope heating.",
        },
    ),
    "upper_level_wind": RuleI18n(
        label={"de": "Höhenwind (850 / 700 hPa)", "en": "Upper-level wind (850 / 700 hPa)"},
        description={
            "de": "850 hPa Windrichtung am Morgen-Peak + 700 hPa Querströmung. SSE-Strömung 150–210° läuft gegen die N-Thermik; > 25 kt in 700 hPa entkoppelt das Tal.",
            "en": "850 hPa direction at the morning peak + 700 hPa crossflow. SSE 150–210° opposes the N-thermal; > 25 kt at 700 hPa decouples the valley.",
        },
    ),
    "synoptic_override": RuleI18n(
        label={"de": "Synoptik-Wind", "en": "Synoptic-flow override"},
        description={
            "de": "Wind in 850 hPa (~1500 m) 09:00–13:00. Über 15 kt zerstört oder deformiert die lokale Thermikzelle.",
            "en": "Wind at 850 hPa (~1500 m) 09:00–13:00. Above 15 kt destroys or deforms the local thermal cell.",
        },
    ),
    "thermal_ignition": RuleI18n(
        label={"de": "Thermik-Zündung (Live)", "en": "Thermal ignition (live)"},
        description={
            "de": "Aktuelle Messwerte Urfeld-Boje + DWD-Station. Ab 8 kt Mittelwind gilt die Thermik als gezündet.",
            "en": "Live readings from the Urfeld buoy + nearest DWD station. ≥ 8 kt mean wind = thermal ignited.",
        },
    ),
}


# Static-UI translation dict. Rule REASON strings from the engine are not
# translated here — they're currently a mix of English and German and would
# need a proper refactor of the rules module to emit bilingual reasons.
_UI: dict[str, dict[str, str]] = {
    "de": {
        "strip_forecast": "Vorhersage",
        "strip_resimulated": "Neu berechnet (mit heutigen Regeln)",
        "strip_actual": "Tatsächlich (Session ≥ 1 h)",
        "strip_legend_go": "Session (≥ 1 h ≥ 11 kt)",
        "strip_legend_maybe": "marginal (≥ 1 h ≥ 8 kt)",
        "strip_legend_no_go": "kein Wind",
        "strip_legend_empty": "keine Daten",
        "live_header": "Aktuell in Urfeld",
        "live_now": "jetzt",
        "live_gust_label": "Böe",
        "live_last_hour": "Ø letzte Stunde",
        "live_trend_up": "steigend",
        "live_trend_down": "fallend",
        "live_trend_flat": "stabil",
        "live_unavailable": "Urfeld-Sensor gerade nicht erreichbar.",
        "historical_chart_header": "Verlauf in Urfeld",
        "chart_aria_historical": "Urfeld-Wind, ganzer Tag",
        "chart_aria": "Urfeld-Wind, letzte 6 Stunden",
        "chart_legend_avg": "Mittelwind",
        "chart_legend_gust": "Böe",
        "chart_legend_ignition": "Zündung (8 kt)",
        "chart_legend_session": "Session (11 kt)",
        "webcam_label": "Webcam Urfeld",
        "lead": "Geht heute Thermik am Walchensee?",
        "verdict_go": "LÄUFT",
        "verdict_maybe": "VIELLEICHT",
        "verdict_no_go": "FLAUTE",
        "for_day": "Für",
        "last_30_days": "Letzte 30 Tage",
        "no_data": "Keine Daten — warte auf die nächste Vorhersage (ca. 08:00 Ortszeit).",
        "no_data_headline": "—",
        "advanced_label": "Details — alle 12 Regeln",
        "view_label_original": "wie damals geschrieben",
        "view_label_resimulated": "neu berechnet",
        "col_rule": "Regel",
        "col_signal": "Signal",
        "col_reason": "Begründung",
        "footer_outline": "Schwellwerte noch Schätzungen auf Basis von Gardasee-Analogien — Kalibrierung läuft.",
        "footer_urfeld": "Urfeld-Wind: © Panoramahotel Karwendelblick, via",
        "footer_dwd": "DWD-Synoptik via",
        "footer_openmeteo": "Druck- & Wetterdaten via",
        "footer_chat": "Lokaler Wind-Chat (Login bei",
        "footer_chat_suffix": " erforderlich).",
    },
    "en": {
        "strip_forecast": "Forecast",
        "strip_resimulated": "Re-scored (current aggregator)",
        "strip_actual": "Actual (session ≥ 1 h)",
        "strip_legend_go": "session (≥ 1 h ≥ 11 kt)",
        "strip_legend_maybe": "marginal (≥ 1 h ≥ 8 kt)",
        "strip_legend_no_go": "no wind",
        "strip_legend_empty": "no data",
        "live_header": "Live at Urfeld",
        "live_now": "now",
        "live_gust_label": "gust",
        "live_last_hour": "last-hour average",
        "live_trend_up": "rising",
        "live_trend_down": "dropping",
        "live_trend_flat": "steady",
        "live_unavailable": "Urfeld sensor not reachable right now.",
        "historical_chart_header": "Wind curve at Urfeld",
        "chart_aria_historical": "Urfeld wind, full day",
        "chart_aria": "Urfeld wind, last 6 hours",
        "chart_legend_avg": "avg wind",
        "chart_legend_gust": "gust",
        "chart_legend_ignition": "ignition (8 kt)",
        "chart_legend_session": "session (11 kt)",
        "webcam_label": "Urfeld webcam",
        "lead": "Will the thermal wind kick in at Walchensee today?",
        "verdict_go": "GO",
        "verdict_maybe": "MAYBE",
        "verdict_no_go": "NO GO",
        "for_day": "For",
        "last_30_days": "Last 30 days",
        "no_data": "No data yet — next scheduled forecast runs at 08:00 CET.",
        "no_data_headline": "—",
        "advanced_label": "Advanced — all 12 rules",
        "view_label_original": "as written at the time",
        "view_label_resimulated": "re-scored",
        "col_rule": "Rule",
        "col_signal": "Signal",
        "col_reason": "Reason",
        "footer_outline": "Thresholds still guesses from Lake Garda analogues — calibration in progress.",
        "footer_urfeld": "Urfeld wind: © Panoramahotel Karwendelblick, via",
        "footer_dwd": "DWD synoptic via",
        "footer_openmeteo": "Pressure & meteorology via",
        "footer_chat": "Local windsurf community chat (login at",
        "footer_chat_suffix": " required).",
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


def _per_lang(
    pick: Callable[[RuleI18n], dict[str, str]], fallback: str,
) -> dict[str, dict[str, str]]:
    """Pivot _RULE_I18N into {lang: {rule_name: text}} once at import.

    `pick` selects either the label or description map per rule. `fallback`
    is the empty-string sentinel for missing tooltips, or the literal "rule"
    to mean "fall back to the rule name itself" (for labels).
    """
    return {
        lang: {
            name: pick(entry).get(lang) or pick(entry).get("de")
                  or (name if fallback == "rule" else fallback)
            for name, entry in _RULE_I18N.items()
        }
        for lang in _UI
    }


_TOOLTIPS_BY_LANG = _per_lang(lambda e: e.description, fallback="")
_LABELS_BY_LANG = _per_lang(lambda e: e.label, fallback="rule")

app = FastAPI(title="Walchi Oracle")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Simple 60s TTL cache so a page load hits GCS at most once per minute per day.
# Bounded: working set is ~32 entries (30-day strip + 3 horizon days), cap a bit
# higher so a few historical clicks don't immediately evict the strip.
_CACHE_TTL_S = 60.0
_CACHE_MAX_ENTRIES = 64
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

    chart_samples = list(reversed(chart_window))
    _urfeld_live = {
        "available": True,
        "latest_avg_kt": round(latest.avg_knots, 1),
        "latest_gust_kt": round(latest.gust_knots, 1),
        "latest_at": latest.measured_at.isoformat(),
        "last_hour_avg": round(last_hour_avg, 1),
        "prev_hour_avg": round(prev_hour_avg, 1) if prev_hour_avg is not None else None,
        "trend": trend,
        "chart_svg": _wind_chart_svgs(chart_samples),
    }
    _urfeld_live_at = now
    return _urfeld_live


# Tooltip format per language. Kept compact so the browser's native tooltip
# stays one line even on narrow viewports.
_CHART_TOOLTIP_FMT = {
    "de": "{t} · Ø {avg:.1f} kt · Böe {gust:.1f} kt",
    "en": "{t} · avg {avg:.1f} kt · gust {gust:.1f} kt",
}

# Reference lines drawn on the wind charts: ignition (8 kt) and the sustained-
# session bar (11 kt, mirroring calibration._DURATION_GO_KT). Named here so the
# line geometry and its axis label can't drift apart. The legend prose in `_UI`
# still spells the numbers out; keep them in sync when tuning.
_CHART_IGNITION_KT = 8
_CHART_SESSION_KT = 11


def _wind_chart_svgs(
    samples: list[UrfeldSample],
    width: int = 720,
    height: int = 120,
    fixed_xlim: tuple[float, float] | None = None,
    fixed_ymax: float | None = None,
) -> dict[str, str]:
    """Render the wind curve as inline SVG (no JS / chart lib), once per UI language.

    Geometry — axes, polylines, hover-disc positions — is identical across
    languages, so we compute it once and only vary the aria-label and the
    per-sample `<title>` tooltips. Returns `{lang: ""}` when fewer than two
    samples make a meaningful curve.

    Pass `fixed_xlim`=(start_ts, end_ts) and/or `fixed_ymax` (knots) to pin the
    axes to a fixed range — used by the historical chart so day-to-day clicks
    are visually comparable. Without them, the live chart auto-fits to its
    sample window so calm days still show the reference lines legibly.
    """
    if len(samples) < 2:
        return {lang: "" for lang in _UI}

    if fixed_ymax is not None:
        y_max = fixed_ymax
    else:
        gust_peak = max(s.gust_knots for s in samples)
        y_max = max(15.0, gust_peak * 1.1)

    pad_l, pad_r, pad_t, pad_b = 24, 8, 6, 16
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b

    if fixed_xlim is not None:
        t0, t1 = fixed_xlim
        t_span = max(t1 - t0, 1.0)
    else:
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

    y8, y11 = y(_CHART_IGNITION_KT), y(_CHART_SESSION_KT)  # ignition / session reference lines
    if fixed_xlim is not None:
        start_label = datetime.fromtimestamp(fixed_xlim[0]).strftime("%H:%M")
        end_label = datetime.fromtimestamp(fixed_xlim[1]).strftime("%H:%M")
    else:
        start_label = samples[0].measured_at.strftime("%H:%M")
        end_label = samples[-1].measured_at.strftime("%H:%M")

    # Language-independent body: polygons, polylines, axis labels.
    body = (
        f'<polygon points="{pts_str(gust_poly)}" fill="#8b949e" fill-opacity="0.18" />'
        f'<line x1="{pad_l}" y1="{y8:.1f}" x2="{width - pad_r}" y2="{y8:.1f}" '
        f'stroke="#d29922" stroke-opacity="0.55" stroke-dasharray="3 4" stroke-width="1" />'
        f'<line x1="{pad_l}" y1="{y11:.1f}" x2="{width - pad_r}" y2="{y11:.1f}" '
        f'stroke="#2ea043" stroke-opacity="0.55" stroke-dasharray="3 4" stroke-width="1" />'
        f'<polyline points="{pts_str(avg_pts)}" fill="none" stroke="#c9d1d9" '
        f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" />'
        f'<text x="{pad_l - 4:.0f}" y="{y8 + 3:.1f}" fill="#8b949e" font-size="10" '
        f'text-anchor="end">{_CHART_IGNITION_KT}</text>'
        f'<text x="{pad_l - 4:.0f}" y="{y11 + 3:.1f}" fill="#8b949e" font-size="10" '
        f'text-anchor="end">{_CHART_SESSION_KT}</text>'
        f'<text x="{pad_l}" y="{height - 3}" fill="#8b949e" font-size="10">{start_label}</text>'
        f'<text x="{width - pad_r}" y="{height - 3}" fill="#8b949e" font-size="10" '
        f'text-anchor="end">{end_label}</text>'
    )

    # Hover discs over each sample. Transparent fill + <title> child = native
    # browser tooltip, no JS. Small visible dot on the avg line for hit target.
    # Tooltip text is the only language-dependent part of each circle pair.
    def hover_layer(lang: str) -> str:
        tip_fmt = _CHART_TOOLTIP_FMT.get(lang, _CHART_TOOLTIP_FMT["en"])
        parts: list[str] = []
        for s, (px, py) in zip(samples, avg_pts):
            label = tip_fmt.format(
                t=s.measured_at.strftime("%H:%M"), avg=s.avg_knots, gust=s.gust_knots,
            )
            parts.append(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2" fill="#c9d1d9" />'
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="10" fill="transparent" '
                f'pointer-events="all"><title>{_svg_escape(label)}</title></circle>'
            )
        return "".join(parts)

    return {
        lang: (
            f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
            f'class="wind-chart" role="img" '
            f'aria-label="{_svg_escape(_UI.get(lang, _UI["en"])["chart_aria"])}">'
            f'{body}{hover_layer(lang)}</svg>'
        )
        for lang in _UI
    }


def _svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _cached_read(iso_day: str) -> dict | None:
    now = time.time()
    value, expires_at = _cache.get(iso_day, (None, 0.0))
    if now < expires_at:
        return value
    fresh = _store().read(iso_day)
    _cache[iso_day] = (fresh, now + _CACHE_TTL_S)
    _evict_if_full()
    return fresh


def _evict_if_full() -> None:
    while len(_cache) > _CACHE_MAX_ENTRIES:
        # Evict the entry that expires soonest — usually a stale one that's
        # about to be re-fetched anyway. Keeps the dict O(N) bounded.
        oldest = min(_cache, key=lambda k: _cache[k][1])
        _cache.pop(oldest, None)


async def _prefetch_days(iso_days: list[str]) -> None:
    """Populate `_cache` for `iso_days` concurrently. No-op for fresh hits.

    The dashboard renders horizon (3 days) + a 30-day history strip on every
    request — 33 sync `store.read` calls in series is a 33-RTT GCS storm on a
    cold cache. Fanning the misses out via `asyncio.to_thread` collapses that
    to one wall-clock RTT (modulo thread-pool size, default ≥ 5).
    """
    now = time.time()
    misses = list({d for d in iso_days if _cache.get(d, (None, 0.0))[1] <= now})
    if not misses:
        return
    store = _store()
    fresh_values = await asyncio.gather(
        *(asyncio.to_thread(store.read, d) for d in misses)
    )
    expires_at = now + _CACHE_TTL_S
    for d, fresh in zip(misses, fresh_values):
        _cache[d] = (fresh, expires_at)
    _evict_if_full()


def _most_recent(today: date) -> dict | None:
    """Find the latest available record within the past week."""
    for i in range(8):
        record = _cached_read((today - timedelta(days=i)).isoformat())
        if record is not None:
            return record
    return None


def _history(today: date, lang: str, days: int = 30) -> list[dict]:
    """30-day strip: one entry per day, oldest first."""
    items: list[dict] = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        record = _cached_read(d.isoformat())
        peak = None
        verdict = None
        resimulated = None
        machine: dict | None = None
        if record:
            machine = (record.get("ground_truth") or {}).get("machine") or {}
            peak = machine.get("peak_avg_knots")
            verdict = record.get("overall")
            # Re-scored verdict under the current aggregator. Falls back to the
            # historical `overall` so days that pre-date `oracle rescore` (or
            # records too incomplete to re-score) still show *something* in the
            # row instead of an empty cell.
            resimulated = record.get("overall_resimulated") or verdict
        items.append({
            "iso": d.isoformat(),
            "day": _fmt_date(d, lang, "strip"),
            "verdict": verdict,
            "resimulated": resimulated,
            "peak_avg_knots": peak,
            "actual": _actual_verdict_duration(machine),
        })
    return items


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _summary_line(overall: str | None, verdicts: list[dict], lang: str) -> str:
    """One-liner reason shown under the verdict headline.

    Caller picks which verdict layer (original write-time vs. rescored) to
    explain — both come from the same record but represent different
    aggregator runs over the same inputs.
    """

    def _reason(v: dict) -> str:
        # Prefer the language-specific reason; fall back to legacy 'reason'
        # for records written before bilingual reasons shipped.
        return v.get(f"reason_{lang}") or v.get("reason") or ""

    if overall == Signal.NO_GO:
        # Prefer a hard blocker (true cause under the severity-aware aggregator).
        # Fall back to any NO_GO for legacy logs written before severity shipped.
        blocker = next(
            (v for v in verdicts
             if v["signal"] == Signal.NO_GO and v.get("severity") == Severity.HARD),
            None,
        ) or next((v for v in verdicts if v["signal"] == Signal.NO_GO), None)
        return _reason(blocker) if blocker else "—"
    if overall == Signal.GO:
        go_count = sum(1 for v in verdicts if v["signal"] == Signal.GO)
        if lang == "en":
            return f"{go_count} of {len(verdicts)} rules green."
        return f"{go_count} von {len(verdicts)} Regeln grün."
    # Overall is MAYBE — a soft NO_GO is more informative than a generic MAYBE,
    # so surface that first if any rule emitted one.
    soft_blocker = next(
        (v for v in verdicts
         if v["signal"] == Signal.NO_GO and v.get("severity") == Severity.SOFT),
        None,
    )
    if soft_blocker:
        return _reason(soft_blocker)
    maybe = next((v for v in verdicts if v["signal"] == Signal.MAYBE), None)
    if maybe:
        return _reason(maybe)
    return "Mixed signals." if lang == "en" else "Gemischte Signale."


def _samples_from_record(record: dict | None) -> list[UrfeldSample]:
    """Reconstruct the Urfeld day curve from a logged record's backfilled
    ground truth. Returns [] when samples are missing or malformed."""
    if not record:
        return []
    machine = (record.get("ground_truth") or {}).get("machine") or {}
    raw_samples = machine.get("samples") or []
    out: list[UrfeldSample] = []
    for s in raw_samples:
        try:
            out.append(UrfeldSample(
                measured_at=datetime.fromisoformat(s["t"]),
                avg_knots=float(s["avg_kt"]),
                gust_knots=float(s["gust_kt"]),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda s: s.measured_at)


def _no_data_chart_svg(lang: str, width: int = 720, height: int = 120) -> str:
    """Empty-state placeholder with the same dimensions as the wind chart, so
    layout doesn't shift when clicking through Urfeld-outage days."""
    label = _UI.get(lang, _UI["en"]).get("strip_legend_empty", "no data")
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'class="wind-chart" role="img" aria-label="{_svg_escape(label)}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="transparent" '
        f'stroke="#30363d" stroke-dasharray="4 4" stroke-width="1" />'
        f'<text x="{width/2:.0f}" y="{height/2 + 4:.0f}" fill="#8b949e" '
        f'font-size="13" text-anchor="middle">— {_svg_escape(label)} —</text>'
        f'</svg>'
    )


def _historical_chart_payload(record: dict | None) -> dict:
    """Build the historical wind-chart payload from a record's stored samples.

    Always returns a payload (never None) so the template renders a chart slot
    for every past day — outage days get a "keine Daten" placeholder rather
    than a missing block. Layout stays stable when clicking through the strip.

    Axes are pinned to 06:00–21:00 × 0–25 kt across all data-bearing days so
    clicks are visually comparable: same X-position = same time of day, same
    Y-height = same wind strength. The Urfeld buoy only records during
    daylight (~07:00–20:00), so 06:00–21:00 frames the thermal window without
    empty padding.
    """
    samples = _samples_from_record(record)
    if len(samples) < 2:
        return {
            "has_data": False,
            "chart_svg": {lang: _no_data_chart_svg(lang) for lang in _UI},
        }
    day = samples[0].measured_at.date()
    xlim = (
        datetime.combine(day, dtime(6, 0)).timestamp(),
        datetime.combine(day, dtime(21, 0)).timestamp(),
    )
    return {
        "has_data": True,
        "chart_svg": _wind_chart_svgs(samples, fixed_xlim=xlim, fixed_ymax=25.0),
    }


def _public_view(record: dict | None) -> dict | None:
    """Pass-through for now; kept as the single seam where any future
    record-level redaction would live before HTML rendering."""
    if record is None:
        return None
    projection = dict(record)
    projection.pop("chat_messages", None)  # legacy field on older logs
    return projection


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    lang = _resolve_lang(request)
    today = date.today()

    # Which day? ?day=YYYY-MM-DD; allow [today-30, today+2] so strip cells can
    # be clicked to inspect any logged historical day in the calibration window.
    selected_day = today
    requested = request.query_params.get("day")
    if requested:
        try:
            parsed = date.fromisoformat(requested)
            if timedelta(days=-30) <= parsed - today <= timedelta(days=2):
                selected_day = parsed
        except ValueError:
            pass

    # Which verdict layer? ?view=original|resimulated. Default is the rescored
    # one (current aggregator's call). `original` shows what was actually
    # written on the day, useful for seeing how calibration has shifted.
    view = request.query_params.get("view")
    if view not in ("original", "resimulated"):
        view = "resimulated"

    # Warm the cache for every day this request will read (selected + horizon
    # + 30-day strip + 7-day fallback for `_most_recent`) in one parallel
    # fan-out, alongside the live-Urfeld fetch. After this gather, every
    # subsequent `_cached_read` is a hit.
    horizon_isos = [(today + timedelta(days=i)).isoformat() for i in range(3)]
    history_isos = [(today - timedelta(days=i)).isoformat() for i in range(30)]
    fallback_isos = [(today - timedelta(days=i)).isoformat() for i in range(8)]
    all_isos = horizon_isos + history_isos + fallback_isos + [selected_day.isoformat()]
    _, live = await asyncio.gather(_prefetch_days(all_isos), _fetch_urfeld_live())

    # Fall back to the most-recent-available record only when today's isn't yet
    # written (early in the morning before the scheduled job has run).
    raw = _cached_read(selected_day.isoformat())
    if raw is None and selected_day == today:
        raw = _most_recent(today)

    if raw and view == "original":
        display_overall = raw.get("overall")
        display_verdicts = raw.get("verdicts", [])
    elif raw:
        display_overall = raw.get("overall_resimulated") or raw.get("overall")
        display_verdicts = raw.get("verdicts_resimulated") or raw.get("verdicts", [])
    else:
        display_overall = None
        display_verdicts = []

    summary = _summary_line(display_overall, display_verdicts, lang) if raw else ""
    tooltips = _TOOLTIPS_BY_LANG[lang]
    rule_labels = _LABELS_BY_LANG[lang]
    horizon = _horizon_days(today, lang, selected_day.isoformat())
    is_today = selected_day == today
    historical = None if is_today else _historical_chart_payload(raw)

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "current": _public_view(raw),
            "display_overall": display_overall,
            "display_verdicts": display_verdicts,
            "view": view,
            "summary": summary,
            "history": _history(today, lang),
            "selected_date_label": _fmt_date(selected_day, lang, "full"),
            "today_iso": today.isoformat(),
            "selected_iso": selected_day.isoformat(),
            "horizon": horizon,
            "rule_descriptions": tooltips,
            "rule_labels": rule_labels,
            "live": live,
            "historical": historical,
            "is_today": is_today,
            "t": _UI[lang],
            "lang": lang,
        },
    )
    q = request.query_params.get("lang")
    if q in _UI:
        response.set_cookie("lang", q, max_age=365 * 24 * 3600, samesite="lax")
    return response
