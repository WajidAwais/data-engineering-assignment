"""IANA timezone derived from coordinates, and honest DST handling.

This is a compliance control, not an analytical nicety. 16 C.F.R. §310.4(c) and
47 C.F.R. §64.1200(c)(1) restrict outbound calls to 8:00-21:00 **local time at
the called party's location**, so the calling window in Part 5c is computed from
this column. A pipeline that derives it from the state, or from NPA-NXX, fails
that section outright.

Why coordinates and not state — measured in our own data:

    817 Texas crashes west of -105 deg   -> America/Denver, not America/Chicago
  1,218 Florida crashes west of -85 deg  -> America/Chicago, not America/New_York

A state rule gets 2,035 records wrong, each of them a potential call placed an
hour outside the legal window.

The source feeds publish naive local wall-clock with no offset, so this
*localises*, it does not convert. Two DST edge cases are flagged rather than
silently resolved:

  DST_GAP        the wall-clock time never existed (spring forward)
  DST_AMBIGUOUS  the wall-clock time happened twice (fall back)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from timezonefinder import TimezoneFinder

log = logging.getLogger(__name__)

RESOLUTION_OK = "OK"
RESOLUTION_GAP = "DST_GAP"
RESOLUTION_AMBIGUOUS = "DST_AMBIGUOUS"
RESOLUTION_NO_ZONE = "NO_ZONE"


@dataclass(frozen=True)
class Localised:
    utc: datetime | None
    resolution: str


class TimezoneResolver:
    """Coordinate -> IANA zone, with a cache.

    `timezone_at` is the point-in-polygon lookup; it returns None over open
    water, which is a real outcome for a bad coordinate rather than an error.
    """

    def __init__(self) -> None:
        self._tf = TimezoneFinder(in_memory=True)
        self._cache: dict[tuple[float, float], str | None] = {}
        self._zones: dict[str, ZoneInfo] = {}

    def zone_for(self, lat: float | None, lon: float | None) -> str | None:
        if lat is None or lon is None:
            return None
        key = (lat, lon)
        if key not in self._cache:
            self._cache[key] = self._tf.timezone_at(lat=lat, lng=lon)
        return self._cache[key]

    def _zoneinfo(self, name: str) -> ZoneInfo:
        if name not in self._zones:
            self._zones[name] = ZoneInfo(name)
        return self._zones[name]

    def localise(self, naive: datetime | None, zone_name: str | None) -> Localised:
        """Attach the zone to a naive wall-clock time and convert to UTC."""
        if naive is None or zone_name is None:
            return Localised(None, RESOLUTION_NO_ZONE)

        tz = self._zoneinfo(zone_name)
        aware = naive.replace(tzinfo=tz)

        # Order matters. `fold` changes the UTC offset for BOTH edge cases, so
        # an ambiguity test alone reports spring-forward gaps as ambiguous.
        # Only the gap fails to round-trip, so test that first.
        #
        # Spring forward: the wall-clock time never existed. 02:30 on a US
        # transition day maps to 07:30Z, which comes back as 03:30 local.
        round_tripped = aware.astimezone(dt_timezone.utc).astimezone(tz).replace(tzinfo=None)
        if round_tripped != naive:
            return Localised(aware.astimezone(dt_timezone.utc), RESOLUTION_GAP)

        # Fall back: the hour occurred twice with two different offsets, and
        # both are real. fold=0 is the first (still on DST) occurrence.
        if aware.replace(fold=0).utcoffset() != aware.replace(fold=1).utcoffset():
            return Localised(aware.replace(fold=0).astimezone(dt_timezone.utc),
                             RESOLUTION_AMBIGUOUS)

        return Localised(aware.astimezone(dt_timezone.utc), RESOLUTION_OK)

    def resolve(
        self, lat: float | None, lon: float | None, naive: datetime | None
    ) -> tuple[str | None, Localised]:
        zone = self.zone_for(lat, lon)
        return zone, self.localise(naive, zone)
