"""Static configuration: stations, thresholds, endpoints.

Thresholds are placeholders informed by research (Garda analogues + local kiter
heuristics) and MUST be calibrated against logged Walchensee observations before
they can be trusted operationally. Every threshold is marked TODO(calibrate).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import Enum


class StationRole(str, Enum):
    ALPENPUMPE_NORTH = "alpenpumpe_north"  # north-of-Alps pressure anchor
    ALPENPUMPE_SOUTH = "alpenpumpe_south"  # south-of-Alps pressure anchor
    FOEHN_NORTH = "foehn_north"            # north side of the Föhn pressure pair
    FOEHN_SOUTH = "foehn_south"            # south side of the Föhn pressure pair
    IGNITION_REFERENCE = "ignition_reference"  # first station to show the thermal
    RIDGE = "ridge"                        # summit, used to detect Föhn aloft
    SHORE = "shore"                        # on-lake station (Urfeld/Galerie/…)


@dataclass(frozen=True)
class Station:
    name: str
    lat: float
    lon: float
    role: StationRole
    provider_id: str | None = None


# --- Pressure-pair anchors ------------------------------------------------
# Alpenpumpe: north minus south drives the cross-Alps thermal engine.
MUNICH = Station("Munich", 48.1374, 11.5755, StationRole.ALPENPUMPE_NORTH)
INNSBRUCK_N = Station("Innsbruck", 47.2692, 11.4041, StationRole.ALPENPUMPE_SOUTH)

# Föhn pair: Bolzano (south) minus Innsbruck (north) positive = Föhn risk.
# Innsbruck appears in both pairs — south for Alpenpumpe, north for Föhn.
INNSBRUCK_F = Station("Innsbruck", 47.2692, 11.4041, StationRole.FOEHN_NORTH)
BOLZANO = Station("Bolzano", 46.4983, 11.3548, StationRole.FOEHN_SOUTH)

# --- Local wind stations around Walchensee --------------------------------
# Krün is ~10 km west of the lake and is the station locals watch for the
# first ignition gust. Herzogstand summit (~1,531 m) is the Föhn/ridge check.
# The three shore stations cover the N→S ignition-fan propagation.
KRUEN = Station("Krün", 47.5772, 11.2622, StationRole.IGNITION_REFERENCE)
HERZOGSTAND = Station("Herzogstand", 47.5839, 11.3081, StationRole.RIDGE)
URFELD = Station("Urfeld", 47.5869, 11.3361, StationRole.SHORE)
GALERIE = Station("Galerie", 47.5783, 11.3225, StationRole.SHORE)
SACHENBACH = Station("Sachenbach", 47.5950, 11.3600, StationRole.SHORE)

# Shore stations in ignition-propagation order (N → S); the thermal typically
# lights up at index 0 first and reaches the last entry ~2 hours later.
SHORE_PROPAGATION: tuple[Station, ...] = (URFELD, GALERIE, SACHENBACH)

ALL_LOCAL_STATIONS: tuple[Station, ...] = (
    KRUEN,
    HERZOGSTAND,
    URFELD,
    GALERIE,
    SACHENBACH,
)

# --- Heuristic thresholds -------------------------------------------------
# TODO(calibrate): all values below are informed guesses from research; replace
# once we have a log of (inputs, actual-conditions) pairs from real sessions.

MIN_ALPENPUMPE_DELTA_HPA = 2.5   # Munich − Innsbruck; Garda uses ~3, Walchensee smaller
FOEHN_TRIGGER_DELTA_HPA = 4.0    # Bolzano − Innsbruck positive ≥ this => Föhn risk
SYNOPTIC_OVERRIDE_KNOTS = 15.0   # ≥ 3 Bft base wind deforms the thermal cell
IGNITION_WIND_KNOTS = 8.0        # shore reading that signals ignition
MAX_OVERNIGHT_CLOUD_COVER_PCT = 30.0  # 22:00→06:00 average; above this, weak inversion
MIN_MORNING_SOLAR_WM2 = 600.0    # max hourly shortwave radiation 09:00–13:00

# Classic Urfeld ignition window 10:30–11:30; propagation done by ~15:00.
IGNITION_WINDOW_LOCAL: tuple[time, time] = (time(10, 30), time(15, 0))

# --- External endpoints ---------------------------------------------------
WINDINFO_CHAT_URL = "https://windinfo.eu/"  # TODO(scraper): refine when wired
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
BRIGHT_SKY_CURRENT_URL = "https://api.brightsky.dev/current_weather"
ADDICTED_SPORTS_BASE_URL = "https://www.addicted-sports.com"
