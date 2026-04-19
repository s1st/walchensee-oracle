"""Pillar 2 — cross-Alps pressure pairs.

Two pairs matter at Walchensee:

1. **Alpenpumpe** (Munich − Innsbruck) — the north-minus-south pumping that
   drives the thermal engine. Positive delta = favourable.
2. **Föhn** (Bolzano − Innsbruck) — south-minus-north; a positive delta signals
   Föhn risk, which suppresses the local thermal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class PressureReading:
    station: str
    hpa: float
    measured_at: datetime


@dataclass
class PressureSnapshot:
    alpenpumpe_north: PressureReading  # Munich
    alpenpumpe_south: PressureReading  # Innsbruck (also serves as Föhn north)
    foehn_south: PressureReading       # Bolzano

    @property
    def alpenpumpe_delta_hpa(self) -> float:
        return self.alpenpumpe_north.hpa - self.alpenpumpe_south.hpa

    @property
    def foehn_delta_hpa(self) -> float:
        return self.foehn_south.hpa - self.alpenpumpe_south.hpa


async def fetch_snapshot() -> PressureSnapshot:
    """Backend: Open-Meteo forecast endpoint (free, no key)."""
    raise NotImplementedError("pressure fetcher not yet implemented")
