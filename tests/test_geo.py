"""Geospatial correctness — CRS discipline, timezone derivation, H3.

The timezone tests are compliance tests as much as geospatial ones: the Part 5c
calling window is computed from this column, so a wrong zone is a call placed
outside the legal hours.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.geo import crs, h3_index
from src.geo.timezone import (
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_GAP,
    RESOLUTION_OK,
    TimezoneResolver,
)


@pytest.fixture(scope="module")
def resolver():
    return TimezoneResolver()


# --- CRS discipline ---------------------------------------------------------


@pytest.mark.parametrize("epsg", [crs.STORAGE, crs.WEB_MERCATOR])
def test_cannot_measure_in_a_non_measuring_crs(epsg):
    """Part 3a scores a section zero for a distance computed in Web Mercator,
    and the error is invisible in the output — a 500 m buffer near Baltimore
    measures ~387 m on the ground. So this raises rather than warns."""
    with pytest.raises(crs.UnsuitableCRS):
        crs.assert_measurable(epsg)


def test_measurement_crs_is_projected_per_jurisdiction():
    assert crs.measurement_crs("MD") == crs.MARYLAND
    assert crs.measurement_crs("TX") == crs.TEXAS_ALBERS
    assert crs.measurement_crs("FL") == crs.FLORIDA_ALBERS


def test_out_of_scope_states_fall_back_to_equal_area():
    """FARS is national; most rows are in states with no configured zone. An
    equal-area continental projection is the honest default."""
    assert crs.measurement_crs("CA") == crs.TRISTATE
    assert crs.measurement_crs(None) == crs.TRISTATE


def test_cross_jurisdiction_comparison_uses_one_equal_area_crs():
    """A per-km² rate computed in Maryland state-plane is not comparable with
    one computed in Texas Albers — different definitions of a square km."""
    assert crs.comparison_crs() == crs.TRISTATE
    crs.assert_measurable(crs.comparison_crs())


# --- timezone from coordinates, not from state ------------------------------


@pytest.mark.parametrize("place,lat,lon,expected", [
    ("Montgomery County MD", 39.15, -77.14, "America/New_York"),
    ("Dallas TX", 32.78, -96.80, "America/Chicago"),
    ("El Paso TX", 31.76, -106.49, "America/Denver"),
    ("Miami FL", 25.77, -80.19, "America/New_York"),
    ("Pensacola FL", 30.42, -87.22, "America/Chicago"),
])
def test_zone_is_derived_from_coordinates(resolver, place, lat, lon, expected):
    assert resolver.zone_for(lat, lon) == expected, place


def test_a_state_rule_would_be_wrong_for_texas_and_florida(resolver):
    """Texas spans Central and Mountain; Florida spans Eastern and Central.
    Measured in our own data: 819 TX crashes resolve to Mountain and 1,211 FL
    crashes to Central. A state lookup gets every one of them wrong."""
    assert resolver.zone_for(31.76, -106.49) != resolver.zone_for(32.78, -96.80)
    assert resolver.zone_for(30.42, -87.22) != resolver.zone_for(25.77, -80.19)


def test_arizona_does_not_observe_dst(resolver):
    """A zone lookup that collapsed to UTC offsets would lose this."""
    assert resolver.zone_for(33.45, -112.07) == "America/Phoenix"


def test_no_zone_for_a_missing_coordinate(resolver):
    assert resolver.zone_for(None, None) is None


# --- DST edges --------------------------------------------------------------


def test_spring_forward_gap_is_flagged(resolver):
    """02:30 on 2024-03-10 never existed in America/New_York. Found one real
    case: FARS 2023:120545 recorded at 2023-03-12 02:00:00 in Florida."""
    result = resolver.localise(datetime(2024, 3, 10, 2, 30), "America/New_York")
    assert result.resolution == RESOLUTION_GAP
    assert result.utc is not None, "flagged, but still resolved — not dropped"


def test_fall_back_ambiguity_is_flagged(resolver):
    """01:30 on 2024-11-03 happened twice, with two different UTC offsets."""
    result = resolver.localise(datetime(2024, 11, 3, 1, 30), "America/New_York")
    assert result.resolution == RESOLUTION_AMBIGUOUS


def test_gap_is_not_misreported_as_ambiguous(resolver):
    """`fold` changes the UTC offset for BOTH edge cases, so testing ambiguity
    first classifies every spring-forward gap as ambiguous. Order matters."""
    gap = resolver.localise(datetime(2024, 3, 10, 2, 30), "America/New_York")
    ambiguous = resolver.localise(datetime(2024, 11, 3, 1, 30), "America/New_York")
    assert gap.resolution != ambiguous.resolution


@pytest.mark.parametrize("dt", [
    datetime(2024, 3, 10, 1, 59),   # one minute before the gap
    datetime(2024, 3, 10, 3, 1),    # one minute after it
    datetime(2024, 6, 15, 12, 0),   # nowhere near a transition
])
def test_times_around_a_transition_are_not_over_flagged(resolver, dt):
    assert resolver.localise(dt, "America/New_York").resolution == RESOLUTION_OK


def test_localise_is_localisation_not_conversion(resolver):
    """The feeds publish naive local wall-clock. Attaching UTC directly would
    shift every Montgomery crash by 4-5 hours."""
    naive = datetime(2024, 6, 15, 12, 0)
    result = resolver.localise(naive, "America/New_York")
    assert result.utc.hour == 16, "June in New York is UTC-4"


def test_same_wall_clock_differs_by_zone(resolver):
    """Two crashes at the same local time in different zones are different
    instants — which is the whole reason the calling window is per-location."""
    naive = datetime(2024, 6, 15, 12, 0)
    east = resolver.localise(naive, "America/New_York").utc
    mountain = resolver.localise(naive, "America/Denver").utc
    assert mountain - east == (mountain - east).__class__(hours=2)


# --- H3 ---------------------------------------------------------------------


def test_h3_uses_the_v4_api():
    """h3 is at 4.x; v3's geo_to_h3 no longer exists and raises rather than
    warning. Keeping call sites in one module makes a bump one file."""
    import h3

    assert hasattr(h3, "latlng_to_cell")
    assert not hasattr(h3, "geo_to_h3"), "v3 API present — pin assumptions are stale"


def test_r9_rolls_up_to_r8():
    lat, lon = 39.15, -77.14
    r8 = h3_index.cell(lat, lon, h3_index.RES_WORKING)
    r9 = h3_index.cell(lat, lon, h3_index.RES_FINE)
    assert r8 != r9
    assert h3_index.parent(r9, h3_index.RES_WORKING) == r8


def test_h3_areas_match_the_documented_grain():
    """r8 ~0.74 km², r9 ~0.10 km² — the figures the brief quotes."""
    assert 0.70 < h3_index.area_km2(8) < 0.78
    assert 0.09 < h3_index.area_km2(9) < 0.12


def test_h3_handles_missing_coordinates():
    assert h3_index.cell(None, None) is None
    assert h3_index.parent(None) is None


def test_neighbours_include_the_centre_cell():
    c = h3_index.cell(39.15, -77.14)
    ring = h3_index.neighbours(c, 1)
    assert c in ring and len(ring) == 7, "k=1 grid_disk is the cell plus 6 neighbours"


# --- enrichment against the warehouse ---------------------------------------


def test_every_geocodable_crash_is_enriched(silver_geo):
    """Coverage in both directions: no crash left behind, no orphan left over.
    Checked by hand once during Phase 3a; this makes it repeatable."""
    con, table = silver_geo

    missing = con.execute(f"""
        SELECT count(*) FROM silver_crash c
         WHERE c.geo_quality = 'OK'
           AND NOT EXISTS (SELECT 1 FROM "{table}" g WHERE g.crash_sk = c.crash_sk)
    """).fetchone()[0]
    assert missing == 0, f"{missing} geocodable crashes have no enrichment row"

    orphans = con.execute(f"""
        SELECT count(*) FROM "{table}" g
         WHERE NOT EXISTS (SELECT 1 FROM silver_crash c WHERE c.crash_sk = g.crash_sk)
    """).fetchone()[0]
    assert orphans == 0, f"{orphans} enrichment rows reference an unknown crash"

    rows, keys = con.execute(f'SELECT count(*), count(DISTINCT crash_sk) FROM "{table}"').fetchone()
    assert rows == keys, "enrichment is not one row per crash"


def test_bad_coordinates_are_not_enriched(silver_geo):
    """Deriving a timezone from a sentinel would manufacture a calling window
    for a location we do not know."""
    con, table = silver_geo
    leaked = con.execute(f"""
        SELECT count(*) FROM "{table}" g JOIN silver_crash c USING (crash_sk)
         WHERE c.geo_quality <> 'OK'
    """).fetchone()[0]
    assert leaked == 0


def test_every_enriched_crash_has_a_zone(silver_geo):
    con, table = silver_geo
    assert con.execute(
        f'SELECT count(*) FROM "{table}" WHERE iana_timezone IS NULL'
    ).fetchone()[0] == 0


def test_unknown_source_times_do_not_become_midnight(silver_geo):
    """FARS HOUR='99' rows fall back to a date-only timestamp, which lands at
    midnight. Without the time_known flag ~2,000 of them read as genuine 00:00
    crashes and put a fake spike in every time-of-day distribution.
    """
    con, table = silver_geo

    fake_midnight = con.execute(f"""
        SELECT count(*) FROM "{table}"
         WHERE time_known = false AND local_hour IS NOT NULL
    """).fetchone()[0]
    assert fake_midnight == 0, "a row with an unknown source time still reports an hour"

    # The date survives a sentinel time, so day-of-week must still be populated.
    lost_dow = con.execute(f"""
        SELECT count(*) FROM "{table}" WHERE time_known = false AND local_dow IS NULL
    """).fetchone()[0]
    assert lost_dow == 0, "day of week should survive an unknown time"


def test_timezone_traps_are_present_in_the_real_data(silver_geo):
    """Not just reachable in theory — TX spans Central/Mountain and FL spans
    Eastern/Central in the data we actually hold."""
    con, table = silver_geo
    tx, fl = con.execute(f"""
        SELECT count(DISTINCT CASE WHEN c.jurisdiction='TX' THEN g.iana_timezone END),
               count(DISTINCT CASE WHEN c.jurisdiction='FL' THEN g.iana_timezone END)
          FROM "{table}" g JOIN silver_crash c USING (crash_sk)
    """).fetchone()
    assert tx >= 2, "Texas should span more than one zone"
    assert fl >= 2, "Florida should span more than one zone"


def test_resolution_is_deterministic(resolver):
    """Enrichment is a pure function of (lat, lon, naive timestamp), which is
    what makes a rebuild reproducible without diffing the whole table."""
    args = (31.76, -106.49, datetime(2024, 3, 10, 2, 30))
    first = resolver.resolve(*args)
    assert all(resolver.resolve(*args) == first for _ in range(5))
