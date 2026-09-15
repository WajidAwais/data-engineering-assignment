"""The four Montgomery County defects.

Each test must FAIL against the raw bronze layer and PASS against your silver
layer. That is the point: a test that only passes proves nothing about whether
your transform did anything.

Fill in the assertions. The expected values are for you to discover — they are
reported in your DATA_QUALITY.md, and we check them against ground truth.

--- Phase 1a note -------------------------------------------------------------
The assertions below state the invariant the SILVER layer must satisfy, and are
currently evaluated against BRONZE, where they fail on real data. Counts are
asserted as ">= 0 violations expected to be 0", never as fixed magic numbers:
the source is live and grew by 235 rows between the assignment being written and
being started, so hardcoding a count would make these tests rot.

Measured on bronze at 2026-09-04 (see DATA_QUALITY.md):
  envelope violations    114 of 125,005
  substance-abuse values  21 distinct, two encodings, 4 spellings of unknown
  cutover overlap        2023-12-28 .. 2024-01-03 (7 days, both schemes live)
  incidents-not-drivers  785 crashes
"""

import pytest

# Montgomery County's bounding envelope, per the county's own GIS extent.
MOCO_LAT = (38.9, 39.36)
MOCO_LON = (-77.54, -76.87)


@pytest.mark.xfail(reason="must fail on bronze")
def test_coordinates_within_montgomery_envelope(bronze_incidents):
    """bhju-22kf has zero null and zero zero-valued coordinates, and still
    contains records well outside the county. A null check does not catch this.

    Montgomery County envelope is roughly lat 38.9-39.36, lon -77.54 to -76.87.
    """
    con, table = bronze_incidents

    nulls, zeros = con.execute(
        f'''SELECT count(*) FILTER (WHERE latitude IS NULL OR longitude IS NULL),
                   count(*) FILTER (WHERE TRY_CAST(latitude AS DOUBLE) = 0)
            FROM "{table}"'''
    ).fetchone()
    # The premise: the usual checks find nothing, which is why they are not enough.
    assert nulls == 0 and zeros == 0

    outside = con.execute(
        f'''SELECT count(*) FROM "{table}"
            WHERE TRY_CAST(latitude  AS DOUBLE) NOT BETWEEN ? AND ?
               OR TRY_CAST(longitude AS DOUBLE) NOT BETWEEN ? AND ?''',
        [*MOCO_LAT, *MOCO_LON],
    ).fetchone()[0]

    assert outside == 0, (
        f"{outside} crashes fall outside Montgomery County's envelope. Only a "
        "geometric check catches these; a null check passes them straight through."
    )


@pytest.mark.xfail(reason="must fail on bronze")
def test_substance_abuse_dictionary_normalised(bronze_drivers):
    """driver_substance_abuse mixes an old uppercase single-value scheme with a
    newer comma-joined pair scheme, and carries at least three distinct
    spellings of null across the two.
    """
    con, table = bronze_drivers

    values = [
        r[0] for r in con.execute(
            f'SELECT DISTINCT driver_substance_abuse FROM "{table}" '
            f"WHERE driver_substance_abuse IS NOT NULL"
        ).fetchall()
    ]

    # The new scheme encodes (alcohol, drug) as one comma-joined string. A
    # normalised model splits that into separate columns, so no surviving value
    # should still carry the pair delimiter.
    pair_encoded = [v for v in values if "," in v]
    assert not pair_encoded, (
        f"{len(pair_encoded)} values still carry the comma-joined pair encoding, "
        f"e.g. {pair_encoded[:2]}. Note the embedded comma also breaks naive splitting."
    )

    # At least four spellings of 'unknown' coexist: NULL, 'N/A', 'UNKNOWN',
    # 'Unknown, Unknown'. Normalisation must collapse them to exactly one.
    unknown_spellings = [
        v for v in values if v.strip().upper() in {"N/A", "UNKNOWN", "UNKNOWN, UNKNOWN", "OTHER"}
    ]
    assert not unknown_spellings, (
        f"unknown/absent is spelled {len(unknown_spellings) + 1} different ways "
        f"({unknown_spellings} plus SQL NULL); it must reduce to NULL alone."
    )


@pytest.mark.xfail(reason="must fail on bronze")
def test_dictionary_cutover_overlap_handled(bronze_drivers):
    """The two encodings coexist for several days. A hardcoded cutover date is
    wrong. Find the overlap window and prove your handling covers it.
    """
    con, table = bronze_drivers

    overlap = con.execute(
        f'''SELECT CAST(TRY_CAST(crash_date_time AS TIMESTAMP) AS DATE) AS d,
                   count(*) FILTER (WHERE driver_substance_abuse NOT LIKE '%,%') AS old_scheme,
                   count(*) FILTER (WHERE driver_substance_abuse LIKE '%,%')     AS new_scheme
              FROM "{table}"
             WHERE driver_substance_abuse IS NOT NULL
             GROUP BY 1
            HAVING old_scheme > 0 AND new_scheme > 0
             ORDER BY 1'''
    ).fetchall()

    assert not overlap, (
        "both encodings are live on the same crash date on "
        f"{[str(r[0]) for r in overlap]} — so no single cutover date can separate "
        "them. Dispatch on the value's own shape, not on the date."
    )


@pytest.mark.xfail(reason="must fail on bronze")
def test_incidents_drivers_report_number_reconciliation(bronze_incidents, bronze_drivers):
    """The two tables do not agree on the set of report_numbers. An inner join
    silently drops crashes. Quantify with an anti-join in both directions.
    """
    con, incidents = bronze_incidents
    _, drivers = bronze_drivers

    incidents_only = con.execute(
        f'''SELECT count(*) FROM (
                SELECT DISTINCT report_number FROM "{incidents}"
                EXCEPT
                SELECT DISTINCT report_number FROM "{drivers}")'''
    ).fetchone()[0]

    drivers_only = con.execute(
        f'''SELECT count(*) FROM (
                SELECT DISTINCT report_number FROM "{drivers}"
                EXCEPT
                SELECT DISTINCT report_number FROM "{incidents}")'''
    ).fetchone()[0]

    # Direction matters: drivers ⊆ incidents holds, the reverse does not. So the
    # referential-integrity check belongs on drivers→incidents, and the join
    # from incidents must be LEFT or those crashes vanish.
    assert drivers_only == 0, f"{drivers_only} driver rows reference an unknown crash"
    assert incidents_only == 0, (
        f"{incidents_only} crashes have no driver row. An inner join drops them "
        "silently, and they are not a random sample: against a 0.63% baseline, "
        "collision_type='UNKNOWN' orphans at 14.4% and single-vehicle at ~1.4-1.7%."
    )


def test_crash_fact_grain_is_one_row_per_crash(silver_crash_fact):
    """Drivers is one row per driver but carries denormalised crash-level
    attributes. Aggregating off it overcounts multi-vehicle crashes.
    """
    con, table = silver_crash_fact

    rows, by_sk, by_natural = con.execute(
        f'''SELECT count(*), count(DISTINCT crash_sk),
                   count(DISTINCT source_system || ':' || source_record_id)
              FROM "{table}"'''
    ).fetchone()
    assert rows == by_sk == by_natural, (
        f"{rows} rows over {by_natural} crashes — the fact is not at crash grain"
    )

    # The fan-out this guards against: drivers is 1.77x crashes, so a crash fact
    # built off it would overcount multi-vehicle crashes by that factor.
    driver_rows, driver_crashes = con.execute(
        """SELECT count(*), count(DISTINCT report_number)
             FROM bronze_montgomery_drivers"""
    ).fetchone()
    assert driver_rows > driver_crashes, "expected drivers to fan out"

    moco = con.execute(
        f"SELECT count(*) FROM \"{table}\" WHERE source_system = 'MONTGOMERY_MD'"
    ).fetchone()[0]
    assert moco < driver_rows, (
        "crash count matches the driver row count — crash-level attributes are "
        "being sourced from the party table"
    )


def test_fars_sentinel_coordinates_excluded(silver_crash_fact):
    """FARS encodes unknown coordinates as 77.7777 / 88.8888 / 99.9999."""
    con, table = silver_crash_fact

    # Longitude uses a different scale (777.7777 / 888.8888 / 999.9999). A rule
    # written from the latitude set alone leaves every longitude sentinel in place.
    leaked = con.execute(
        f'''SELECT count(*) FROM "{table}"
             WHERE latitude  IN (77.7777, 88.8888, 99.9999)
                OR longitude IN (777.7777, 888.8888, 999.9999)'''
    ).fetchone()[0]
    assert leaked == 0, f"{leaked} sentinel coordinates survived into silver"

    # Excluded from geometry, but the crashes themselves are retained and labelled.
    flagged = con.execute(
        f"SELECT count(*) FROM \"{table}\" WHERE geo_quality = 'SENTINEL'"
    ).fetchone()[0]
    assert flagged > 0, "sentinels should be flagged, not silently dropped"

    null_coords = con.execute(
        f'''SELECT count(*) FROM "{table}"
             WHERE geo_quality = 'SENTINEL'
               AND (latitude IS NOT NULL OR longitude IS NOT NULL)'''
    ).fetchone()[0]
    assert null_coords == 0, "a sentinel-flagged row still carries a usable coordinate"


def test_pipeline_is_idempotent_under_restatement(pipeline_runner):
    """Re-running over a date range that includes an amended TxDOT report must
    produce byte-identical output.
    """
    backfill, con = pipeline_runner

    # A window with no restatements would prove far less. amend_supp_fl marks a
    # supplemental or amended report — 1,150 of TxDOT's 14,535 rows (7.9%).
    start, end = backfill.amended_window(con)
    assert start is not None and end is not None, "no amended TxDOT reports in scope"

    amended = con.execute("""
        SELECT count(*) FROM bronze_txdot_cris_crash WHERE amend_supp_fl = '1'
    """).fetchone()[0]
    assert amended > 0, "the restatement case is not represented in bronze"

    result = backfill.prove(con, "silver_crash", start, end)
    assert result["identical"], (
        f"rebuild diverged over {start}..{end}: "
        f"{result['before'].rows} rows / {result['before'].digest} -> "
        f"{result['after'].rows} rows / {result['after'].digest}"
    )

    # Bronze keeps every version of an amended record; silver must collapse each
    # to exactly one. That is the property a naive re-run would break.
    duplicated = con.execute("""
        SELECT count(*) FROM (
            SELECT source_record_id FROM silver_crash
             WHERE source_system = 'TXDOT_CRIS'
             GROUP BY 1 HAVING count(*) > 1)
    """).fetchone()[0]
    assert duplicated == 0, f"{duplicated} TxDOT crashes appear more than once after rebuild"
