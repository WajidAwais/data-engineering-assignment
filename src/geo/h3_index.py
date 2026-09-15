"""H3 indexing at resolutions 8 and 9.

r8 ~0.74 km² (~530 m edge) is the working grain; r9 ~0.10 km² (~200 m edge) is
the finest defensible one given the coordinate precision the sources publish.

This wraps the h3 library for one reason worth stating: **h3 is at 4.x and the
v3 API no longer exists.** Code written against `geo_to_h3` / `h3_to_parent`
does not merely warn, it raises AttributeError. Keeping the call sites in one
module means a future version bump is one file, and the test below fails loudly
rather than silently indexing nothing.
"""

from __future__ import annotations

import h3

RES_WORKING = 8
RES_FINE = 9


def cell(lat: float | None, lon: float | None, resolution: int = RES_WORKING) -> str | None:
    if lat is None or lon is None:
        return None
    return h3.latlng_to_cell(lat, lon, resolution)


def parent(cell_id: str | None, resolution: int = RES_WORKING) -> str | None:
    """Roll a fine cell up to a coarser one — r9 -> r8 for multi-resolution work."""
    if cell_id is None:
        return None
    return h3.cell_to_parent(cell_id, resolution)


def neighbours(cell_id: str, k: int = 1) -> list[str]:
    """k-ring around a cell, for neighbourhood smoothing in Phase 3d."""
    return list(h3.grid_disk(cell_id, k))


def area_km2(resolution: int = RES_WORKING) -> float:
    """Average cell area. Used as the denominator where population is not
    available — see DECISIONS on why ACS is deliberately not ingested."""
    return h3.average_hexagon_area(resolution, unit="km^2")
