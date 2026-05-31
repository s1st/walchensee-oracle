"""Dashboard unit tests: record projection (`_public_view`, the redaction seam
kept as a guard rail after the chat pillar's removal), sample/chart helpers,
verdict-summary wording, language resolution, and date formatting.
"""
from __future__ import annotations

from datetime import date

from starlette.requests import Request

from oracle.dashboard.main import (
    _fmt_date,
    _historical_chart_payload,
    _public_view,
    _resolve_lang,
    _samples_from_record,
    _summary_line,
)


def test_public_view_returns_none_for_none():
    assert _public_view(None) is None


def test_public_view_preserves_verdict_fields():
    record = {
        "overall": "no_go",
        "verdicts": [{"rule": "x", "signal": "no_go", "reason": "y"}],
    }
    result = _public_view(record)
    assert result["overall"] == "no_go"
    assert result["verdicts"] == record["verdicts"]


def test_public_view_drops_legacy_chat_messages_field():
    """Older logs (pre-chat-removal) still have `chat_messages` on disk —
    the projection drops it so the template never sees stale third-party text."""
    record = {
        "overall": "go",
        "chat_messages": [{"author": "x", "text": "anything"}],
    }
    assert "chat_messages" not in _public_view(record)


def _make_record(*, samples: list[dict] | None) -> dict:
    return {"ground_truth": {"machine": {"samples": samples} if samples is not None else None}}


def test_samples_from_record_parses_iso_timestamps():
    record = _make_record(samples=[
        {"t": "2026-04-22T08:00:00", "avg_kt": 5.0, "gust_kt": 8.0},
        {"t": "2026-04-22T12:00:00", "avg_kt": 14.0, "gust_kt": 19.0},
    ])
    samples = _samples_from_record(record)
    assert len(samples) == 2
    assert samples[0].avg_knots == 5.0
    assert samples[1].gust_knots == 19.0


def test_samples_from_record_handles_missing_block():
    assert _samples_from_record(None) == []
    assert _samples_from_record({}) == []
    assert _samples_from_record(_make_record(samples=None)) == []


def test_samples_from_record_skips_malformed_rows():
    record = _make_record(samples=[
        {"t": "2026-04-22T08:00:00", "avg_kt": 5.0, "gust_kt": 8.0},
        {"t": "not-a-date", "avg_kt": 1.0, "gust_kt": 2.0},
        {"avg_kt": 3.0},  # missing keys
    ])
    samples = _samples_from_record(record)
    assert len(samples) == 1


def test_historical_chart_payload_returns_per_lang_svg():
    record = _make_record(samples=[
        {"t": "2026-04-22T08:00:00", "avg_kt": 5.0, "gust_kt": 8.0},
        {"t": "2026-04-22T12:00:00", "avg_kt": 14.0, "gust_kt": 19.0},
    ])
    payload = _historical_chart_payload(record)
    assert payload["has_data"] is True
    assert set(payload["chart_svg"].keys()) == {"de", "en"}
    assert payload["chart_svg"]["de"].startswith("<svg")


def test_historical_chart_payload_returns_placeholder_when_no_samples():
    """Outage days (no Urfeld backfill) still get a chart slot — keeps layout
    stable when clicking through the strip."""
    payload = _historical_chart_payload(_make_record(samples=[]))
    assert payload["has_data"] is False
    assert payload["chart_svg"]["de"].startswith("<svg")
    assert "keine Daten" in payload["chart_svg"]["de"]
    assert "no data" in payload["chart_svg"]["en"]

    # Single-sample days also count as no-data (can't draw a curve from one point).
    one_sample = _make_record(samples=[
        {"t": "2026-04-22T08:00:00", "avg_kt": 5.0, "gust_kt": 8.0},
    ])
    assert _historical_chart_payload(one_sample)["has_data"] is False
    assert _historical_chart_payload(None)["has_data"] is False


# --- _summary_line --------------------------------------------------------


def _vd(rule, signal, severity="none", reason_en="", reason_de=""):
    return {
        "rule": rule, "signal": signal, "severity": severity,
        "reason_en": reason_en, "reason_de": reason_de, "reason": reason_en,
    }


def test_summary_line_no_go_prefers_hard_blocker_over_soft():
    verdicts = [
        _vd("overnight_cooling", "no_go", "soft", "soft EN", "soft DE"),
        _vd("foehn_override", "no_go", "hard", "Föhn EN", "Föhn DE"),
    ]
    assert _summary_line("no_go", verdicts, "en") == "Föhn EN"
    assert _summary_line("no_go", verdicts, "de") == "Föhn DE"


def test_summary_line_no_go_legacy_record_falls_back_to_any_no_go():
    # Pre-severity logs have no `severity` key — there's no HARD blocker to find,
    # so the summary falls back to whichever rule said NO_GO.
    v = _vd("thermik", "no_go", reason_en="legacy blocker")
    del v["severity"]
    assert _summary_line("no_go", [v], "en") == "legacy blocker"


def test_summary_line_go_counts_green_rules():
    verdicts = [_vd("a", "go"), _vd("b", "go"), _vd("c", "no_go", "soft")]
    assert _summary_line("go", verdicts, "en") == "2 of 3 rules green."
    assert _summary_line("go", verdicts, "de") == "2 von 3 Regeln grün."


def test_summary_line_maybe_prefers_soft_blocker():
    verdicts = [
        _vd("dew_point_spread", "no_go", "soft", "too moist", "zu feucht"),
        _vd("boundary_layer_height", "maybe", reason_en="shallow"),
    ]
    assert _summary_line("maybe", verdicts, "en") == "too moist"
    assert _summary_line("maybe", verdicts, "de") == "zu feucht"


def test_summary_line_maybe_falls_back_to_maybe_then_mixed():
    assert _summary_line("maybe", [_vd("blh", "maybe", reason_en="shallow")], "en") == "shallow"
    # No SOFT NO_GO and no MAYBE rule → generic mixed-signals string.
    assert _summary_line("maybe", [_vd("x", "go")], "en") == "Mixed signals."
    assert _summary_line("maybe", [_vd("x", "go")], "de") == "Gemischte Signale."


# --- _resolve_lang --------------------------------------------------------


def _request(*, query_string: str = "", cookies: dict | None = None,
             accept_language: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if accept_language is not None:
        headers.append((b"accept-language", accept_language.encode()))
    if cookies:
        cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie.encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/",
        "query_string": query_string.encode(), "headers": headers,
    })


def test_resolve_lang_query_param_wins_over_everything():
    req = _request(query_string="lang=en", cookies={"lang": "de"}, accept_language="de-DE")
    assert _resolve_lang(req) == "en"


def test_resolve_lang_cookie_beats_header():
    assert _resolve_lang(_request(cookies={"lang": "en"}, accept_language="de-DE")) == "en"


def test_resolve_lang_header_fallback():
    assert _resolve_lang(_request(accept_language="en-US,en;q=0.9")) == "en"


def test_resolve_lang_defaults_to_de():
    assert _resolve_lang(_request()) == "de"
    assert _resolve_lang(_request(accept_language="fr-FR")) == "de"  # unsupported → default
    assert _resolve_lang(_request(query_string="lang=es")) == "de"   # unknown lang ignored


# --- _fmt_date ------------------------------------------------------------


def test_fmt_date_all_styles_both_languages():
    d = date(2026, 4, 23)  # a Thursday
    assert _fmt_date(d, "de", "short") == "23.4."
    assert _fmt_date(d, "en", "short") == "Apr 23"
    assert _fmt_date(d, "de", "full") == "23.04.2026"
    assert _fmt_date(d, "en", "full") == "Apr 23, 2026"
    assert _fmt_date(d, "de", "strip") == "Do 23.04."
    assert _fmt_date(d, "en", "strip") == "Thu Apr 23"


def test_fmt_date_accepts_iso_string():
    assert _fmt_date("2026-04-23", "de", "short") == "23.4."
