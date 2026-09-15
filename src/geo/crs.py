"""Coordinate reference systems, and a guard against measuring in the wrong one.

Canonical storage is EPSG:4326. Every distance, length, area or buffer operation
reprojects first — and the function that hands out a CRS for measuring will
refuse to hand back a geographic or Web Mercator one.

Why the guard exists rather than a comment: Part 3a of the brief scores any
section zero if a measurement is performed in EPSG:3857. Scale error there is
1/cos(lat) — a 500 m buffer drawn in Web Mercator near Baltimore is about 387 m
on the ground, and nothing about the output looks wrong.

State the CRS per operation, in code, with the reason. That is what this module
is for.
"""

from __future__ import annotations

# Storage. Geographic, degrees — correct for interchange, never for measurement.
STORAGE = 4326

# Tri-state and national work. NAD83 / CONUS Albers, equal-area, so per-km²
# rates across several states are comparable rather than distorted by latitude.
TRISTATE = 5070

# State-local work, where a projected zone beats a continental projection for
# distance accuracy.
MARYLAND = 26985        # NAD83 / Maryland (single zone)
TEXAS_CENTRAL = 32139   # NAD83 / Texas Central
TEXAS_NORTH = 32137
TEXAS_NORTH_CENTRAL = 32138
TEXAS_SOUTH_CENTRAL = 32140
TEXAS_SOUTH = 32141
TEXAS_ALBERS = 3083     # statewide single-CRS alternative
FLORIDA_EAST = 26958
FLORIDA_WEST = 26959
FLORIDA_NORTH = 26960
FLORIDA_ALBERS = 3086   # statewide single-CRS alternative

# Rendering tiles. Listed so it is named and refused, not so it is used.
WEB_MERCATOR = 3857

# Statewide measurement CRS per jurisdiction. Albers variants for TX and FL
# because both span multiple state-plane zones and a single zone would distort
# the far side of the state.
_BY_JURISDICTION = {
    "MD": MARYLAND,
    "TX": TEXAS_ALBERS,
    "FL": FLORIDA_ALBERS,
}

_NEVER_MEASURE_IN = {
    STORAGE: "EPSG:4326 is geographic — degrees are not a unit of length",
    WEB_MERCATOR: (
        "EPSG:3857 is for rendering tiles only. Scale error is 1/cos(lat): a "
        "500 m buffer near Baltimore measures ~387 m on the ground"
    ),
}


class UnsuitableCRS(ValueError):
    """Raised when a CRS that cannot measure is used for measuring."""


def assert_measurable(epsg: int) -> int:
    """Gate every distance/area/buffer call on this."""
    if epsg in _NEVER_MEASURE_IN:
        raise UnsuitableCRS(f"refusing to measure in EPSG:{epsg} — {_NEVER_MEASURE_IN[epsg]}")
    return epsg


def measurement_crs(jurisdiction: str | None = None) -> int:
    """Projected CRS for measuring within one jurisdiction.

    Falls back to CONUS Albers for anything outside MD/TX/FL — FARS is national,
    so most rows are in states with no state-plane zone configured here, and an
    equal-area continental projection is the honest default for them.
    """
    return assert_measurable(_BY_JURISDICTION.get((jurisdiction or "").upper(), TRISTATE))


def comparison_crs() -> int:
    """CRS for rates compared ACROSS jurisdictions.

    Equal-area, single projection. Comparing a per-km² rate computed in Maryland
    state-plane against one computed in Texas Albers would be comparing two
    different definitions of a square kilometre.
    """
    return assert_measurable(TRISTATE)
