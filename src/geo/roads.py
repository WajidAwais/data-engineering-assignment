"""Road snapping and linear referencing, Maryland.

Fills the last two geospatial fields in the output contract — `geo.road_class`
and `geo.snap_distance_m` — and implements the rule Part 3b states as a quality
requirement rather than a feature:

    "Record the snap distance as a first-class quality attribute and set a
     rejection threshold. A crash snapped 400 m to a road is not enrichment,
     it is fiction."

Everything here measures in **EPSG:26985** (NAD83 / Maryland). `crs.py` refuses
to hand back a geographic CRS for measurement, so the guard built in Phase 3a
does real work in this module rather than sitting decorative: a nearest-neighbour
search in degrees would return a distance in degrees, and 0.0003 of those looks
much like a small number.

Source is the Geofabrik state extract, not Overpass — the brief caps Overpass at
small ad-hoc queries and a county road network is not one.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd

from src.config import load_settings
from src.ingest import watermark

from .census import fetch
from .crs import assert_measurable, measurement_crs

log = logging.getLogger("geo.roads")

GEOFABRIK_MD = "https://download.geofabrik.de/north-america/us/maryland-latest.osm.pbf"

# Montgomery County envelope plus a buffer, so a crash near the county line can
# still match a segment just outside it.
BBOX = (-77.60, 38.85, -76.80, 39.42)

# Ways that carry vehicle traffic. Footpaths and cycleways are excluded: snapping
# a car crash to a pavement produces a plausible-looking distance and a wrong
# road class.
DRIVABLE = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link",
    "unclassified", "residential", "living_street", "service",
}

# Default rejection threshold. Overridable, and the value is justified from the
# observed distance distribution rather than intuition — see DECISIONS.
DEFAULT_MAX_SNAP_M = 50.0


def extract_path(settings) -> Path:
    path = settings.data_dir / "reference" / "osm"
    path.mkdir(parents=True, exist_ok=True)
    return path / "maryland-latest.osm.pbf"


def _parse_other_tags(value: str | None) -> dict[str, str]:
    """GDAL's OSM driver exposes `highway` and `name` as real columns and pushes
    everything else into an HSTORE string: '"maxspeed"=>"35 mph","lanes"=>"2"'.

    A documented quirk of the driver, not a workaround — maxspeed and lanes are
    only reachable this way without shipping a custom osmconf.ini.
    """
    # Untagged ways come back as NaN, not None, and `not float("nan")` is
    # False — so a falsy check alone lets a float reach the regex.
    if not isinstance(value, str) or not value:
        return {}
    return dict(re.findall(r'"([^"]+)"=>"([^"]*)"', value))


def load_roads(settings, *, force: bool = False) -> gpd.GeoDataFrame:
    path = extract_path(settings)
    if force or not path.exists():
        fetch(GEOFABRIK_MD, path, force=force)

    log.info("reading %s (bbox-clipped to Montgomery)", path.name)
    roads = gpd.read_file(path, layer="lines", bbox=BBOX,
                          columns=["osm_id", "highway", "name", "other_tags"])
    roads = roads[roads["highway"].isin(DRIVABLE)].copy()

    tags = roads["other_tags"].map(_parse_other_tags)
    roads["maxspeed"] = tags.map(lambda t: t.get("maxspeed"))
    roads["lanes"] = tags.map(lambda t: t.get("lanes"))
    roads["ref"] = tags.map(lambda t: t.get("ref"))
    roads = roads.drop(columns=["other_tags"])

    log.info("%s drivable segments", f"{len(roads):,}")
    return roads


def _crash_points(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute("""
        SELECT g.crash_sk, c.latitude, c.longitude
          FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
         WHERE c.source_system = 'MONTGOMERY_MD' AND c.geo_quality = 'OK'
    """).fetch_df()


def snap(points: pd.DataFrame, roads: gpd.GeoDataFrame, *,
         max_snap_m: float = DEFAULT_MAX_SNAP_M) -> pd.DataFrame:
    """Nearest drivable segment, with distance and offset along it.

    Both frames are reprojected to a projected CRS first. `assert_measurable`
    raises on 4326/3857, so this cannot silently measure in degrees.
    """
    crs = assert_measurable(measurement_crs("MD"))

    pts = gpd.GeoDataFrame(
        points, geometry=gpd.points_from_xy(points.longitude, points.latitude),
        crs="EPSG:4326",
    ).to_crs(epsg=crs)
    segs = roads.to_crs(epsg=crs)

    joined = gpd.sjoin_nearest(pts, segs, how="left", distance_col="snap_distance_m")
    joined = joined.drop_duplicates(subset="crash_sk", keep="first")

    # Linear referencing: offset along the matched segment. This is what makes
    # corridor analysis possible instead of point analysis.
    seg_geom = segs.geometry.reindex(joined["index_right"]).reset_index(drop=True)
    pt_geom = joined.geometry.reset_index(drop=True)
    offsets, fractions, lengths = [], [], []
    for line, point in zip(seg_geom, pt_geom):
        if line is None or line.is_empty:
            offsets.append(None); fractions.append(None); lengths.append(None)
            continue
        offset = line.project(point)
        offsets.append(offset)
        lengths.append(line.length)
        fractions.append(offset / line.length if line.length else None)

    out = pd.DataFrame({
        "crash_sk": joined["crash_sk"].to_numpy(),
        "road_class": joined["highway"].to_numpy(),
        "road_name": joined["name"].to_numpy(),
        "road_ref": joined["ref"].to_numpy(),
        "maxspeed": joined["maxspeed"].to_numpy(),
        "lanes": joined["lanes"].to_numpy(),
        "snap_distance_m": joined["snap_distance_m"].to_numpy(),
        "segment_offset_m": offsets,
        "segment_length_m": lengths,
        "segment_fraction": fractions,
    })

    # Flagged, never dropped — consistent with how bad geometry is handled
    # everywhere else in this pipeline.
    out["snap_quality"] = "OK"
    out.loc[out["road_class"].isna(), "snap_quality"] = "NO_ROAD"
    out.loc[out["snap_distance_m"] > max_snap_m, "snap_quality"] = "DISTANCE_EXCEEDED"
    return out


ROAD_COLUMNS = {
    "road_class": "VARCHAR", "road_name": "VARCHAR", "road_ref": "VARCHAR",
    "maxspeed": "VARCHAR", "lanes": "VARCHAR", "snap_distance_m": "DOUBLE",
    "segment_offset_m": "DOUBLE", "segment_length_m": "DOUBLE",
    "segment_fraction": "DOUBLE", "snap_quality": "VARCHAR",
}


def apply_to_warehouse(con: duckdb.DuckDBPyConnection, snapped: pd.DataFrame) -> int:
    for column, sqltype in ROAD_COLUMNS.items():
        con.execute(
            f"ALTER TABLE silver_crash_geo ADD COLUMN IF NOT EXISTS {column} {sqltype}")
    con.register("_roads_staging", snapped)
    try:
        assignments = ", ".join(f"{c} = s.{c}" for c in ROAD_COLUMNS)
        con.execute(f"""
            UPDATE silver_crash_geo AS g SET {assignments}
              FROM _roads_staging AS s WHERE g.crash_sk = s.crash_sk
        """)
    finally:
        con.unregister("_roads_staging")
    return len(snapped)


def run(*, max_snap_m: float = DEFAULT_MAX_SNAP_M, force: bool = False) -> dict:
    settings = load_settings()
    roads = load_roads(settings, force=force)

    con = watermark.connect(settings.warehouse_path)
    try:
        points = _crash_points(con)
        log.info("snapping %s crashes", f"{len(points):,}")
        snapped = snap(points, roads, max_snap_m=max_snap_m)
        apply_to_warehouse(con, snapped)

        percentiles = con.execute("""
            SELECT quantile_cont(snap_distance_m, [0.5, 0.9, 0.95, 0.99])
              FROM silver_crash_geo WHERE snap_distance_m IS NOT NULL
        """).fetchone()[0]
        quality = dict(con.execute("""
            SELECT snap_quality, count(*) FROM silver_crash_geo
             WHERE snap_quality IS NOT NULL GROUP BY 1
        """).fetchall())
        nulls = con.execute("""
            SELECT count(*) FILTER (WHERE maxspeed IS NULL),
                   count(*) FILTER (WHERE lanes IS NULL), count(*)
              FROM silver_crash_geo WHERE snap_quality = 'OK'
        """).fetchone()
    finally:
        con.close()

    return {"segments": len(roads), "snapped": len(snapped),
            "percentiles": percentiles, "quality": quality,
            "maxspeed_null": nulls[0], "lanes_null": nulls[1], "snapped_ok": nulls[2]}


def lookup(records: list[dict], *, settings=None,
           max_snap_m: float = DEFAULT_MAX_SNAP_M) -> dict[str, dict]:
    """Road attributes for arbitrary points, keyed by `party_id`.

    Mirrors `census.lookup()` — degrades to empty when the extract is absent so a
    clean clone can still emit.
    """
    settings = settings or load_settings()
    empty = {r["party_id"]: {} for r in records}
    if not extract_path(settings).exists():
        log.warning("OSM extract absent; emitting null road attributes")
        return empty

    usable = [r for r in records
              if r.get("jurisdiction") == "MD"
              and r.get("latitude") is not None and r.get("longitude") is not None]
    if not usable:
        return empty

    roads = load_roads(settings)
    points = pd.DataFrame([{"crash_sk": r["party_id"], "latitude": r["latitude"],
                            "longitude": r["longitude"]} for r in usable])
    snapped = snap(points, roads, max_snap_m=max_snap_m)

    out = dict(empty)
    for row in snapped.to_dict("records"):
        out[row.pop("crash_sk")] = {
            k: (None if pd.isna(v) else v) for k, v in row.items()
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snap crashes to the road network")
    parser.add_argument("--max-snap-m", type=float, default=DEFAULT_MAX_SNAP_M)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")

    r = run(max_snap_m=args.max_snap_m, force=args.force)
    p50, p90, p95, p99 = r["percentiles"]
    print(f"\n  segments            {r['segments']:>9,}")
    print(f"  crashes snapped     {r['snapped']:>9,}")
    print(f"\n  snap distance (m):  p50={p50:.1f}  p90={p90:.1f}  p95={p95:.1f}  p99={p99:.1f}")
    print("\n  snap quality:")
    for k, v in sorted(r["quality"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:<20} {v:>8,}")
    ok = r["snapped_ok"] or 1
    print(f"\n  tag coverage on accepted snaps (reported, never imputed):")
    print(f"    maxspeed null      {r['maxspeed_null']:>8,}  ({r['maxspeed_null']/ok:.1%})")
    print(f"    lanes null         {r['lanes_null']:>8,}  ({r['lanes_null']/ok:.1%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
