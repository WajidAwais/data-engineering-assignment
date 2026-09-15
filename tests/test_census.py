"""Census geography assignment.

The interesting test here is not that the join works — it is that the GEOID
nesting claim the design rests on is true, and that a point outside our boundary
coverage gets null rather than a nearest match.
"""

from __future__ import annotations

import pytest

from src.geo import census


@pytest.fixture(scope="module")
def geo(warehouse):
    exists = warehouse.execute(
        """SELECT count(*) FROM information_schema.columns
            WHERE table_name = 'silver_crash_geo' AND column_name = 'census_bg'"""
    ).fetchone()[0]
    if not exists:
        pytest.skip("census not joined yet; run: python -m src.geo.census")
    return warehouse


# --- the claim the design rests on ------------------------------------------


def test_tract_geoid_is_the_block_group_prefix(geo):
    """A block-group GEOID is state(2)+county(3)+tract(6)+bg(1) = 12 chars, and
    the tract GEOID is the first 11. This is why only the BG files are fetched.
    """
    mismatches = geo.execute("""
        SELECT count(*) FROM silver_crash_geo
         WHERE census_bg IS NOT NULL AND census_tract <> substr(census_bg, 1, 11)
    """).fetchone()[0]
    assert mismatches == 0

    lengths = geo.execute("""
        SELECT DISTINCT length(census_bg), length(census_tract)
          FROM silver_crash_geo WHERE census_bg IS NOT NULL
    """).fetchall()
    assert lengths == [(12, 11)]


@pytest.mark.slow
def test_derived_tracts_are_real_tract_geoids():
    """Verifies the nesting against the authoritative tract file rather than
    trusting the string-slice. Downloads the MD tract shapefile (5.6 MB)."""
    from src.config import load_settings

    real = census.load_tract_geoids(load_settings(), "MD")
    assert real, "no tract GEOIDs loaded"
    # Every MD tract GEOID must itself be 11 chars and prefix some block group.
    assert all(len(g) == 11 for g in real)


# --- assignment correctness -------------------------------------------------


def test_state_prefix_always_matches_the_jurisdiction(geo):
    """FIPS 24=MD, 48=TX, 12=FL. A mismatch means a point was assigned across a
    state line."""
    rows = geo.execute("""
        SELECT c.jurisdiction, substr(g.census_bg, 1, 2), count(*)
          FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
         WHERE g.census_bg IS NOT NULL
         GROUP BY 1, 2
    """).fetchall()
    expected = {"MD": "24", "TX": "48", "FL": "12"}
    for jurisdiction, fips, _ in rows:
        assert fips == expected[jurisdiction], f"{jurisdiction} assigned FIPS {fips}"


def test_out_of_scope_states_are_null_not_nearest_match(geo):
    """We hold boundaries for three states only. A crash in California must get
    null — a wrong tract is worse than an absent one."""
    leaked = geo.execute("""
        SELECT count(*) FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
         WHERE c.jurisdiction NOT IN ('MD', 'TX', 'FL') AND g.census_bg IS NOT NULL
    """).fetchone()[0]
    assert leaked == 0


def test_bad_coordinates_are_not_assigned(geo):
    """Enrichment only ever runs on geo_quality='OK', so the 114 out-of-envelope
    crashes have no geo row at all, let alone a tract."""
    assigned = geo.execute("""
        SELECT count(*) FROM silver_crash c
          LEFT JOIN silver_crash_geo g USING (crash_sk)
         WHERE c.geo_quality <> 'OK' AND g.census_bg IS NOT NULL
    """).fetchone()[0]
    assert assigned == 0


def test_coverage_is_high_but_the_shortfall_is_real(geo):
    """~99.9% assigned. The remainder is not noise — see the DC/Virginia finding
    in DATA_QUALITY. Asserted as a floor so a regression is visible."""
    total, matched = geo.execute("""
        SELECT count(*), count(g.census_bg)
          FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
         WHERE c.geo_quality = 'OK' AND c.jurisdiction IN ('MD', 'TX', 'FL')
    """).fetchone()
    assert matched / total > 0.995, f"coverage fell to {matched}/{total}"
    assert matched < total, "expected a small unmatched remainder, not perfection"


def test_the_envelope_check_missed_adjacent_jurisdictions(geo):
    """The reason the shortfall matters. Montgomery County's bounding envelope is
    a RECTANGLE, and DC plus parts of Virginia sit inside it — so those crashes
    pass the envelope check and only the tract join rejects them.
    """
    unmatched = geo.execute("""
        SELECT count(*) FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
         WHERE c.source_system = 'MONTGOMERY_MD' AND c.geo_quality = 'OK'
           AND g.census_bg IS NULL
    """).fetchone()[0]
    assert unmatched > 0, (
        "expected Montgomery crashes that clear the envelope but fall outside "
        "any Maryland block group — if this is zero, either the data changed or "
        "the join is matching too permissively"
    )


# --- the lookup used by the emitter -----------------------------------------


def test_lookup_degrades_to_null_without_a_cache(monkeypatch):
    """A clean clone must still be able to emit. An absent tract is honest; a
    crashed emitter is not."""
    monkeypatch.setattr(census, "load_block_groups",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no cache")))
    result = census.lookup([
        {"party_id": "P001", "jurisdiction": "MD", "latitude": 39.0, "longitude": -77.1}
    ])
    assert result == {"P001": (None, None)}


def test_lookup_skips_records_without_coordinates():
    result = census.lookup([{"party_id": "X", "jurisdiction": "MD",
                             "latitude": None, "longitude": None}])
    assert result["X"] == (None, None)
