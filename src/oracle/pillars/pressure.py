"""Pillar 2 — pressure gradient Munich − Innsbruck (hPa).

A high-pressure zone north of the Alps paired with lower pressure to the south
drives the thermal cell at Walchensee. Positive delta = favourable.
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
class PressureGradient:
    north: PressureReading
    south: PressureReading

    @property
    def delta_hpa(self) -> float:
        return self.north.hpa - self.south.hpa


async def fetch_gradient() -> PressureGradient:
    raise NotImplementedError("pressure fetcher not yet implemented")
