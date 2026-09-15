"""The silver half of the graded defect tests.

`tests/test_known_defects.py` holds the same four assertions against BRONZE,
where they xfail. These are the twins that run against SILVER, where they pass.
The pair is what actually demonstrates "fails on bronze, passes on silver" —
either half alone proves nothing.
"""

from __future__ import annotations

import pytest

MOCO_LAT = (38.9, 39.36)
MOCO_LON = (-77.54, -76.87)

FARS_LAT_SENTINELS = (77.7777, 88.8888, 99.9999)
FARS_LON_SENTINELS = (777.7777, 888.8888, 999.9999)


# --- the four bronze defects, now fixed -------------------------------------


def test_out_of_envelope_crashes_are_flagged(silver_crash_fact):
    """A1 counterpart. Silver flags rather than drops: a mis-geocoded crash is
    still a crash, and deleting it understates the denominator."""
    con, table = silver_crash_fact

    unflagged = con.execute(
        f"""SELECT count(*) FROM "{table}"
            WHERE source_system = 'MONTGOMERY_MD' AND geo_quality = 'OK'
              AND (latitude NOT BETWEEN ? AND ? OR longitude NOT BETWEEN ? AND ?)""",
        [*MOCO_LAT, *MOCO_LON],
    ).fetchone()[0]
    assert unflagged == 0, "an out-of-envelope crash is still marked geo_quality='OK'"

    flagged = con.execute(
        f"SELECT count(*) FROM \"{table}\" WHERE geo_quality = 'OUT_OF_ENVELOPE'"
    ).fetchone()[0]
    assert flagged > 0, "the defect should be represented, not silently dropped"


def test_substance_abuse_is_normalised(silver_party):
    """A2 counterpart: the comma-joined pair is split into two booleans and
    every spelling of unknown has collapsed to NULL."""
    con, table = silver_party

    bad = con.execute(
        f"""SELECT count(*) FROM "{table}"
            WHERE CAST(alcohol_suspected AS VARCHAR) LIKE '%,%'
               OR CAST(drugs_suspected  AS VARCHAR) LIKE '%,%'"""
    ).fetchone()[0]
    assert bad == 0, "pair encoding survived into the normalised columns"

    # The unknown spellings must not appear as *values*; the raw column is kept
    # deliberately for lineage, so it is excluded from this check.
    leaked = con.execute(
        f"""SELECT count(*) FROM "{table}"
            WHERE lower(CAST(severity_ordinal AS VARCHAR))
                  IN ('n/a', 'unknown', 'none', 'unknown, unknown')"""
    ).fetchone()[0]
    assert leaked == 0


def test_dictionary_cutover_needs_no_cutover_date(silver_party):
    """A3 counterpart. Both encodings are live on 2023-12-28 and 2024-01-03, so
    the proof is that records from *both* days normalise successfully."""
    con, table = silver_party

    rows = con.execute(
        f"""SELECT count(*) FILTER (WHERE substance_abuse_raw LIKE '%,%'),
                   count(*) FILTER (WHERE substance_abuse_raw NOT LIKE '%,%')
              FROM "{table}"
             WHERE alcohol_suspected IS NOT NULL OR drugs_suspected IS NOT NULL"""
    ).fetchone()
    assert rows[0] > 0 and rows[1] > 0, (
        "both encodings must resolve; if only one does, the parser is "
        "scheme-specific rather than shape-dispatched"
    )


def test_no_crash_is_lost_to_an_inner_join(silver_crash_fact):
    """A4 counterpart: every Montgomery crash reaches silver, including the 785
    with no driver row. An inner join would have dropped them."""
    con, table = silver_crash_fact

    bronze = con.execute(
        "SELECT count(DISTINCT report_number) FROM bronze_montgomery_incidents"
    ).fetchone()[0]
    silver = con.execute(
        f"SELECT count(*) FROM \"{table}\" WHERE source_system = 'MONTGOMERY_MD'"
    ).fetchone()[0]
    assert silver == bronze, f"{bronze - silver} crashes lost between bronze and silver"

    # And they carry a severity via the documented fallback rather than NULL.
    orphans_with_severity = con.execute(
        f"""SELECT count(*) FROM "{table}"
            WHERE severity_basis = 'moco_report_type' AND severity_ordinal IS NOT NULL"""
    ).fetchone()[0]
    assert orphans_with_severity > 0


# --- conformance ------------------------------------------------------------


def test_severity_ordinal_is_within_the_output_contract(silver_crash_fact):
    """contracts/lead_output.schema.json: integer 0-5 or null."""
    con, table = silver_crash_fact
    bad = con.execute(
        f'SELECT count(*) FROM "{table}" WHERE severity_ordinal NOT BETWEEN 0 AND 5'
    ).fetchone()[0]
    assert bad == 0


def test_jurisdiction_matches_the_contract_pattern(silver_crash_fact):
    """Pattern ^[A-Z]{2}$ — the compliance gate keys blackout windows on it."""
    con, table = silver_crash_fact
    bad = con.execute(
        f"""SELECT count(*) FROM "{table}"
            WHERE jurisdiction IS NULL OR NOT regexp_matches(jurisdiction, '^[A-Z]{{2}}$')"""
    ).fetchone()[0]
    assert bad == 0


def test_every_crash_carries_a_severity_basis(silver_crash_fact):
    """Part 2 asks where the crosswalk is lossy. Answering per-record beats
    answering in prose."""
    con, table = silver_crash_fact
    bad = con.execute(
        f'SELECT count(*) FROM "{table}" WHERE severity_basis IS NULL'
    ).fetchone()[0]
    assert bad == 0


def test_fars_time_sentinels_did_not_become_real_times(silver_crash_fact):
    """HOUR='99' and MINUTE='99' are sentinels. Loaded naively they throw;
    loaded carelessly they become hour 99."""
    con, table = silver_crash_fact
    bad = con.execute(
        f"""SELECT count(*) FROM "{table}" WHERE source_system = 'NHTSA_FARS'
            AND (date_part('hour', crash_timestamp) > 23
              OR date_part('minute', crash_timestamp) > 59)"""
    ).fetchone()[0]
    assert bad == 0


# --- rebuild determinism ----------------------------------------------------


def test_latest_version_selection_is_deterministic():
    """Silver is current-state over an event-sourced bronze, so the build is
    only reproducible if picking "the latest version" is itself deterministic.

    Bronze holds every version of a record. Ordering by _ingested_at alone is
    not enough — two versions ingested in the same run share a timestamp and
    would then order arbitrarily, so the rebuild would differ run to run. The
    content hash is the tie-break.
    """
    import duckdb

    from src.transform.silver import _latest

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE b ("_source_record_id" VARCHAR, "_ingested_at" VARCHAR,
                        "_content_hash" VARCHAR, payload VARCHAR);
        INSERT INTO b VALUES
            ('r1', '2026-01-01T00:00:00', 'aaa', 'old'),
            ('r1', '2026-02-01T00:00:00', 'bbb', 'new'),
            -- same timestamp, different content: the tie-break case
            ('r2', '2026-01-01T00:00:00', 'ddd', 'tie-b'),
            ('r2', '2026-01-01T00:00:00', 'ccc', 'tie-a');
    """)

    runs = [
        con.execute(f"SELECT payload FROM ({_latest('b')}) ORDER BY 1").fetchall()
        for _ in range(5)
    ]
    assert len({tuple(r) for r in runs}) == 1, "latest-version selection is unstable"

    rows = dict(con.execute(
        f'SELECT "_source_record_id", payload FROM ({_latest("b")})'
    ).fetchall())
    assert rows["r1"] == "new", "did not take the most recent version"
    assert len(rows) == 2, "one row per source record"
    con.close()


def test_crash_dedup_keys_on_the_business_key_not_the_record_id():
    """Montgomery amends a crash by deleting it and re-inserting under a NEW
    Socrata :id. Keyed on :id both versions survive and the crash fact carries
    the crash twice — which is how this was found (grain check, 2026-09-15).
    """
    import duckdb

    from src.transform.silver import _latest

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE b ("_source_record_id" VARCHAR, "_ingested_at" VARCHAR,
                        "_content_hash" VARCHAR, report_number VARCHAR, t VARCHAR);
        INSERT INTO b VALUES
            ('row-aaa', '2026-09-04T00:00:00', 'h1', 'MCP1', '04:47'),
            ('row-zzz', '2026-09-15T00:00:00', 'h2', 'MCP1', '04:34');
    """)

    by_record_id = con.execute(f'SELECT count(*) FROM ({_latest("b")})').fetchone()[0]
    assert by_record_id == 2, "precondition: keying on :id keeps both"

    rows = con.execute(f'SELECT t FROM ({_latest("b", "report_number")})').fetchall()
    assert len(rows) == 1, "one row per crash"
    assert rows[0][0] == "04:34", "must keep the amended version, not the original"
    con.close()


def test_party_dedup_keeps_same_run_person_id_collisions():
    """Two distinct non-motorists in one crash can share a person_id (C4), and
    they arrive in the SAME run. An amendment arrives in a LATER run. Collapsing
    on (report_number, person_id) would delete a real person.
    """
    import duckdb

    from src.transform.silver import _latest_party

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE b ("_source_record_id" VARCHAR, "_ingested_at" VARCHAR,
                        "_content_hash" VARCHAR, report_number VARCHAR,
                        person_id VARCHAR, note VARCHAR);
        INSERT INTO b VALUES
            -- two different people, same person_id, same run: both are real
            ('row-1', '2026-09-04T00:00:00', 'h1', 'C1', 'P1', 'person-a'),
            ('row-2', '2026-09-04T00:00:00', 'h2', 'C1', 'P1', 'person-b'),
            -- one person amended in a later run: only the newer survives
            ('row-3', '2026-09-04T00:00:00', 'h3', 'C2', 'P2', 'stale'),
            ('row-4', '2026-09-15T00:00:00', 'h4', 'C2', 'P2', 'amended');
    """)

    kept = {r[0] for r in con.execute(
        f'SELECT note FROM ({_latest_party("b")})').fetchall()}
    assert kept == {"person-a", "person-b", "amended"}, (
        f"expected both same-run siblings plus the amended row, got {kept}"
    )
    con.close()
