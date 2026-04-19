"""Pillar 1 — windinfo.eu chat scraper.

Extracts recent messages from the live chat so heuristics can mine local expert tips.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ChatMessage:
    author: str
    text: str
    posted_at: datetime


async def fetch_recent_messages(limit: int = 50) -> list[ChatMessage]:
    raise NotImplementedError("windinfo.eu chat scraper not yet implemented")
