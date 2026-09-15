"""Census geography from TIGER/Line block groups.

Adds `census_bg` and `census_tract` to `silver_crash_geo` — two fields the
output contract asks for that were previously emitting null.

Only the block-group files are fetched. A TIGER block-group GEOID is 12
characters — state(2) + county(3) + tract(6) + blockgroup(1) — and a tract
GEOID is the first 11 of those, so the tract comes free from the block group.
That halves the download and the join. The claim is structural but it is
verified rather than assumed: see tests/test_census.py.

No API key anywhere. TIGER shapefiles are open; the ACS *variables* are what
needed a key, and we deliberately do not hold those — block-group income is a
protected-characteristic proxy. Geographic identifiers yes, socioeconomic
attributes no, which is the same line 18 U.S.C. 2725(3) draws around ZIP5.
"""

from __future__ import annotations

import argparse
import logging
import sys
import zipfile
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
import requests

from src.config import load_settings
from src.ingest import watermark

log = logging.getLogger("geo.census")

TIGER_BG = "https://www2.census.gov/geo/tiger/TIGER2025/BG/tl_2025_{fips}_bg.zip"
TIGER_TRACT = "https://www2.census.gov/geo/tiger/TIGER2025/TRACT/tl_2025_{fips}_tract.zip"

STATE_FIPS = {"MD": "24", "TX": "48", "FL": "12"}

# TIGER publishes in NAD83. Our coordinates are WGS84. The two differ by 1-2 m
# in CONUS, far inside block-group boundary precision — but they get aligned
# rather than relied upon. Note this is a containment test, not a measurement,
# so crs.py's guard does not apply: alignment here is about join correctness.
TIGER_CRS = 4269
POINT_CRS = 4326


def cache_dir(settings) -> Path:
    path = settings.data_dir / "reference" / "tiger"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(url: str, dest: Path, *, force: bool = False) -> Path:
    """Conditional download. Same ETag-versioning idea as src/ingest/fars.py:
    an unchanged file costs one HEAD."""
    etag_file = dest.with_suffix(".etag")
    if dest.exists() and not force:
        head = requests.head(url, timeout=60, allow_redirects=True)
        head.raise_for_status()
        if etag_file.exists() and etag_file.read_text().strip() == head.headers.get("ETag", ""):
            log.debug("%s unchanged", dest.name)
            return dest

    log.info("downloading %s", dest.name)
    with requests.get(url, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
        etag_file.write_text(resp.headers.get("ETag", ""))
    return dest


def load_block_groups(settings, states: list[str], *, force: bool = False) -> gpd.GeoDataFrame:
    frames = []
    for state in states:
        fips = STATE_FIPS[state]
        path = fetch(TIGER_BG.format(fips=fips), cache_dir(settings) / f"bg_{fips}.zip",
                     force=force)
        # GeoPandas reads a zipped shapefile in place via GDAL's vsizip handler.
        gdf = gpd.read_file(f"zip://{path}")[["GEOID", "geometry"]]
        gdf["state"] = state
        frames.append(gdf)
        log.info("%s: %s block groups", state, f"{len(gdf):,}")

    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    return combined.to_crs(epsg=POINT_CRS)


def load_tract_geoids(settings, state: str = "MD", *, force: bool = False) -> set[str]:
    """Real tract GEOIDs, used only to verify the nesting claim in tests."""
    fips = STATE_FIPS[state]
    path = fetch(TIGER_TRACT.format(fips=fips), cache_dir(settings) / f"tract_{fips}.zip",
                 force=force)
    return set(gpd.read_file(f"zip://{path}")["GEOID"])


def _crash_points(con: duckdb.DuckDBPyConnection, states: list[str]) -> pd.DataFrame:
    """Geocodable crashes in the states we hold boundaries for.

    Anything else stays null rather than being matched to a nearest polygon —
    a wrong tract is worse than an absent one.
    """
    placeholders = ", ".join("?" for _ in states)
    return con.execute(f"""
        SELECT g.crash_sk, c.latitude, c.longitude
          FROM silver_crash_geo g
          JOIN silver_crash c USING (crash_sk)
         WHERE c.geo_quality = 'OK' AND c.jurisdiction IN ({placeholders})
    """, states).fetch_df()


def assign(points: pd.DataFrame, block_groups: gpd.GeoDataFrame) -> pd.DataFrame:
    """Point-in-polygon. GeoPandas rather than DuckDB spatial: sjoin builds an
    STRtree index, and 182k points against ~25k polygons without one is a
    nested loop."""
    gdf = gpd.GeoDataFrame(
        points,
        geometry=gpd.points_from_xy(points.longitude, points.latitude),
        crs=f"EPSG:{POINT_CRS}",
    )
    joined = gpd.sjoin(gdf, block_groups[["GEOID", "geometry"]],
                       how="left", predicate="within")
    # A point on a shared boundary can match twice; keep the first deterministically.
    joined = joined.drop_duplicates(subset="crash_sk", keep="first")
    joined = joined.rename(columns={"GEOID": "census_bg"})
    joined["census_tract"] = joined["census_bg"].str[:11]
    return joined[["crash_sk", "census_bg", "census_tract"]]


def apply_to_warehouse(con: duckdb.DuckDBPyConnection, assigned: pd.DataFrame) -> int:
    for column in ("census_bg", "census_tract"):
        con.execute(f'ALTER TABLE silver_crash_geo ADD COLUMN IF NOT EXISTS {column} VARCHAR')
    con.register("_census_staging", assigned)
    try:
        con.execute("""
            UPDATE silver_crash_geo AS g
               SET census_bg = s.census_bg, census_tract = s.census_tract
              FROM _census_staging AS s
             WHERE g.crash_sk = s.crash_sk
        """)
    finally:
        con.unregister("_census_staging")
    return int(assigned["census_bg"].notna().sum())


def run(states: list[str] | None = None, *, force: bool = False) -> dict:
    settings = load_settings()
    states = states or list(STATE_FIPS)
    block_groups = load_block_groups(settings, states, force=force)

    con = watermark.connect(settings.warehouse_path)
    try:
        points = _crash_points(con, states)
        log.info("joining %s crashes against %s block groups",
                 f"{len(points):,}", f"{len(block_groups):,}")
        assigned = assign(points, block_groups)
        matched = apply_to_warehouse(con, assigned)
        total = con.execute("SELECT count(*) FROM silver_crash_geo").fetchone()[0]
    finally:
        con.close()

    return {"states": states, "candidates": len(points),
            "matched": matched, "block_groups": len(block_groups), "geo_rows": total}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Join crashes to TIGER census geography")
    parser.add_argument("--states", default=",".join(STATE_FIPS))
    parser.add_argument("--force", action="store_true", help="re-download regardless of ETag")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")

    result = run([s.strip().upper() for s in args.states.split(",") if s.strip()],
                 force=args.force)
    unmatched = result["candidates"] - result["matched"]
    print(f"\n  block groups loaded  {result['block_groups']:>8,}")
    print(f"  in-scope crashes     {result['candidates']:>8,}")
    print(f"  assigned a tract     {result['matched']:>8,}")
    print(f"  unmatched            {unmatched:>8,}"
          + (f"  ({unmatched / result['candidates']:.2%})" if result["candidates"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())


def lookup(records: list[dict], *, settings=None, states: list[str] | None = None
           ) -> dict[str, tuple[str | None, str | None]]:
    """Block group + tract for arbitrary points, keyed by `party_id`.

    Used by the emitter for fixture party coordinates. Degrades to all-None if
    the TIGER cache is absent so a clean clone can still emit — an absent tract
    is honest, a crashed emitter is not.
    """
    from src.config import load_settings

    settings = settings or load_settings()
    states = states or sorted({r.get("jurisdiction") for r in records
                               if r.get("jurisdiction") in STATE_FIPS})
    empty = {r["party_id"]: (None, None) for r in records}
    if not states:
        return empty

    try:
        block_groups = load_block_groups(settings, states)
    except Exception as exc:  # noqa: BLE001 - absent cache is an expected state
        log.warning("census lookup unavailable (%s); emitting null geography", exc)
        return empty

    points = pd.DataFrame([
        {"crash_sk": r["party_id"], "latitude": r.get("latitude"),
         "longitude": r.get("longitude")}
        for r in records
        if r.get("latitude") is not None and r.get("longitude") is not None
    ])
    if points.empty:
        return empty

    assigned = assign(points, block_groups)
    out = dict(empty)
    for row in assigned.itertuples(index=False):
        bg = None if pd.isna(row.census_bg) else row.census_bg
        tract = None if pd.isna(row.census_tract) else row.census_tract
        out[row.crash_sk] = (bg, tract)
    return out
