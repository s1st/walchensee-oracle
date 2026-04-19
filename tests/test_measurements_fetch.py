"""Unit tests for the measurements pillar.

Covers:
- Bright Sky (DWD) fetcher — km/h → knots conversion, fallback-station resolution.
- Addicted-Sports Urfeld scraper — CSRF meta → CsrfToken header flow.
- The aggregator: both succeed, and degraded mode when one source fails.
"""
from __future__ import annotations

import httpx
import pytest

from oracle.config import ADDICTED_SPORTS_BASE_URL, BRIGHT_SKY_CURRENT_URL, StationRole
from oracle.pillars.measurements import WindReading, fetch_latest


_BRIGHT_SKY_PAYLOAD = {
    "weather": {
        "source_id": 332569,
        "timestamp": "2026-04-19T18:00:00+00:00",
        "wind_speed_10": 14.4,        # km/h → ~7.78 kt
        "wind_gust_speed_10": 20.9,
        "wind_direction_10": 160,
        "fallback_source_ids": {"wind_speed_10": 186686},
    },
    "sources": [
        {"id": 332569, "station_name": "Mittenwald-Buckelwie"},
        {"id": 186686, "station_name": "Mittenwald/Obb."},
    ],
}

_URFELD_HTML = """<html><head>
<meta name="csrf-token" content="TESTTOKEN12345">
</head><body>ok</body></html>"""

_URFELD_JSON = {
    "measurment": {
        "417 2026-04-19 16:01:00": {
            "temp": "9.4", "wsavg": "1.08", "wsmax": "1.62",
            "tsdatetime": "2026-04-19 16:01:00",
            "utctstamp": "1776607260",
        },
        "417 2026-04-19 14:58:00": {
            "temp": "11.3", "wsavg": "12.78", "wsmax": "17.28",
            "tsdatetime": "2026-04-19 14:58:00",
            "utctstamp": "1776603480",
        },
    }
}


def _dispatch(bright_sky_handler, urfeld_html_handler, urfeld_json_handler):
    """Build an httpx.MockTransport that routes per-source via URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(BRIGHT_SKY_CURRENT_URL):
            return bright_sky_handler(request)
        if url.startswith(f"{ADDICTED_SPORTS_BASE_URL}/fileadmin/webcam/src/getWeatherData.php"):
            return urfeld_json_handler(request)
        if url.startswith(f"{ADDICTED_SPORTS_BASE_URL}/webcam/walchensee/urfeld/"):
            return urfeld_html_handler(request)
        raise AssertionError(f"unexpected URL in test: {url}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_latest_returns_readings_from_both_sources():
    seen_urfeld_token = {}

    def bright_sky(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BRIGHT_SKY_PAYLOAD)

    def urfeld_html(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_URFELD_HTML)

    def urfeld_json(req: httpx.Request) -> httpx.Response:
        seen_urfeld_token["csrf"] = req.headers.get("CsrfToken") or req.headers.get("csrftoken")
        return httpx.Response(200, json=_URFELD_JSON)

    transport = _dispatch(bright_sky, urfeld_html, urfeld_json)
    async with httpx.AsyncClient(transport=transport) as client:
        readings = await fetch_latest(client=client)

    assert seen_urfeld_token["csrf"] == "TESTTOKEN12345"  # case-sensitive header carried through
    by_role = {r.role: r for r in readings}
    assert StationRole.IGNITION_REFERENCE in by_role
    assert StationRole.SHORE in by_role
    assert by_role[StationRole.IGNITION_REFERENCE].station == "Mittenwald/Obb."
    # Urfeld should pick the latest entry (utctstamp=1776607260).
    urfeld = by_role[StationRole.SHORE]
    assert urfeld.station == "Urfeld"
    assert urfeld.avg_knots == pytest.approx(1.08)
    assert urfeld.gust_knots == pytest.approx(1.62)
    assert urfeld.direction_deg is None


@pytest.mark.asyncio
async def test_fetch_latest_tolerates_urfeld_failure():
    def bright_sky(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BRIGHT_SKY_PAYLOAD)

    def urfeld_html(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>no meta here</html>")

    def urfeld_json(_: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be reached when HTML has no token")

    transport = _dispatch(bright_sky, urfeld_html, urfeld_json)
    async with httpx.AsyncClient(transport=transport) as client:
        readings = await fetch_latest(client=client)

    assert len(readings) == 1
    assert readings[0].role is StationRole.IGNITION_REFERENCE


@pytest.mark.asyncio
async def test_fetch_latest_raises_when_all_sources_fail():
    def bright_sky(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "weather": {
                "source_id": 1,
                "timestamp": "2026-04-19T18:00:00+00:00",
                "wind_speed_10": None,
                "wind_gust_speed_10": None,
                "wind_direction_10": None,
            },
            "sources": [{"id": 1, "station_name": "X"}],
        })

    def urfeld_html(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>no token</html>")

    def urfeld_json(_: httpx.Request) -> httpx.Response:
        raise AssertionError("not expected")

    transport = _dispatch(bright_sky, urfeld_html, urfeld_json)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="All station sources failed"):
            await fetch_latest(client=client)


@pytest.mark.asyncio
async def test_fetch_latest_rejects_urfeld_csrf_error_response():
    def bright_sky(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BRIGHT_SKY_PAYLOAD)

    def urfeld_html(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_URFELD_HTML)

    def urfeld_json(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "No CSRF token.", "result": {"webcams": []}})

    transport = _dispatch(bright_sky, urfeld_html, urfeld_json)
    async with httpx.AsyncClient(transport=transport) as client:
        readings = await fetch_latest(client=client)

    # Bright Sky still returns; Urfeld source is dropped for this run.
    assert len(readings) == 1
    assert readings[0].role is StationRole.IGNITION_REFERENCE
