"""Pillar 1 — windinfo.eu Wind-Wetter-Chat scraper.

The community chat is a WordPress "Wise Chat Pro" plugin behind a login.
Flow:

1. POST /wp-login.php with credentials → capture session cookies.
2. GET /wind-wetter-chat/ → parse the checksum out of the HTML-entity-encoded
   `_wiseChat` config blob.
3. GET /wp-admin/admin-ajax.php?action=wise_chat_messages_endpoint with
   lastId=0 init=1 checksum=<token> → JSON list of recent messages.

Credentials come from the `WINDINFO_USER` and `WINDINFO_PASS` env vars. If
either is missing the fetcher raises — the engine treats that as a degraded
source and continues.
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import httpx

WINDINFO_BASE_URL = "https://www.windinfo.eu"
_LOGIN_URL = f"{WINDINFO_BASE_URL}/wp-login.php"
_CHAT_PAGE_URL = f"{WINDINFO_BASE_URL}/wind-wetter-chat/"
_AJAX_URL = f"{WINDINFO_BASE_URL}/wp-admin/admin-ajax.php"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_CHECKSUM_RE = re.compile(r'"checksum":"([^"]+)"')

# Keywords that mark a message as likely relevant to Walchensee conditions.
# Case-insensitive; lemmatised roughly by taking the stem.
_WALCHENSEE_KEYWORDS = (
    "walchensee", "walchi", "urfeld", "galerie", "nordufer", "sachenbach",
    "wiese", "zwergern", "einsiedl", "kesselberg", "herzogstand", "jochberg",
)


@dataclass
class ChatMessage:
    id: str
    author: str
    text: str
    posted_at: datetime
    channel: str


async def fetch_recent_messages(
    limit: int = 50,
    client: httpx.AsyncClient | None = None,
) -> list[ChatMessage]:
    """Log in to windinfo.eu and pull recent chat messages mentioning Walchensee.

    Returns at most `limit` messages, ordered oldest → newest.
    """
    user = os.environ.get("WINDINFO_USER")
    password = os.environ.get("WINDINFO_PASS")
    if not user or not password:
        raise RuntimeError("WINDINFO_USER / WINDINFO_PASS not set in environment")

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    try:
        await _login(client, user, password)
        checksum = await _fetch_checksum(client)
        payload = await _fetch_messages(client, checksum)
    finally:
        if owns_client:
            await client.aclose()

    return _filter_and_parse(payload, limit)


async def _login(client: httpx.AsyncClient, user: str, password: str) -> None:
    # Prime the test cookie
    await client.get(_LOGIN_URL, headers={"User-Agent": _UA})
    response = await client.post(
        _LOGIN_URL,
        headers={"User-Agent": _UA},
        data={
            "log": user,
            "pwd": password,
            "wp-submit": "Log In",
            "redirect_to": _CHAT_PAGE_URL,
            "testcookie": "1",
        },
    )
    response.raise_for_status()
    if not any(c.name.startswith("wordpress_logged_in") for c in client.cookies.jar):
        raise RuntimeError("windinfo.eu login did not set the logged-in cookie")


async def _fetch_checksum(client: httpx.AsyncClient) -> str:
    page = await client.get(_CHAT_PAGE_URL, headers={"User-Agent": _UA})
    page.raise_for_status()
    # The _wiseChat config is HTML-entity-encoded inside a data-* attribute.
    unescaped = html.unescape(page.text)
    match = _CHECKSUM_RE.search(unescaped)
    if not match:
        raise RuntimeError("Wise Chat checksum not found on the chat page")
    return match.group(1)


async def _fetch_messages(client: httpx.AsyncClient, checksum: str) -> dict:
    response = await client.get(
        _AJAX_URL,
        headers={
            "User-Agent": _UA,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": _CHAT_PAGE_URL,
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        params={
            "action": "wise_chat_messages_endpoint",
            "lastId": "0",
            "fromActionId": "0",
            "lastCheckTime": "",
            "checksum": checksum,
            "init": "1",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"Wise Chat endpoint error: {payload['error']}")
    return payload


def _filter_and_parse(payload: dict, limit: int) -> list[ChatMessage]:
    raw = payload.get("result") or []
    messages: list[ChatMessage] = []
    for entry in raw:
        text = entry.get("text") or ""
        if not _mentions_walchensee(text):
            continue
        messages.append(
            ChatMessage(
                id=str(entry.get("id", "")),
                author=str((entry.get("sender") or {}).get("name") or "?"),
                text=text,
                posted_at=_parse_time(entry),
                channel=str((entry.get("channel") or {}).get("name") or ""),
            )
        )
    messages.sort(key=lambda m: m.posted_at)
    return messages[-limit:]


def _mentions_walchensee(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in _WALCHENSEE_KEYWORDS)


def _parse_time(entry: dict) -> datetime:
    iso = entry.get("timeUTC")
    if iso:
        try:
            return datetime.fromisoformat(str(iso))
        except ValueError:
            pass
    return datetime.now()
