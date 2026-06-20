"""FastAPI dashboard for the Walchi Oracle.

Reads per-day records from the same store the scheduled job writes
(local disk in dev, GCS in production via $RUNS_BUCKET). Shows:

- Today's verdict + rule breakdown
- Live Urfeld wind panel + 6-hour curve
- 30-day strip of forecast vs. actual peak wind
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from oracle.calibration import Report, compile_report
from oracle.calibration import (
    _label_record as _cal_label_record,
    _empty_confusion as _cal_empty_confusion,
    actual_verdict_duration as _actual_verdict_duration,
    constant_baselines as _cal_constant_baselines,
    storm_suspected as _storm_suspected,
)
from oracle.knowledge.rules import SIGNAL_ORDER, Severity, Signal
from oracle.logger import RunStore, default_store
from oracle.pillars.measurements import UrfeldSample, fetch_urfeld_day_curve
from oracle.traffic import real_browser_hit


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
    "no_insolation": RuleI18n(
        label={"de": "Keine Einstrahlung", "en": "No insolation"},
        description={
            "de": "Harte Sperre: dichte Tagesbewölkung (≥ 70 %) UND niedrige Morgenstrahlung (≤ 400 W/m²) zusammen — ohne Sonne keine Hangheizung, keine Thermik. Einzeln sind beide nur weiche Signale; die Kombination ist eindeutig.",
            "en": "Hard veto: heavy daytime cloud (≥ 70 %) AND low morning solar (≤ 400 W/m²) together — no sun, no slope heating, no thermal. Either alone is only a soft hint; the combination is decisive.",
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
    "air_lake_delta": RuleI18n(
        label={"de": "See-/Lufttemperatur", "en": "Lake / air temperature"},
        description={
            "de": "Luft−Wasser-Temperaturdifferenz. Kalter See (Luft wärmer als Wasser um > 10 °C) bremst die Frühjahrs-Thermik; warmer See unterstützt sie. Bojen-Wassertemperatur + Open-Meteo-Lufttemperatur.",
            "en": "Air−water temperature delta. Cold lake (air warmer than water by > 10 °C) opposes the spring thermal; warm lake aids it. Buoy water temperature + Open-Meteo air temperature.",
        },
    ),
}


# Static-UI translation dict. Rule REASON strings from the engine are not
# translated here — they're currently a mix of English and German and would
# need a proper refactor of the rules module to emit bilingual reasons.
_UI: dict[str, dict[str, str]] = {
    "de": {
        "strip_forecast": "Regelbasierte Vorhersage (tatsächlich genutzt)",
        "strip_ml": "ML-basierte Vorhersage — Logistisch (experimentell)",
        "strip_hgb": "ML-basierte Vorhersage — HistGradientBoosting (Blackbox-Modell, experimentell)",
        "strip_forecast_original": "Vergangene Vorhersageart",
        "strip_forecast_original_note": "Wie der Tag damals vorhergesagt wurde, vor der Nachkalibrierung der Regeln — nur zum Vergleich.",
        "strip_actual": "Tatsächliche Messwerte (Session ≥ 1 h)",
        "strip_legend_go": "Session (≥ 1 h ≥ 11 kt)",
        "strip_legend_maybe": "marginal (≥ 1 h ≥ 8 kt)",
        "strip_legend_no_go": "kein Wind",
        "strip_legend_empty": "keine Daten",
        "strip_legend_storm": "Gewitter (aus Kalibrierung ausgenommen)",
        "storm_hint": "⚡ Gewitter-Risiko — kein Thermik-Tag",
        "live_header": "Aktuell in Urfeld",
        "live_now": "jetzt",
        "live_gust_label": "Böe",
        "live_water_label": "Wasser",
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
        "ml_title": "🤖 ML-Klassifikator",
        "ml_experimental": "experimentell",
        "ml_note": "Zweitmeinung eines gelernten Modells, das parallel zu den 14 Regeln läuft. Wir sammeln Daten, um zu sehen, ob es den regelbasierten Ansatz langfristig schlägt. Die drei Werte sind seine Wahrscheinlichkeiten für GO / VIELLEICHT / FLAUTE.",
        "ml_prob_go": "GO",
        "ml_prob_maybe": "VIELLEICHT",
        "ml_prob_no_go": "FLAUTE",
        "for_day": "Für",
        "last_30_days": "Letzte 30 Tage",
        "no_data": "Keine Daten — warte auf die nächste Vorhersage (ca. 08:00 Ortszeit).",
        "no_data_headline": "—",
        "advanced_label": "Details — alle 14 Regeln",
        "view_label_original": "wie damals geschrieben",
        "col_rule": "Regel",
        "col_signal": "Signal",
        "col_reason": "Begründung",
        "stats_label": "Statistik",
        "stats_views_header": "Besucher",
        "stats_unique_visitors": "Echte Besucher (30 Tage)",
        "stats_total_hits": "Seitenaufrufe (30 Tage)",
        "stats_views_note": "Bots und Scanner herausgefiltert; IPv6-Adressen pro Haushalt (/64) zusammengefasst.",
        "stats_forecast_header": "Vorhersage-Qualität (ganze Saison)",
        "stats_sample_size": "Bewertete Tage",
        "stats_accuracy": "Treffergenauigkeit",
        "stats_accuracy_note": "Anteil der Tage, an denen die Vorhersage genau die richtige Kategorie (Session / marginal / kein Wind) getroffen hat.",
        "stats_ml_header": "ML-Klassifikator — Logistisch (Parallelmodell)",
        "stats_ml_note": "Dasselbe Tages-Set wie oben, aber Vorhersage = der experimentelle logistische ML-Klassifikator. Treibt nie die offizielle Vorhersage — nur Vergleich.",
        "stats_hgb_header": "ML-Klassifikator — HistGradientBoosting (Blackbox-Modell, Parallelmodell)",
        "stats_hgb_note": "Dasselbe Tages-Set, aber Vorhersage = HistGradientBoosting. Das stärkste Offline-Modell (Peirce +0.142), aber Blackbox und ohne Prod-Deployment. Nur Vergleich.",
        "stats_baseline": "Naiver Vergleich",
        "stats_baseline_note": "Was ein stumpfer „immer dasselbe\"-Tipp (die häufigste Kategorie) träfe. Die Vorhersage muss das schlagen, um nützlich zu sein.",
        "stats_quarantined_note": "Gewittertage aus der Wertung ausgenommen",
        "stats_advanced_label": "Erweiterte Statistik",
        "stats_advanced_rule_label": "Erweiterte Statistik — Regel-Ebene",
        "stats_advanced_ml_label": "Erweiterte Statistik — ML Logistisch (Parallelmodell)",
        "stats_advanced_hgb_label": "Erweiterte Statistik — HistGradientBoosting (Blackbox-Modell)",
        "stats_confusion_rule_header": "Konfusionsmatrix — Regel-Ebene",
        "stats_confusion_ml_header": "Konfusionsmatrix — ML Logistisch (Parallelmodell)",
        "stats_confusion_hgb_header": "Konfusionsmatrix — HistGradientBoosting (Blackbox-Modell)",
        "stats_confusion_header": "Konfusionsmatrix",
        "stats_confusion_note": "Zeilen = was vorhergesagt wurde, Spalten = was tatsächlich am See passiert ist. Auf der Diagonalen liegen die Treffer.",
        "stats_axis_forecast": "Vorhersage",
        "stats_axis_actual": "Tatsächlich",
        "stats_binary_note": "Sensitivität und Spezifität sind binäre Maße. Dafür werden die drei Kategorien zu „Wind-Tag ja/nein“ zusammengefasst: LÄUFT und VIELLEICHT zählen als Wind-Tag, FLAUTE als kein Wind — sowohl bei der Vorhersage als auch beim tatsächlichen Ausgang.",
        "stats_sensitivity": "Sensitivität",
        "stats_sensitivity_note": "Anteil der echten Wind-Tage (≥ 1 h ≥ 8 kt), an denen das Orakel nicht FLAUTE gesagt hat — wie viele gute Tage wir erwischen.",
        "stats_specificity": "Spezifität",
        "stats_specificity_note": "Anteil der Flaute-Tage, die korrekt als FLAUTE vorhergesagt wurden — wie selten wir umsonst an den See schicken.",
        "stats_unavailable": "—",
        "footer_outline": "Schwellwerte gegen ~10 Jahre Urfeld-Historie (Apr–Okt) kalibriert; einzelne Seltenereignis-Regeln noch Schätzungen.",
        "footer_urfeld": "Urfeld-Wind: © Panoramahotel Karwendelblick, via",
        "footer_dwd": "DWD-Synoptik via",
        "footer_openmeteo": "Druck- & Wetterdaten via",
        "footer_chat": "Lokaler Wind-Chat (Login bei",
        "footer_chat_suffix": " erforderlich).",
        "nav_today": "Heute",
        "nav_history": "Verlauf",
        "nav_stats": "Statistik",
        "nav_about": "Erklärung",
        "history_lead": "30 Tage Vorhersage vs. was am See passiert ist. Klicke auf einen einzelnen Tag um Details zu sehen.",
        "stats_lead": "Wie gut das Orakel trifft — und wie viele es lesen.",
        "about_lead": "Die vierzehn Regeln, mit denen das Orakel aus Druck, Wetter und Live-Messungen eine Thermik-Vorhersage baut.",
        "about_thermal_intro": "Die Walchensee-Thermik entsteht durch Hangheizung an den Südflanken von Herzogstand (1 731 m) und Jochberg (1 565 m). Erwärmt sich der Hang, steigt Luft auf und zieht kühle Luft aus dem Kochelsee-Becken (600 m) nach — komprimiert durch den Sattel zwischen den Gipfeln als Düseneffekt zum konsistenten N-bis-NNE-Wind. Die Grundformel: klare Nacht → starke Auskühlung → ungehinderter Morgensonnenschein auf den Hängen → Thermik ab Mittag. Das Orakel prüft 14 Bedingungen, die diesen Mechanismus begünstigen oder blockieren.",
        "about_docs_link": "Ausführliche Hintergründe (Meteorologie, Schwellenwerte, Kalibrierung) im technischen Modell-Dokument auf GitHub.",
        "about_rules_header": "Die 14 Regeln",
        "advanced_label_history": "Details — vergangene Vorhersageart",
        "index_history_link": "30-Tage-Verlauf",
        "index_stats_link": "Statistik",
        "index_about_link": "Wie das Orakel funktioniert",
    },
    "en": {
        "strip_forecast": "Rule-based forecast (actually used)",
        "strip_ml": "ML-based forecast — logistic (experimental)",
        "strip_hgb": "ML-based forecast — HistGradientBoosting (black-box model, experimental)",
        "strip_forecast_original": "Previous forecast method",
        "strip_forecast_original_note": "How the day was forecast at the time, before the rules were recalibrated — shown for comparison only.",
        "strip_actual": "Actual measurements (session ≥ 1 h)",
        "strip_legend_go": "session (≥ 1 h ≥ 11 kt)",
        "strip_legend_maybe": "marginal (≥ 1 h ≥ 8 kt)",
        "strip_legend_no_go": "no wind",
        "strip_legend_empty": "no data",
        "strip_legend_storm": "thunderstorm (excluded from calibration)",
        "storm_hint": "⚡ thunderstorm risk — not a thermal day",
        "live_header": "Live at Urfeld",
        "live_now": "now",
        "live_gust_label": "gust",
        "live_water_label": "water",
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
        "ml_title": "🤖 ML Classifier",
        "ml_experimental": "experimental",
        "ml_note": "A second opinion from a learned model running alongside the 14 rules. We collect data to see whether it beats the rule-based approach over time. The three values are its probabilities for GO / MAYBE / NO GO.",
        "ml_prob_go": "GO",
        "ml_prob_maybe": "MAYBE",
        "ml_prob_no_go": "NO GO",
        "for_day": "For",
        "last_30_days": "Last 30 days",
        "no_data": "No data yet — next scheduled forecast runs at 08:00 CET.",
        "no_data_headline": "—",
        "advanced_label": "Advanced — all 14 rules",
        "view_label_original": "as written at the time",
        "col_rule": "Rule",
        "col_signal": "Signal",
        "col_reason": "Reason",
        "stats_label": "Statistics",
        "stats_views_header": "Visitors",
        "stats_unique_visitors": "Real visitors (30 days)",
        "stats_total_hits": "Page views (30 days)",
        "stats_views_note": "Bots and scanners filtered out; IPv6 addresses grouped per household (/64).",
        "stats_forecast_header": "Forecast quality (whole season)",
        "stats_sample_size": "Days scored",
        "stats_accuracy": "Accuracy",
        "stats_accuracy_note": "Share of days where the forecast hit exactly the right bucket (session / marginal / no wind).",
        "stats_ml_header": "ML classifier — logistic (parallel model)",
        "stats_ml_note": "Same day set as above, but forecast = the experimental logistic ML classifier. Never drives the official verdict — comparison only.",
        "stats_hgb_header": "ML classifier — HistGradientBoosting (black-box model, parallel)",
        "stats_hgb_note": "Same day set, but forecast = HistGradientBoosting. The strongest offline model (Peirce +0.142), but a black box with no prod deployment. Comparison only.",
        "stats_baseline": "Naive baseline",
        "stats_baseline_note": "What a blunt \"always the same\" guess (the most common outcome) would score. The forecast has to beat this to be useful.",
        "stats_quarantined_note": "thunderstorm days excluded from scoring",
        "stats_advanced_label": "Advanced statistics",
        "stats_advanced_rule_label": "Advanced statistics — rule layer",
        "stats_advanced_ml_label": "Advanced statistics — ML logistic (parallel model)",
        "stats_advanced_hgb_label": "Advanced statistics — HistGradientBoosting (black-box model)",
        "stats_confusion_rule_header": "Confusion matrix — rule layer",
        "stats_confusion_ml_header": "Confusion matrix — ML logistic (parallel model)",
        "stats_confusion_hgb_header": "Confusion matrix — HistGradientBoosting (black-box model)",
        "stats_confusion_header": "Confusion matrix",
        "stats_confusion_note": "Rows = what was forecast, columns = what actually happened at the lake. The diagonal holds the hits.",
        "stats_axis_forecast": "Forecast",
        "stats_axis_actual": "Actual",
        "stats_binary_note": "Sensitivity and specificity are binary measures. The three buckets are collapsed to “wind day yes/no”: GO and MAYBE count as a wind day, NO GO as calm — for both the forecast and the actual outcome.",
        "stats_sensitivity": "Sensitivity",
        "stats_sensitivity_note": "Share of real wind days (≥ 1 h ≥ 8 kt) where the oracle did not say NO GO — how many good days we catch.",
        "stats_specificity": "Specificity",
        "stats_specificity_note": "Share of calm days correctly forecast as NO GO — how rarely we send you to the lake for nothing.",
        "stats_unavailable": "—",
        "footer_outline": "Thresholds calibrated against ~10 years of Urfeld history (Apr–Oct); some rare-event guardrails are still estimates.",
        "footer_urfeld": "Urfeld wind: © Panoramahotel Karwendelblick, via",
        "footer_dwd": "DWD synoptic via",
        "footer_openmeteo": "Pressure & meteorology via",
        "footer_chat": "Local windsurf community chat (login at",
        "footer_chat_suffix": " required).",
        "nav_today": "Today",
        "nav_history": "History",
        "nav_stats": "Stats",
        "nav_about": "About",
        "history_lead": "30 days of forecast vs. what actually happened at the lake. Click a single day to see its details.",
        "stats_lead": "How well the oracle calls it — and how many read it.",
        "about_lead": "The fourteen rules the oracle uses to turn pressure, weather and live readings into a thermal forecast.",
        "about_thermal_intro": "The Walchensee thermal is driven by slope heating on the south-facing flanks of Herzogstand (1,731 m) and Jochberg (1,565 m). As the slopes warm through the morning, rising air draws cool air from the Kochelsee basin (600 m) to the north — compressed through the saddle between the peaks by the Düseneffekt (funnel effect) into a consistent N to NNE wind. The formula: clear night → strong overnight cooling → unobstructed morning sun on the slopes → thermal wind by midday. The oracle checks 14 conditions that favour or block this mechanism.",
        "about_docs_link": "Detailed background (meteorology, thresholds, calibration) in the technical model document on GitHub.",
        "about_rules_header": "The 14 rules",
        "advanced_label_history": "Advanced — previous forecast method",
        "index_history_link": "30-day history",
        "index_stats_link": "Statistics",
        "index_about_link": "How the oracle works",
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


def _horizon_days(
    today: date, lang: str, selected_iso: str, view: str = "resimulated"
) -> list[dict]:
    """Return today + next 2 days with label, verdict (if logged), selection flag.

    `view` mirrors the main panel's verdict-layer toggle so the tab dots can't
    contradict the headline after a rescore shifts `overall_resimulated`.
    """
    labels = _HORIZON_LABELS.get(lang, _HORIZON_LABELS["en"])
    out: list[dict] = []
    for i, label in enumerate(labels):
        d = today + timedelta(days=i)
        record = _cached_read(d.isoformat())
        if record is None:
            verdict = None
        elif view == "original":
            verdict = record.get("overall")
        else:
            verdict = record.get("overall_resimulated") or record.get("overall")
        out.append({
            "iso": d.isoformat(),
            "label": label,
            "short_date": _fmt_date(d, lang, "short"),
            "verdict": verdict,
            "selected": d.isoformat() == selected_iso,
            "storm": _storm_suspected(record) if record else False,
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


@app.on_event("startup")
async def _startup_prewarm() -> None:
    """Kick off the stats compilation in the background at startup so the
    first visitor never pays the 30–60 s cold GCS walk."""
    asyncio.create_task(_forecast_stats())

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
        "latest_water_temp_c": (
            round(latest.water_temp_c, 1) if latest.water_temp_c is not None else None
        ),
        "latest_at": latest.measured_at.isoformat(),
        "last_hour_avg": round(last_hour_avg, 1),
        "prev_hour_avg": round(prev_hour_avg, 1) if prev_hour_avg is not None else None,
        "trend": trend,
        "chart_svg": _wind_chart_svgs(chart_samples),
    }
    _urfeld_live_at = now
    return _urfeld_live


# Page-views cache — 12 h TTL. One Cloud Logging walk costs a few seconds and
# the number only needs to be roughly current; failures (e.g. local dev
# without ADC, missing logging.viewer) cache None for the full TTL so a
# creds-less box doesn't retry on every request.
_VIEWS_TTL_S = 12 * 3600.0
_views: dict | None = None
_views_at: float = 0.0


def _fetch_page_views_sync() -> dict:
    """Count real-browser traffic over the last 30 days from Cloud Run logs.

    Same classification as scripts/dashboard_traffic.py (shared via
    oracle.traffic). Capped at 20k log entries — beyond that the count
    silently undercounts, which is acceptable for a vanity metric.
    On Cloud Run no configuration is needed: the project comes from ADC and
    `K_SERVICE` is set by the platform; LOG_PROJECT/LOG_SERVICE are dev
    overrides in the spirit of RUNS_BUCKET.
    """
    from google.cloud import logging as gcp_logging  # lazy: [dashboard] extra

    client = gcp_logging.Client(project=os.environ.get("LOG_PROJECT") or None)
    service = os.environ.get("LOG_SERVICE") or os.environ.get("K_SERVICE") or "walchi-oracle-dash"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    flt = (
        f'resource.type="cloud_run_revision" AND '
        f'resource.labels.service_name="{service}" AND '
        f'httpRequest.requestMethod="GET" AND httpRequest.status<400 AND '
        f'timestamp>="{cutoff}"'
    )
    ips: set[str] = set()
    hits = 0
    for entry in client.list_entries(filter_=flt, page_size=1000, max_results=20000):
        req = entry.http_request or {}
        hit = real_browser_hit(
            req.get("userAgent") or "", req.get("requestUrl") or "", req.get("remoteIp") or ""
        )
        if hit is not None:
            hits += 1
            ips.add(hit[0])
    return {"unique_visitors": len(ips), "total_hits": hits}


async def _fetch_page_views() -> dict | None:
    global _views, _views_at
    now = time.time()
    if _views_at and now - _views_at < _VIEWS_TTL_S:
        return _views
    try:
        _views = await asyncio.to_thread(_fetch_page_views_sync)
    except Exception:  # no ADC locally, missing IAM, API hiccup — show "—"
        _views = None
    _views_at = now
    return _views


# Forecast-quality stats cache — 6 h TTL. compile_report(replayed=True) walks
# ~3,300 GCS replay records; against GCS that takes 30–60 s on a cold hit.
# 6 h amortises that cost acceptably; the cache is also pre-warmed at startup
# so the first visitor never pays it. Stale-on-error: a failed refresh keeps
# the last good payload for another TTL.
_STATS_TTL_S = 6 * 3600.0
_stats: dict | None = None
_stats_at: float = 0.0
_stats_computing: bool = False  # guard against concurrent replay walks


def _binary_rates(confusion: dict[str, dict[str, int]]) -> tuple[float | None, float | None]:
    """Sensitivity/specificity under a binary collapse of the 3×3 matrix.

    Positive = the day actually fired (actual go|maybe, i.e. ≥ 1 h above
    8 kt); forecast-positive = forecast go|maybe. None when the denominator
    class is empty (template renders "—").
    """
    pos = (Signal.GO.value, Signal.MAYBE.value)
    neg = Signal.NO_GO.value
    tp = sum(confusion[f][a] for f in pos for a in pos)
    fn = sum(confusion[neg][a] for a in pos)
    tn = confusion[neg][neg]
    fp = sum(confusion[f][neg] for f in pos)
    sens = tp / (tp + fn) if tp + fn else None
    spec = tn / (tn + fp) if tn + fp else None
    return sens, spec


def _stats_payload(report: Report) -> dict:
    """Project a calibration Report into a template-ready dict."""
    sens, spec = _binary_rates(report.confusion)
    matrix = [
        {
            "forecast": f.value,
            "cells": [report.confusion[f.value][a.value] for a in SIGNAL_ORDER],
        }
        for f in SIGNAL_ORDER
    ]
    # Honesty companion to raw accuracy: the best "always the same verdict"
    # guess. Accuracy alone is beatable by a constant on imbalanced classes; we
    # show that constant's score so a visitor can see the forecast beats it.
    baselines = report.baselines() if report.sample_size else {}
    best_class = max(baselines, key=lambda k: baselines[k]["accuracy"]) if baselines else None
    return {
        "n": report.sample_size,
        "accuracy": report.overall_accuracy if report.sample_size else None,
        "baseline_class": best_class,
        "baseline_accuracy": baselines[best_class]["accuracy"] if best_class else None,
        "quarantined": len(report.quarantined_days),
        "matrix": matrix,
        "axis": [s.value for s in SIGNAL_ORDER],
        "sensitivity": sens,
        "specificity": spec,
    }


async def _forecast_stats() -> dict | None:
    """Return the cached stats payload, or None while the replay walk is in
    progress. The walk is expensive (~60 s on GCS); concurrent callers get the
    stale value immediately rather than all blocking on the same walk."""
    global _stats, _stats_at, _stats_computing
    now = time.time()
    if _stats_at and now - _stats_at < _STATS_TTL_S:
        return _stats
    if _stats_computing:
        return _stats  # return stale (or None) — walk already in flight
    _stats_computing = True
    try:
        report = await asyncio.to_thread(
            compile_report, _store(),
            label="duration", resimulated=True, replayed=True,
        )
        _stats = _stats_payload(report)
        _stats["ml"] = _ml_report_payload(report, "ml_classifier", replayed=True)
        _stats["hgb"] = _ml_report_payload(report, "hgb_classifier", replayed=True)
        _stats_at = now
    except Exception:
        _stats_at = now  # suppress retry storm; keep last good payload
    finally:
        _stats_computing = False
    return _stats


def _ml_report_payload(
    report: Report, field: str = "ml_classifier", replayed: bool = False
) -> dict:
    """Score a shadow classifier on the same day set as the rule report.

    `field` selects which record key to read: "ml_classifier" (logistic) or
    "hgb_classifier" (HGB). Both must have the same schema: a dict with a
    "verdict" key. Returns the SAME dict shape as `_stats_payload` so the
    template renders an identical advanced panel for each model.

    In replayed mode the ISOs in `report.days_with_ground_truth` are replay
    record keys, so we read from the replay namespace. If the replay record
    lacks a pre-computed ml_classifier block (common — backfill hasn't run),
    we score the logistic on-the-fly from the stored inputs (pure Python, no
    extra deps). HGB requires sklearn so it stays "—" until hgb-backfill
    --replayed has been run.

    n=0 when no scored day has the block — the template renders "—".
    """
    from oracle.ml_classifier import classify as _classify_logistic

    valid = {s.value for s in SIGNAL_ORDER}
    confusion = _cal_empty_confusion()
    store = _store()
    n = 0
    for iso in report.days_with_ground_truth:
        record = store.read_replay(iso) if replayed else store.read(iso)
        if not record:
            continue
        ml = (record.get(field) or {}).get("verdict")
        # For the logistic classifier in replay mode: score on-the-fly from
        # stored inputs when no pre-computed block is present.
        if ml is None and field == "ml_classifier" and replayed:
            inputs = record.get("inputs") or {}
            result = _classify_logistic(inputs.get("pressure"), inputs.get("meteo"))
            ml = result.verdict if result else None
        if ml not in valid:
            continue
        actual = _cal_label_record(record, report.label_mode)
        if actual is None or actual not in valid:
            continue
        confusion[ml][actual] += 1
        n += 1
    if n == 0:
        return {"n": 0, "accuracy": None, "baseline_class": None,
                "baseline_accuracy": None, "quarantined": len(report.quarantined_days),
                "matrix": [], "axis": [s.value for s in SIGNAL_ORDER],
                "sensitivity": None, "specificity": None}
    sens, spec = _binary_rates(confusion)
    matrix = [
        {"forecast": f.value,
         "cells": [confusion[f.value][a.value] for a in SIGNAL_ORDER]}
        for f in SIGNAL_ORDER
    ]
    baselines = _cal_constant_baselines(confusion)
    best_class = max(baselines, key=lambda k: baselines[k]["accuracy"]) if baselines else None
    hits = sum(confusion[s.value][s.value] for s in SIGNAL_ORDER)
    return {
        "n": n,
        "accuracy": hits / n,
        "baseline_class": best_class,
        "baseline_accuracy": baselines[best_class]["accuracy"] if best_class else None,
        "quarantined": len(report.quarantined_days),
        "matrix": matrix,
        "axis": [s.value for s in SIGNAL_ORDER],
        "sensitivity": sens,
        "specificity": spec,
    }


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
        ml = None
        hgb = None
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
            # Shadow ML verdicts — only present on records after each respective
            # backfill; earlier days show an empty cell.
            ml = (record.get("ml_classifier") or {}).get("verdict")
            hgb = (record.get("hgb_classifier") or {}).get("verdict")
        items.append({
            "iso": d.isoformat(),
            "day": _fmt_date(d, lang, "strip"),
            "verdict": verdict,
            "resimulated": resimulated,
            "ml": ml,
            "hgb": hgb,
            "peak_avg_knots": peak,
            "actual": _actual_verdict_duration(machine),
            "storm": _storm_suspected(record) if record else False,
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
            wt = s.get("water_temp_c")
            out.append(UrfeldSample(
                measured_at=datetime.fromisoformat(s["t"]),
                avg_knots=float(s["avg_kt"]),
                gust_knots=float(s["gust_kt"]),
                water_temp_c=float(wt) if wt is not None else None,
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


def _selected_day(request: Request, today: date) -> date:
    """Parse ?day=YYYY-MM-DD, clamped to [today-30, today+2] so strip cells can
    inspect any logged day in the calibration window. Falls back to today."""
    requested = request.query_params.get("day")
    if requested:
        try:
            parsed = date.fromisoformat(requested)
            if timedelta(days=-30) <= parsed - today <= timedelta(days=2):
                return parsed
        except ValueError:
            pass
    return today


def _selected_view(request: Request) -> str:
    """Verdict layer: ?view=original|resimulated, default resimulated."""
    view = request.query_params.get("view")
    return view if view in ("original", "resimulated") else "resimulated"


def _day_detail_context(selected_day: date, today: date, lang: str, view: str) -> dict:
    """Per-day detail block shared by `/` and `/history`: the verdict card, the
    shadow-ML card, the historical wind chart, and the labels they need.

    Both routes warm `_cache` for `selected_day` before calling this (and for
    the 7-day fallback window `_most_recent` walks), so every read here is a
    cache hit.
    """
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

    # Shadow ML classifier (experimental extra; independent of the view toggle
    # since it's a single learned prediction, not a rescored rule verdict).
    ml_forecast = None
    if raw and raw.get("ml_classifier"):
        _ml = raw["ml_classifier"]
        ml_forecast = {
            "verdict": _ml.get("verdict"),
            "probabilities": _ml.get("probabilities", {}),
            "reason": _ml.get("reason_de" if lang == "de" else "reason_en"),
        }

    summary = _summary_line(display_overall, display_verdicts, lang) if raw else ""
    is_today = selected_day == today
    historical = None if is_today else _historical_chart_payload(raw)
    return {
        "current": _public_view(raw),
        "display_overall": display_overall,
        "display_verdicts": display_verdicts,
        "view": view,
        "summary": summary,
        "ml_forecast": ml_forecast,
        "selected_date_label": _fmt_date(selected_day, lang, "full"),
        "selected_iso": selected_day.isoformat(),
        "historical": historical,
        "is_today": is_today,
    }


def _base_context(request: Request, active: str, lang: str | None = None) -> dict:
    """Context every page shares: lang, UI strings, nav state, GitHub flag.

    The GitHub repo carries the author's real name (commit authors, LICENSE),
    so the source link is suppressed on the pseudonymous host (s1st.de) while
    staying on the real-name face (simon-stieber.de) and in local/dev.
    """
    if lang is None:
        lang = _resolve_lang(request)
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return {
        "lang": lang,
        "t": _UI[lang],
        "active": active,
        "show_github": not host.endswith("s1st.de"),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    lang = _resolve_lang(request)
    today = date.today()
    selected_day = _selected_day(request, today)
    view = _selected_view(request)

    # Warm the cache for every day this request will read (selected + horizon
    # + 7-day fallback for `_most_recent`) in one parallel fan-out, alongside
    # the live-Urfeld fetch. The 30-day strip moved to /history, so its days
    # are no longer prefetched here.
    horizon_isos = [(today + timedelta(days=i)).isoformat() for i in range(3)]
    fallback_isos = [(today - timedelta(days=i)).isoformat() for i in range(8)]
    all_isos = horizon_isos + fallback_isos + [selected_day.isoformat()]
    _, live = await asyncio.gather(
        _prefetch_days(all_isos), _fetch_urfeld_live()
    )

    detail = _day_detail_context(selected_day, today, lang, view)
    horizon = _horizon_days(today, lang, selected_day.isoformat(), view)

    ctx = _base_context(request, active="index", lang=lang)
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            **ctx,
            **detail,
            "today_iso": today.isoformat(),
            "horizon": horizon,
            "live": live,
        },
    )
    q = request.query_params.get("lang")
    if q in _UI:
        response.set_cookie("lang", q, max_age=365 * 24 * 3600, samesite="lax")
    return response


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> Response:
    """30-day forecast-vs-actual strip with the selected day's detail rendered
    inline. Cells link back to /history?day=... so browsing the strip never
    leaves the page — server navigation, no in-place SPA swap."""
    lang = _resolve_lang(request)
    today = date.today()
    selected_day = _selected_day(request, today)
    view = _selected_view(request)

    # Warm the 30-day strip plus the selected day (if it falls outside the
    # strip, e.g. a +2 horizon day) and the 7-day fallback `_most_recent` walks.
    history_isos = [(today - timedelta(days=i)).isoformat() for i in range(30)]
    fallback_isos = [(today - timedelta(days=i)).isoformat() for i in range(8)]
    await _prefetch_days(history_isos + fallback_isos + [selected_day.isoformat()])

    detail = _day_detail_context(selected_day, today, lang, view)
    ctx = _base_context(request, active="history", lang=lang)
    response = templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            **ctx,
            **detail,
            "history": _history(today, lang),
        },
    )
    _set_lang_cookie(request, response)
    return response


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> Response:
    """Forecast-quality + visitor stats. The compile_report walk and the Cloud
    Logging walk are both cached (1 h / 12 h), so a cold hit is slow but repeat
    hits are instant."""
    lang = _resolve_lang(request)
    views, stats = await asyncio.gather(_fetch_page_views(), _forecast_stats())
    ctx = _base_context(request, active="stats", lang=lang)
    response = templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={**ctx, "views": views, "stats": stats},
    )
    _set_lang_cookie(request, response)
    return response


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request) -> Response:
    """Long-form explanation of the 14 rules — the expanded form of the `?`
    tooltips on the (now-removed) advanced panel."""
    lang = _resolve_lang(request)
    ctx = _base_context(request, active="about", lang=lang)
    response = templates.TemplateResponse(
        request=request,
        name="about.html",
        context={
            **ctx,
            "rule_descriptions": _TOOLTIPS_BY_LANG[lang],
            "rule_labels": _LABELS_BY_LANG[lang],
        },
    )
    _set_lang_cookie(request, response)
    return response


def _set_lang_cookie(request: Request, response: Response) -> None:
    """Set the lang cookie when ?lang= was passed, on any route."""
    q = request.query_params.get("lang")
    if q in _UI:
        response.set_cookie("lang", q, max_age=365 * 24 * 3600, samesite="lax")
