"""Unit tests for the windinfo.eu chat scraper.

Covers:
- Happy path: login → checksum extraction → messages parsed + filtered to Walchensee.
- Unrelated messages are filtered out.
- Missing credentials raise before any network call.
"""
from __future__ import annotations

import httpx
import pytest

from oracle.pillars import chat
from oracle.pillars.chat import fetch_recent_messages


_CHAT_PAGE_HTML = """<html><body>
<div data-wise-chat='{&quot;chatId&quot;:&quot;wc1&quot;,&quot;checksum&quot;:&quot;TESTCHECKSUM&quot;}'></div>
</body></html>"""

_MESSAGES_PAYLOAD = {
    "init": True,
    "nowTime": "2026-04-19T18:00:00+00:00",
    "result": [
        {
            "id": "abc",
            "text": "Heute am Walchensee gut Wind!",
            "channel": {"name": "global"},
            "timeUTC": "2026-04-19T15:30:00+00:00",
            "sender": {"name": "andylucia"},
        },
        {
            "id": "def",
            "text": "Irgendwas am Gardasee los?",
            "channel": {"name": "global"},
            "timeUTC": "2026-04-19T16:00:00+00:00",
            "sender": {"name": "Chriz76"},
        },
        {
            "id": "ghi",
            "text": "Urfeld war heute tot.",
            "channel": {"name": "global"},
            "timeUTC": "2026-04-19T17:00:00+00:00",
            "sender": {"name": "Fred"},
        },
    ],
}


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{chat.WINDINFO_BASE_URL}/wp-login.php"):
            response = httpx.Response(
                200,
                text="ok",
                headers={
                    "Set-Cookie": "wordpress_logged_in_abc=TESTSESSION; Path=/",
                },
            )
            return response
        if url.startswith(f"{chat.WINDINFO_BASE_URL}/wind-wetter-chat/"):
            return httpx.Response(200, text=_CHAT_PAGE_HTML)
        if url.startswith(f"{chat.WINDINFO_BASE_URL}/wp-admin/admin-ajax.php"):
            assert request.url.params["action"] == "wise_chat_messages_endpoint"
            assert request.url.params["checksum"] == "TESTCHECKSUM"
            assert request.url.params["init"] == "1"
            return httpx.Response(200, json=_MESSAGES_PAYLOAD)
        raise AssertionError(f"unexpected URL: {url}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_recent_messages_filters_and_parses(monkeypatch):
    monkeypatch.setenv("WINDINFO_USER", "u")
    monkeypatch.setenv("WINDINFO_PASS", "p")

    async with httpx.AsyncClient(transport=_transport(), follow_redirects=True) as client:
        messages = await fetch_recent_messages(limit=10, client=client)

    assert [m.author for m in messages] == ["andylucia", "Fred"]
    assert "Gardasee" not in [m.text for m in messages][0]  # unrelated is dropped
    assert messages[0].posted_at.year == 2026
    assert messages[0].channel == "global"


@pytest.mark.asyncio
async def test_fetch_recent_messages_raises_when_creds_missing(monkeypatch):
    monkeypatch.delenv("WINDINFO_USER", raising=False)
    monkeypatch.delenv("WINDINFO_PASS", raising=False)
    with pytest.raises(RuntimeError, match="WINDINFO_USER"):
        await fetch_recent_messages()


@pytest.mark.asyncio
async def test_fetch_recent_messages_raises_when_login_cookie_missing(monkeypatch):
    monkeypatch.setenv("WINDINFO_USER", "u")
    monkeypatch.setenv("WINDINFO_PASS", "p")

    def handler(request: httpx.Request) -> httpx.Response:
        # Login endpoint returns 200 but sets no wordpress_logged_in cookie.
        if str(request.url).startswith(f"{chat.WINDINFO_BASE_URL}/wp-login.php"):
            return httpx.Response(200, text="login failed")
        raise AssertionError("should have failed before reaching this URL")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(RuntimeError, match="did not set the logged-in cookie"):
            await fetch_recent_messages(client=client)
