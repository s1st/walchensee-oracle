"""Static configuration: stations, thresholds, endpoints."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    name: str
    lat: float
    lon: float
    provider_id: str | None = None


# Pressure gradient endpoints (North → South drives the thermal)
MUNICH = Station("Munich", 48.1374, 11.5755)
INNSBRUCK = Station("Innsbruck", 47.2692, 11.4041)

# Local wind stations around Walchensee
LOCAL_STATIONS: tuple[Station, ...] = (
    Station("Urfeld", 47.5869, 11.3361),
    Station("Galerie", 47.5783, 11.3225),
    Station("Sachenbach", 47.5950, 11.3600),
)

# Heuristic thresholds — calibrate with real data over time
MIN_PRESSURE_DELTA_HPA = 4.0   # below this, the drive from Munich isn't worth it
IGNITION_WIND_KNOTS = 8.0      # local station reading that signals thermal ignition
SYNOPTIC_OVERRIDE_KNOTS = 20.0 # synoptic wind above this tends to destroy the cell

WINDINFO_CHAT_URL = "https://windinfo.eu/"  # placeholder — refine when scraper is wired
