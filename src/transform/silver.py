"""Bronze -> silver.

Two tables, built as a pure function of bronze:

  silver_crash   one row per crash, all three sources conformed
  silver_party   driver + non-motorist at person grain (Montgomery only)

Build is a full CREATE OR REPLACE, not an incremental merge. At ~590k rows that
takes seconds, and it means silver is reproducible from bronze alone — which is
what makes the idempotency claim checkable rather than asserted.

SCD strategy: bronze is event-sourced (every version of every record retained,
keyed by content hash), and silver is current-state over it — the latest version
per source record. Part 2 names event-sourcing as an acceptable strategy, and
this costs no extra machinery: history already exists one layer down.

    python -m src.transform.silver
"""

from __future__ import annotations

import argparse
import logging
import sys

import duckdb

from src.config import load_settings
from src.ingest import watermark

from . import normalize, severity

log = logging.getLogger("transform.silver")

# Montgomery County's bounding envelope (county GIS extent).
MOCO_LAT = (38.9, 39.36)
MOCO_LON = (-77.54, -76.87)

# FARS unknown-coordinate sentinels. Latitude and longitude use DIFFERENT
# scales — a rule written for latitude alone leaves every longitude sentinel in
# place. Compared numerically, never as strings: the stored precision changes
# between years ('77.7777' in 2019, '77.77770000' after), so string equality
# catches 68 of 873 and looks like it worked.
FARS_LAT_SENTINELS = (77.7777, 88.8888, 99.9999)
FARS_LON_SENTINELS = (777.7777, 888.8888, 999.9999)

# FARS stores state as a FIPS code; the output contract wants a 2-letter
# jurisdiction. Needed downstream because the compliance blackout windows in
# Phase 5 are keyed on jurisdiction.
FIPS_TO_USPS = {
    "1": "AL", "2": "AK", "4": "AZ", "5": "AR", "6": "CA", "8": "CO", "9": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID",
    "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA",
    "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ",
    "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK",
    "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN",
    "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY", "72": "PR", "78": "VI",
}


def _fips_to_usps(value: str | None) -> str | None:
    if value is None:
        return None
    return FIPS_TO_USPS.get(value.strip().split(".")[0])


def register_udfs(con: duckdb.DuckDBPyConnection) -> None:
    """Expose the pure normalisers to SQL so the transform stays set-based."""
    fns = [
        ("norm_injury_severity", normalize.injury_severity, ["VARCHAR"], "INTEGER"),
        ("norm_collision_type", normalize.collision_type, ["VARCHAR"], "VARCHAR"),
        ("norm_alcohol", lambda v: normalize.substance_abuse(v)[0], ["VARCHAR"], "BOOLEAN"),
        ("norm_drugs", lambda v: normalize.substance_abuse(v)[1], ["VARCHAR"], "BOOLEAN"),
        ("sev_from_report_type", severity.from_report_type, ["VARCHAR"], "INTEGER"),
        ("sev_from_crash_sev_id", severity.from_crash_sev_id, ["VARCHAR"], "INTEGER"),
        ("fips_to_usps", _fips_to_usps, ["VARCHAR"], "VARCHAR"),
    ]
    for name, fn, args, ret in fns:
        try:
            con.remove_function(name)
        except Exception:
            pass
        # 'special' because these legitimately map non-NULL input to NULL —
        # 'N/A' and 'UNKNOWN' are values that mean absent. Under DEFAULT,
        # DuckDB filters NULL input and forbids NULL output.
        con.create_function(name, fn, args, ret, null_handling="special")


def _latest(table: str, key: str = '"_source_record_id"') -> str:
    """Latest version per BUSINESS key, collapsed to exactly one row.

    The key is not always the bronze record id. Montgomery amends a crash by
    deleting it and re-inserting under a brand-new Socrata `:id`, so the same
    `report_number` can hold two `:id` values with different content. Bronze is
    right to keep both — it is append-only history — but a crash fact keyed on
    `:id` would then carry the crash twice and break its own grain.

    Observed 2026-09-15: 4 report_numbers with two `:id` each, one of which had
    its crash time amended (MCP26350097, 04:47 -> 04:34).

    Tie-broken on content hash so a rebuild is deterministic — without it, two
    versions sharing an ingestion timestamp order arbitrarily.
    """
    return f"""
        SELECT * FROM "{table}"
        QUALIFY row_number() OVER (
            PARTITION BY {key}
            ORDER BY "_ingested_at" DESC, "_content_hash"
        ) = 1
    """


def _latest_party(table: str) -> str:
    """Latest version per person, WITHOUT collapsing same-run siblings.

    Party rows need a different rule from crashes, because two distinct people
    in one crash can share a `person_id` (DATA_QUALITY C4). Those rows arrive
    together, in the same ingestion run.

    An amendment, by contrast, arrives in a *later* run. So the rule is "keep
    every row whose ingestion timestamp is the newest for its (crash, person)" —
    same-run siblings both survive because they share that timestamp, while a
    superseded version from an earlier run drops out.

    Collapsing to one row per (report_number, person_id) would have deleted real
    non-motorists; keying on `:id` alone would have kept stale amended twins.
    """
    return f"""
        SELECT * FROM "{table}"
        QUALIFY "_ingested_at" = max("_ingested_at") OVER (
            PARTITION BY report_number, person_id
        )
    """


def build_crash(con: duckdb.DuckDBPyConnection) -> None:
    lat_lo, lat_hi = MOCO_LAT
    lon_lo, lon_hi = MOCO_LON
    lat_sent = ", ".join(str(v) for v in FARS_LAT_SENTINELS)
    lon_sent = ", ".join(str(v) for v in FARS_LON_SENTINELS)

    con.execute(f"""
    CREATE OR REPLACE TABLE silver_crash AS

    WITH moco AS ({_latest('bronze_montgomery_incidents', 'report_number')}),
         txdot AS ({_latest('bronze_txdot_cris_crash', 'crash_id')}),
         fars AS ({_latest('bronze_fars_accident')}),

    -- Crash severity from the worst party injury. Drivers and non-motorists are
    -- both party tables; a crash's severity is the max across both.
    moco_party_sev AS (
        SELECT report_number, max(sev) AS sev FROM (
            SELECT report_number, norm_injury_severity(injury_severity) AS sev
              FROM ({_latest_party('bronze_montgomery_drivers')})
            UNION ALL
            SELECT report_number, norm_injury_severity(injury_severity) AS sev
              FROM ({_latest_party('bronze_montgomery_non_motorists')})
        ) GROUP BY report_number
    ),

    moco_crash AS (
        SELECT
            'MONTGOMERY_MD'                      AS source_system,
            m.report_number                      AS source_record_id,
            'MD'                                 AS jurisdiction,
            TRY_CAST(m.crash_date_time AS TIMESTAMP) AS crash_timestamp,
            TRY_CAST(m.latitude  AS DOUBLE)      AS latitude,
            TRY_CAST(m.longitude AS DOUBLE)      AS longitude,
            CASE
                WHEN TRY_CAST(m.latitude AS DOUBLE) IS NULL
                  OR TRY_CAST(m.longitude AS DOUBLE) IS NULL THEN 'MISSING'
                WHEN TRY_CAST(m.latitude  AS DOUBLE) NOT BETWEEN {lat_lo} AND {lat_hi}
                  OR TRY_CAST(m.longitude AS DOUBLE) NOT BETWEEN {lon_lo} AND {lon_hi}
                    THEN 'OUT_OF_ENVELOPE'
                ELSE 'OK'
            END                                  AS geo_quality,
            -- Party max where available; the 785 crashes with no party row fall
            -- back to the 3-level report type, which cannot separate C/B/A.
            coalesce(p.sev, sev_from_report_type(m.acrs_report_type)) AS severity_ordinal,
            CASE WHEN p.sev IS NOT NULL
                 THEN '{severity.BASIS_PARTY_MAX}'
                 ELSE '{severity.BASIS_REPORT_TYPE}' END AS severity_basis,
            norm_collision_type(m.collision_type) AS collision_type,
            m.weather                            AS weather,
            m.light                              AS light,
            m.surface_condition                  AS surface_condition,
            m."_run_id"                          AS bronze_run_id,
            TRY_CAST(m.crash_date_time AS TIMESTAMP) IS NOT NULL AS time_known,
            m."_content_hash"                    AS content_hash
        FROM moco m
        LEFT JOIN moco_party_sev p USING (report_number)
    ),

    txdot_crash AS (
        SELECT
            'TXDOT_CRIS'                         AS source_system,
            t."_source_record_id"                AS source_record_id,
            'TX'                                 AS jurisdiction,
            TRY_CAST(t.crash_date || ' ' || coalesce(t.crash_time, '00:00:00')
                     AS TIMESTAMP)               AS crash_timestamp,
            -- CRIS-derived pair first, officer-reported as fallback. The
            -- fallback only rescues 203 of 1,562 nulls.
            coalesce(TRY_CAST(t.latitude AS DOUBLE),
                     TRY_CAST(t.rpt_latitude AS DOUBLE))  AS latitude,
            coalesce(TRY_CAST(t.longitude AS DOUBLE),
                     TRY_CAST(t.rpt_longitude AS DOUBLE)) AS longitude,
            CASE WHEN coalesce(TRY_CAST(t.latitude AS DOUBLE),
                               TRY_CAST(t.rpt_latitude AS DOUBLE)) IS NULL
                 THEN 'MISSING' ELSE 'OK' END    AS geo_quality,
            sev_from_crash_sev_id(t.crash_sev_id) AS severity_ordinal,
            '{severity.BASIS_TXDOT_CODE}'        AS severity_basis,
            NULL                                 AS collision_type,
            NULL                                 AS weather,
            NULL                                 AS light,
            NULL                                 AS surface_condition,
            t."_run_id"                          AS bronze_run_id,
            t.crash_time IS NOT NULL             AS time_known,
            t."_content_hash"                    AS content_hash
        FROM txdot t
    ),

    fars_crash AS (
        SELECT
            'NHTSA_FARS'                         AS source_system,
            f."_source_record_id"                AS source_record_id,
            fips_to_usps(f.STATE)                AS jurisdiction,
            -- HOUR='99' and MINUTE='99' are sentinels, not times. A valid hour
            -- with a sentinel minute keeps the hour; an invalid hour drops the
            -- time entirely and keeps the date.
            CASE WHEN TRY_CAST(f.HOUR AS INTEGER) BETWEEN 0 AND 23
                 THEN make_timestamp(
                        TRY_CAST(f.YEAR AS BIGINT), TRY_CAST(f.MONTH AS BIGINT),
                        TRY_CAST(f.DAY AS BIGINT), TRY_CAST(f.HOUR AS BIGINT),
                        CASE WHEN TRY_CAST(f.MINUTE AS INTEGER) BETWEEN 0 AND 59
                             THEN TRY_CAST(f.MINUTE AS BIGINT) ELSE 0 END, 0)
                 ELSE TRY_CAST(f.YEAR || '-' || lpad(f.MONTH, 2, '0') || '-'
                               || lpad(f.DAY, 2, '0') AS TIMESTAMP)
            END                                  AS crash_timestamp,
            CASE WHEN TRY_CAST(f.LATITUDE AS DOUBLE) IN ({lat_sent}) THEN NULL
                 ELSE TRY_CAST(f.LATITUDE AS DOUBLE) END  AS latitude,
            CASE WHEN TRY_CAST(f.LONGITUD AS DOUBLE) IN ({lon_sent}) THEN NULL
                 ELSE TRY_CAST(f.LONGITUD AS DOUBLE) END  AS longitude,
            CASE
                WHEN TRY_CAST(f.LATITUDE AS DOUBLE) IN ({lat_sent})
                  OR TRY_CAST(f.LONGITUD AS DOUBLE) IN ({lon_sent}) THEN 'SENTINEL'
                WHEN TRY_CAST(f.LATITUDE AS DOUBLE) IS NULL THEN 'MISSING'
                ELSE 'OK'
            END                                  AS geo_quality,
            -- FARS is a fatality census: every row has FATALS >= 1.
            {severity.FATAL}                     AS severity_ordinal,
            '{severity.BASIS_FARS_CENSUS}'       AS severity_basis,
            NULL                                 AS collision_type,
            NULL                                 AS weather,
            NULL                                 AS light,
            NULL                                 AS surface_condition,
            f."_run_id"                          AS bronze_run_id,
            TRY_CAST(f.HOUR AS INTEGER) BETWEEN 0 AND 23 AS time_known,
            f."_content_hash"                    AS content_hash
        FROM fars f
    ),

    unioned AS (
        SELECT * FROM moco_crash
        UNION ALL SELECT * FROM txdot_crash
        UNION ALL SELECT * FROM fars_crash
    )

    SELECT
        md5(source_system || ':' || source_record_id) AS crash_sk,
        *,
        CAST(crash_timestamp AS DATE) AS crash_date,
        now()                         AS built_at
    FROM unioned
    """)


def build_party(con: duckdb.DuckDBPyConnection) -> None:
    """Driver and non-motorist facts at person grain.

    Montgomery only — TxDOT's public layer is crash-grain and FARS `person` is
    not ingested (one `--tables person` flag away if a later phase needs it).

    Deduped on Socrata `:id`, NOT `person_id`: the latter collides 9 times in
    the non-motorists table, so keying on it silently drops rows.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE silver_party AS
    SELECT
        md5('MONTGOMERY_MD:' || party_type || ':' || source_record_id) AS party_sk,
        md5('MONTGOMERY_MD:' || report_number)                         AS crash_sk,
        *
    FROM (
        SELECT
            'driver'                             AS party_type,
            d."_source_record_id"                AS source_record_id,
            d.report_number                      AS report_number,
            d.person_id                          AS person_id,
            norm_injury_severity(d.injury_severity) AS severity_ordinal,
            norm_alcohol(d.driver_substance_abuse)  AS alcohol_suspected,
            norm_drugs(d.driver_substance_abuse)    AS drugs_suspected,
            d.driver_substance_abuse             AS substance_abuse_raw,
            d."_run_id"                          AS bronze_run_id
        FROM ({_latest_party('bronze_montgomery_drivers')}) d
        UNION ALL
        SELECT
            'non_motorist'                       AS party_type,
            n."_source_record_id",
            n.report_number,
            n.person_id,
            norm_injury_severity(n.injury_severity),
            norm_alcohol(n.non_motorist_substance_abuse),
            norm_drugs(n.non_motorist_substance_abuse),
            n.non_motorist_substance_abuse,
            n."_run_id"
        FROM ({_latest_party('bronze_montgomery_non_motorists')}) n
    )
    """)


def build(con: duckdb.DuckDBPyConnection, only: str | None = None) -> dict[str, int]:
    register_udfs(con)
    counts = {}
    if only in (None, "crash"):
        build_crash(con)
        counts["silver_crash"] = con.execute("SELECT count(*) FROM silver_crash").fetchone()[0]
    if only in (None, "party"):
        build_party(con)
        counts["silver_party"] = con.execute("SELECT count(*) FROM silver_party").fetchone()[0]
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the silver layer from bronze")
    parser.add_argument("--only", choices=("crash", "party"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = load_settings()
    con = watermark.connect(settings.warehouse_path)
    try:
        counts = build(con, args.only)
        for table, n in counts.items():
            log.info("%s: %s rows", table, f"{n:,}")

        if "silver_crash" in counts:
            print()
            for row in con.execute("""
                SELECT source_system, geo_quality, count(*)
                FROM silver_crash GROUP BY 1, 2 ORDER BY 1, 2
            """).fetchall():
                print(f"  {row[0]:<15} {row[1]:<16} {row[2]:>8,}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
