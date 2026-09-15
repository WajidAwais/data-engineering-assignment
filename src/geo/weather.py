"""Weather join — Open-Meteo ERA5 reanalysis.

Montgomery County's 125,058 crashes span only **30 distinct 0.1-degree cells**,
and one Open-Meteo request returns a full date range per cell. So the whole
history is ~30 calls against a 10,000/day budget, not one call per crash.

Joined on **UTC**, using `crash_timestamp_utc` from Phase 3a. The sources publish
naive local wall-clock; joining on that would silently offset every crash by 4-5
hours and quietly mismatch day and night. The localisation built in 3a is what
makes this join correct rather than plausible.

What this data is
-----------------
ERA5 is *reanalysis* on a ~9-25 km grid — a physical model constrained by
observations, not a measurement. It is spatially smooth and **never missing**,
which is precisely why it is the backbone here: NOAA GHCNh station observation is
accurate at a point and frequently absent, and a weather feature that is null
across the rural half of the county is worse for modelling than a slightly
smoothed one that is always present.

That is a limitation, recorded as one. A crash's "visibility" here is the
model's estimate for an 11 km cell, not what the driver could see.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta

import duckdb
import pandas as pd

from src.config import load_settings
from src.ingest import http, watermark

log = logging.getLogger("geo.weather")

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
# Kept deliberately short. Open-Meteo meters by data volume, not request count,
# so each extra variable over an 11-year hourly range costs real quota.
# `precipitation` already subsumes rain and snowfall for our purposes.
HOURLY = ["temperature_2m", "precipitation", "visibility", "wind_speed_10m"]

CELL_PRECISION = 1     # 0.1 degrees, ~11 km — at ERA5's own resolution
REQUEST_PAUSE_S = 5.0  # volume-weighted quota; pacing beats retrying

# ERA5 is a reanalysis product and lags real time by roughly five days while
# observations are assimilated. Asking past that returns HTTP 400, so the
# request window is clamped rather than letting the job fail on fresh crashes.
ARCHIVE_LAG_DAYS = 6

# Open-Meteo's free tier meters by DATA VOLUME, not request count: 11.7 years of
# hourly data across 4 variables is ~410k values per cell, and 30 of those
# exhausts the hourly budget outright ("Hourly API request limit exceeded").
#
# So the join is bounded to a recent window. An operational pipeline enriching
# *new* crashes needs recent weather, not a decade of it; backfilling the full
# history would need either the paid tier or a chunked job spread over days.
# That is a deliberate cut, recorded in DECISIONS rather than hidden.
DEFAULT_WINDOW_START = "2025-01-01"


def cells(con: duckdb.DuckDBPyConnection, source: str = "MONTGOMERY_MD",
          window_start: str = DEFAULT_WINDOW_START) -> pd.DataFrame:
    return con.execute(f"""
        SELECT round(latitude, {CELL_PRECISION})  AS cell_lat,
               round(longitude, {CELL_PRECISION}) AS cell_lon,
               min(crash_date) AS start_date,
               max(crash_date) AS end_date,
               count(*)        AS crashes
          FROM silver_crash
         WHERE source_system = ? AND geo_quality = 'OK' AND crash_date >= ?
         GROUP BY 1, 2 ORDER BY crashes DESC
    """, [source, window_start]).fetch_df()


def fetch_cell(lat: float, lon: float, start, end, *, session=None) -> pd.DataFrame:
    # DuckDB hands these back as datetime64; the API wants bare ISO dates.
    start = pd.Timestamp(start).date()
    end = min(pd.Timestamp(end).date(), date.today() - timedelta(days=ARCHIVE_LAG_DAYS))
    if start > end:
        return pd.DataFrame()

    resp = http.get(ARCHIVE, params={
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": ",".join(HOURLY), "timezone": "UTC",
    }, session=session)
    payload = json.loads(resp.body)
    hourly = payload.get("hourly") or {}
    if not hourly.get("time"):
        return pd.DataFrame()

    frame = pd.DataFrame(hourly)
    frame["cell_lat"], frame["cell_lon"] = lat, lon
    # Open-Meteo returns naive strings already in the requested timezone (UTC).
    frame["observed_at"] = pd.to_datetime(frame.pop("time"), utc=True)
    return frame


def load(con: duckdb.DuckDBPyConnection, source: str = "MONTGOMERY_MD",
         window_start: str = DEFAULT_WINDOW_START) -> int:
    grid = cells(con, source, window_start)
    log.info("%d weather cells cover %s crashes", len(grid), f"{grid.crashes.sum():,}")

    session = http.build_session()
    frames = []
    for i, row in enumerate(grid.itertuples(index=False), start=1):
        frame = fetch_cell(row.cell_lat, row.cell_lon, row.start_date, row.end_date,
                           session=session)
        frames.append(frame)
        log.info("  cell %2d/%d (%.1f, %.1f): %s hours",
                 i, len(grid), row.cell_lat, row.cell_lon, f"{len(frame):,}")
        time.sleep(REQUEST_PAUSE_S)

    observations = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    con.register("_weather_staging", observations)
    try:
        con.execute("""
            CREATE OR REPLACE TABLE weather_hourly AS
            SELECT * FROM _weather_staging
        """)
        con.execute("CREATE INDEX IF NOT EXISTS ix_weather ON weather_hourly (cell_lat, cell_lon, observed_at)")
    finally:
        con.unregister("_weather_staging")
    return len(observations)


WEATHER_COLUMNS = {
    "temperature_2m": "DOUBLE", "precipitation": "DOUBLE",
    "visibility": "DOUBLE", "wind_speed_10m": "DOUBLE",
}


def join_to_crashes(con: duckdb.DuckDBPyConnection) -> int:
    """Join on the UTC hour. Using the naive local timestamp instead would shift
    every Montgomery crash by 4-5 hours."""
    for column, sqltype in WEATHER_COLUMNS.items():
        con.execute(
            f"ALTER TABLE silver_crash_geo ADD COLUMN IF NOT EXISTS {column} {sqltype}")

    assignments = ", ".join(f"{c} = w.{c}" for c in WEATHER_COLUMNS)
    con.execute(f"""
        UPDATE silver_crash_geo AS g SET {assignments}
          FROM weather_hourly AS w, silver_crash AS c
         WHERE g.crash_sk = c.crash_sk
           AND w.cell_lat = round(c.latitude, {CELL_PRECISION})
           AND w.cell_lon = round(c.longitude, {CELL_PRECISION})
           AND w.observed_at = date_trunc('hour', g.crash_timestamp_utc)
    """)
    return con.execute("""
        SELECT count(*) FROM silver_crash_geo WHERE temperature_2m IS NOT NULL
    """).fetchone()[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Join crashes to ERA5 reanalysis weather")
    parser.add_argument("--source", default="MONTGOMERY_MD")
    parser.add_argument("--since", default=DEFAULT_WINDOW_START,
                        help="weather window start; the free tier meters by volume")
    parser.add_argument("--join-only", action="store_true",
                        help="re-join without re-fetching")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")

    settings = load_settings()
    con = watermark.connect(settings.warehouse_path)
    try:
        if not args.join_only:
            rows = load(con, args.source, args.since)
            log.info("weather_hourly: %s rows", f"{rows:,}")
        matched = join_to_crashes(con)
        total = con.execute("""
            SELECT count(*) FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
             WHERE c.source_system = ?""", [args.source]).fetchone()[0]
    finally:
        con.close()

    print(f"\n  crashes with weather  {matched:>9,} of {total:,}  ({matched/total:.1%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
