"""Geospatial enrichment: bronze-independent, silver-derived.

Builds `silver_crash_geo`, keyed on `crash_sk`. Kept separate from
`silver_crash` so enrichment re-runs without rebuilding the transform, and so a
slow spatial join never blocks the fast path.

Phase 3a covers what needs no download and no API key — timezone and H3 — which
means it covers all 360k geocodable crashes nationally rather than only the
three jurisdictions in scope. Census geography (3b), road snapping (3c) and
weather (3d) add columns to this same table later.

    python -m src.geo.enrich
"""

from __future__ import annotations

import argparse
import logging
import sys

import duckdb
import pandas as pd

from src.config import load_settings
from src.ingest import watermark

from . import h3_index
from .timezone import TimezoneResolver

log = logging.getLogger("geo.enrich")

BATCH_LOG_EVERY = 50_000


def _rows(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Only geocodable crashes. A flagged bad coordinate must not be enriched —
    deriving a timezone from a sentinel would manufacture a calling window for a
    location we do not know."""
    return con.execute("""
        SELECT crash_sk, latitude, longitude, crash_timestamp, jurisdiction, time_known
          FROM silver_crash
         WHERE geo_quality = 'OK'
         ORDER BY crash_sk
    """).fetch_df()


def enrich(con: duckdb.DuckDBPyConnection) -> int:
    df = _rows(con)
    log.info("enriching %s geocodable crashes", f"{len(df):,}")

    resolver = TimezoneResolver()
    out = []

    for i, row in enumerate(df.itertuples(index=False), start=1):
        lat = None if pd.isna(row.latitude) else float(row.latitude)
        lon = None if pd.isna(row.longitude) else float(row.longitude)
        naive = None if pd.isna(row.crash_timestamp) else row.crash_timestamp.to_pydatetime()

        zone, localised = resolver.resolve(lat, lon, naive)
        utc = localised.utc
        time_known = bool(row.time_known)

        out.append({
            "crash_sk": row.crash_sk,
            "iana_timezone": zone,
            "tz_resolution": localised.resolution,
            "crash_timestamp_utc": utc,
            "time_known": time_known,
            # Local wall-clock parts — what a calling window is expressed in.
            # local_hour is NULL where the source time was a sentinel: FARS
            # HOUR='99' rows fall back to midnight, so ~2,000 of them would
            # otherwise read as genuine 00:00 crashes and put a fake spike in
            # any time-of-day distribution. The date survives, so the day of
            # week is still sound.
            "local_hour": naive.hour if (naive is not None and time_known) else None,
            "local_dow": naive.isoweekday() if naive is not None else None,
            "h3_r8": h3_index.cell(lat, lon, h3_index.RES_WORKING),
            "h3_r9": h3_index.cell(lat, lon, h3_index.RES_FINE),
        })

        if i % BATCH_LOG_EVERY == 0:
            log.info("  %s / %s", f"{i:,}", f"{len(df):,}")

    enriched = pd.DataFrame(out)
    con.register("_geo_staging", enriched)
    try:
        con.execute("""
            CREATE OR REPLACE TABLE silver_crash_geo AS
            SELECT *, now() AS enriched_at FROM _geo_staging
        """)
    finally:
        con.unregister("_geo_staging")

    return len(enriched)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Geospatial enrichment of silver_crash")
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
        n = enrich(con)
        log.info("silver_crash_geo: %s rows", f"{n:,}")

        print("\n  timezones by jurisdiction (the traps):")
        for row in con.execute("""
            SELECT c.jurisdiction, g.iana_timezone, count(*)
              FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
             WHERE c.jurisdiction IN ('MD', 'TX', 'FL')
             GROUP BY 1, 2 ORDER BY 1, 3 DESC
        """).fetchall():
            print(f"    {row[0]}  {str(row[1]):<22} {row[2]:>8,}")

        print("\n  DST resolution:")
        for row in con.execute("""
            SELECT tz_resolution, count(*) FROM silver_crash_geo
             GROUP BY 1 ORDER BY 2 DESC
        """).fetchall():
            print(f"    {row[0]:<16} {row[1]:>8,}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
