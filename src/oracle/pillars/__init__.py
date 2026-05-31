"""Data pillars + small helpers shared across them."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx


@asynccontextmanager
async def client_scope(
    client: httpx.AsyncClient | None, timeout: float = 10.0
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an httpx client, owning (and closing) a temporary one only when the
    caller didn't pass theirs.

    Centralises the ``owns_client = client is None`` open/close dance that every
    pillar fetcher would otherwise repeat. A caller-supplied client is left open
    for the caller to manage.
    """
    if client is not None:
        yield client
        return
    owned = httpx.AsyncClient(timeout=timeout)
    try:
        yield owned
    finally:
        await owned.aclose()
