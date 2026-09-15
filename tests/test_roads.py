"""Road snapping, linear referencing, and the rejection threshold.

The rule under test is Part 3b's: "Record the snap distance as a first-class
quality attribute and set a rejection threshold. A crash snapped 400 m to a road
is not enrichment, it is fiction."
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from src.geo import crs, roads


@pytest.fixture(scope="module")
def snapped(warehouse):
    exists = warehouse.execute(
        """SELECT count(*) FROM information_schema.columns
            WHERE table_name = 'silver_crash_geo' AND column_name = 'snap_distance_m'"""
    ).fetchone()[0]
    if not exists:
        pytest.skip("roads not snapped yet; run: python -m src.geo.roads")
    return warehouse


# --- measuring in the right units -------------------------------------------


def test_snapping_uses_a_projected_crs():
    """The 3a guard doing real work: a nearest-neighbour search in degrees
    returns a distance in degrees, and 0.0003 looks a lot like a small number."""
    assert crs.measurement_crs("MD") == crs.MARYLAND
    with pytest.raises(crs.UnsuitableCRS):
        crs.assert_measurable(crs.STORAGE)


def test_snap_distances_are_metres_not_degrees(snapped):
    """A median of ~3 m is plausible; a median of ~0.00003 would mean the
    reprojection never happened."""
    p50 = snapped.execute("""
        SELECT quantile_cont(snap_distance_m, 0.5) FROM silver_crash_geo
         WHERE snap_distance_m IS NOT NULL
    """).fetchone()[0]
    assert 0.5 < p50 < 100, f"median snap distance {p50} is not in metres"


# --- the rejection threshold ------------------------------------------------


def test_exceeding_the_threshold_flags_rather_than_drops(snapped):
    """Consistent with how bad geometry is handled everywhere else here."""
    total, flagged = snapped.execute("""
        SELECT count(*), count(*) FILTER (WHERE snap_quality = 'DISTANCE_EXCEEDED')
          FROM silver_crash_geo WHERE snap_quality IS NOT NULL
    """).fetchone()
    assert flagged > 0, "expected some snaps beyond the threshold"
    assert total > flagged, "everything cannot be rejected"

    # A rejected row keeps its distance — the number is the evidence.
    kept = snapped.execute("""
        SELECT count(*) FROM silver_crash_geo
         WHERE snap_quality = 'DISTANCE_EXCEEDED' AND snap_distance_m IS NULL
    """).fetchone()[0]
    assert kept == 0


def test_the_fiction_case_the_brief_describes_actually_exists(snapped):
    """"A crash snapped 400 m to a road is not enrichment, it is fiction."
    Measured: 47 such crashes, and a worst case of 8.6 km."""
    far, worst = snapped.execute("""
        SELECT count(*) FILTER (WHERE snap_distance_m > 400), max(snap_distance_m)
          FROM silver_crash_geo WHERE snap_distance_m IS NOT NULL
    """).fetchone()
    assert far > 0
    assert worst > 1000, "expected at least one absurd snap in 125k crashes"


def test_the_threshold_sits_beyond_the_99th_percentile(snapped):
    """Chosen from the distribution, not intuition: p99 is ~32 m and p99.9 is
    ~124 m, so 50 m falls in the natural gap between them."""
    p99, p999 = snapped.execute("""
        SELECT quantile_cont(snap_distance_m, 0.99), quantile_cont(snap_distance_m, 0.999)
          FROM silver_crash_geo WHERE snap_distance_m IS NOT NULL
    """).fetchone()
    assert p99 < roads.DEFAULT_MAX_SNAP_M < p999


# --- linear referencing -----------------------------------------------------


def test_offsets_stay_within_their_segment(snapped):
    bad = snapped.execute("""
        SELECT count(*) FROM silver_crash_geo
         WHERE snap_quality = 'OK'
           AND (segment_offset_m < 0 OR segment_offset_m > segment_length_m
                OR segment_fraction NOT BETWEEN 0 AND 1)
    """).fetchone()[0]
    assert bad == 0


def test_line_project_gives_distance_along_not_distance_to():
    """The distinction that makes corridor analysis possible."""
    line = LineString([(0, 0), (100, 0)])
    assert line.project(Point(30, 5)) == 30.0


# --- attributes come from the matched segment -------------------------------


def test_road_class_is_inherited_not_defaulted(snapped):
    classes = {r[0] for r in snapped.execute("""
        SELECT DISTINCT road_class FROM silver_crash_geo WHERE snap_quality = 'OK'
    """).fetchall()}
    assert len(classes) > 3, "a single class would mean a default, not an inheritance"
    assert classes <= roads.DRIVABLE


def test_maxspeed_and_lanes_are_reported_sparse_not_imputed(snapped):
    """The brief: "report the null rate rather than silently imputing". A 0%
    null rate would be the tell that something is filling them in."""
    maxspeed_null, lanes_null, total = snapped.execute("""
        SELECT count(*) FILTER (WHERE maxspeed IS NULL),
               count(*) FILTER (WHERE lanes IS NULL), count(*)
          FROM silver_crash_geo WHERE snap_quality = 'OK'
    """).fetchone()
    assert 0 < maxspeed_null < total
    assert 0 < lanes_null < total


def test_maxspeed_sparsity_tracks_road_importance(snapped):
    """Coverage degrades monotonically with road class — trunk ~14% null,
    service ~99%. This is why imputing from road class would be inventing the
    value precisely where it varies most."""
    rates = dict(snapped.execute("""
        SELECT road_class,
               count(*) FILTER (WHERE maxspeed IS NULL) * 1.0 / count(*)
          FROM silver_crash_geo WHERE snap_quality = 'OK'
         GROUP BY 1 HAVING count(*) > 2000
    """).fetchall())
    assert rates["trunk"] < rates["residential"] < rates["service"]


def test_only_drivable_ways_are_candidates():
    """Snapping a car crash to a footpath yields a plausible distance and a
    wrong road class."""
    assert "footway" not in roads.DRIVABLE
    assert "cycleway" not in roads.DRIVABLE
    assert "motorway" in roads.DRIVABLE


# --- parsing GDAL's HSTORE --------------------------------------------------


def test_other_tags_hstore_is_parsed():
    parsed = roads._parse_other_tags('"maxspeed"=>"35 mph","lanes"=>"2","ref"=>"MD 109"')
    assert parsed == {"maxspeed": "35 mph", "lanes": "2", "ref": "MD 109"}


def test_untagged_ways_do_not_crash_the_parser():
    """GDAL returns NaN, not None, for a way with no extra tags — and
    `not float("nan")` is False, so a falsy check alone lets a float reach the
    regex. That is how this broke on the first run."""
    assert roads._parse_other_tags(float("nan")) == {}
    assert roads._parse_other_tags(None) == {}
    assert roads._parse_other_tags("") == {}


# --- the emitter path -------------------------------------------------------


def test_lookup_degrades_without_the_extract(monkeypatch, tmp_path):
    monkeypatch.setattr(roads, "extract_path", lambda s: tmp_path / "absent.pbf")
    result = roads.lookup([
        {"party_id": "P001", "jurisdiction": "MD", "latitude": 39.0, "longitude": -77.1}
    ])
    assert result == {"P001": {}}


def test_rejected_snaps_withhold_the_road_class():
    """A class inherited from a segment 400 m away is fiction, so it is withheld
    rather than emitted with a caveat nobody downstream reads."""
    from src.compliance import emit

    import inspect
    source = inspect.getsource(emit.build_record)
    assert 'snap_quality") == "OK"' in source
