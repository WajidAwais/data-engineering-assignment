"""GeoParquet 1.1.0 export with a bbox covering column.

Part 6 is specific about the version: *"GeoParquet **1.1.0** (the current stable
spec — target it, and use the `bbox` covering column for row-group pruning; 2.0
is still a release candidate and GeoParquet remains an incubating OGC standard)."*

Why the bbox covering column matters
------------------------------------
A reader filtering on a spatial predicate can only skip a row group if it knows
that group's extent without decoding the geometry. 1.1.0 adds a `covering`
entry to the file metadata pointing at a struct column of xmin/ymin/xmax/ymax,
so the reader prunes on ordinary Parquet statistics. Without it, a bounding-box
query decompresses every row group.

Partitioning
------------
By `source_system` then crash year. Both are how the data is actually queried —
a jurisdiction-scoped compliance question never wants FARS's other 47 states,
and the statutory windows are all date-anchored. File sizes are reported by the
exporter rather than asserted, so the choice can be argued from measurement.

Honest assessment of the bbox column at THIS size
-------------------------------------------------
Measured mean row-group bbox width, as a fraction of each file's full extent:

    NHTSA_FARS   / 2023   5 groups   44.4%
    MONTGOMERY_MD/ 2025   2 groups   67.1%
    TXDOT_CRIS   / 2024   1 group   100.0%

Two things fall out, and the second contradicts the intuition:

1. A partition small enough to fit one row group cannot be pruned at all. Most
   of ours are — the median file is under 700 KB.
2. **The national partition prunes better than the county one.** FARS spans a
   continent with dense urban clusters, so H3-sorted rows concentrate; Montgomery
   is spread fairly evenly across a small county, so consecutive cells still
   cover much of its (small) extent. Absolute area is irrelevant; what matters is
   how clustered the points are *relative to* the partition.

So at this scale the partition scheme is doing essentially all the pruning and
the covering column earns little. It is implemented because the spec requires it
and because it matters at national multi-year volume — not because it is paying
for itself here. Claiming otherwise would be selling an optimisation that the
measurement does not support.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from shapely import wkb

from src.config import load_settings
from src.ingest import watermark

log = logging.getLogger("ops.geoparquet")

GEOPARQUET_VERSION = "1.1.0"
STORAGE_CRS = "EPSG:4326"

# Row-group size, and it is load-bearing rather than conventional. At 50,000 a
# typical partition here fits in ONE row group, and a reader can then only skip
# the whole file or none of it — the covering column buys nothing. 8,000 gives a
# median partition 2-5 groups, which is where pruning starts to mean something.
ROW_GROUP_SIZE = 8_000


def _bbox_struct(geometries: gpd.GeoSeries) -> pa.Array:
    """The covering column: one xmin/ymin/xmax/ymax struct per row."""
    bounds = geometries.bounds  # minx, miny, maxx, maxy
    return pa.StructArray.from_arrays(
        [pa.array(bounds[c].to_numpy(), type=pa.float32())
         for c in ("minx", "miny", "maxx", "maxy")],
        names=["xmin", "ymin", "xmax", "ymax"],
    )


def geo_metadata(geometry_column: str, bounds: tuple, *,
                 bbox_column: str = "bbox") -> dict:
    """GeoParquet 1.1.0 file metadata, including the `covering` pointer.

    `covering` is the 1.1.0 addition that makes the bbox column usable for
    pruning: without it a reader sees an ordinary struct column and has no
    reason to consult it.
    """
    return {
        "version": GEOPARQUET_VERSION,
        "primary_column": geometry_column,
        "columns": {
            geometry_column: {
                "encoding": "WKB",
                "geometry_types": ["Point"],
                "crs": STORAGE_CRS,
                "bbox": [float(b) for b in bounds],
                "covering": {
                    "bbox": {
                        "xmin": [bbox_column, "xmin"],
                        "ymin": [bbox_column, "ymin"],
                        "xmax": [bbox_column, "xmax"],
                        "ymax": [bbox_column, "ymax"],
                    }
                },
            }
        },
    }


def export_partition(frame: pd.DataFrame, path: Path) -> dict:
    """Write one partition as GeoParquet 1.1.0.

    Rows arrive sorted by H3 cell, which is the other half of making the bbox
    column useful: pruning works by comparing a query box against each row
    group's extent, so if rows sit in arbitrary order every group's bbox spans
    the whole partition and nothing is ever skipped. Sorting on a
    space-filling-curve index keeps neighbouring rows in the same group.
    """
    gdf = gpd.GeoDataFrame(
        frame.drop(columns=["latitude", "longitude"]),
        geometry=gpd.points_from_xy(frame.latitude, frame.longitude),
        crs=STORAGE_CRS,
    )

    table = pa.Table.from_pandas(
        pd.DataFrame(gdf.drop(columns=["geometry"])), preserve_index=False)
    table = table.append_column(
        "geometry", pa.array(gdf.geometry.apply(lambda g: wkb.dumps(g)), type=pa.binary()))
    table = table.append_column("bbox", _bbox_struct(gdf.geometry))

    metadata = dict(table.schema.metadata or {})
    metadata[b"geo"] = json.dumps(
        geo_metadata("geometry", gdf.total_bounds)).encode()
    table = table.replace_schema_metadata(metadata)

    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd", row_group_size=ROW_GROUP_SIZE)
    return {"path": path, "rows": len(frame), "bytes": path.stat().st_size}


def export(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[dict]:
    """Partition by source_system then crash year — both are how this data is
    actually filtered."""
    partitions = con.execute("""
        SELECT source_system, date_part('year', crash_date)::INTEGER AS yr, count(*)
          FROM silver_crash WHERE geo_quality = 'OK' AND crash_date IS NOT NULL
         GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()

    written = []
    for source_system, year, _ in partitions:
        frame = con.execute("""
            SELECT c.crash_sk, c.source_system, c.source_record_id, c.jurisdiction,
                   c.crash_date, c.crash_timestamp, c.severity_ordinal,
                   c.severity_basis, c.geo_quality, c.latitude, c.longitude,
                   g.h3_r8
              FROM silver_crash c LEFT JOIN silver_crash_geo g USING (crash_sk)
             WHERE c.source_system = ? AND date_part('year', c.crash_date) = ?
               AND c.geo_quality = 'OK' AND c.crash_date IS NOT NULL
             ORDER BY h3_r8, crash_sk
        """, [source_system, year]).fetch_df()
        path = out_dir / f"source_system={source_system}" / f"year={year}" / "part-0.parquet"
        written.append(export_partition(frame, path))
    return written


def verify(path: Path) -> dict:
    """Read back the metadata a GeoParquet reader would consult."""
    meta = pq.read_metadata(path)
    geo = json.loads(pq.read_schema(path).metadata[b"geo"])
    # How tight are the per-group bboxes? If each group spans the whole file,
    # pruning is theoretical — the covering column is present but useless.
    table = pq.read_table(path, columns=["bbox"])
    bbox = table.column("bbox").combine_chunks()
    full_w = max(bbox.field("xmax").to_pylist()) - min(bbox.field("xmin").to_pylist())

    widths = []
    offset = 0
    for i in range(meta.num_row_groups):
        n = meta.row_group(i).num_rows
        chunk = bbox.slice(offset, n)
        widths.append(max(chunk.field("xmax").to_pylist())
                      - min(chunk.field("xmin").to_pylist()))
        offset += n

    return {
        "version": geo["version"],
        "primary_column": geo["primary_column"],
        "has_covering": "covering" in geo["columns"][geo["primary_column"]],
        "row_groups": meta.num_row_groups,
        "rows": meta.num_rows,
        "mean_group_width_frac": round(sum(widths) / len(widths) / full_w, 4) if full_w else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export silver as GeoParquet 1.1.0")
    parser.add_argument("--out", default="data/export/geoparquet")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = load_settings()
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = settings.data_dir.parent / out_dir

    con = watermark.connect(settings.warehouse_path)
    try:
        written = export(con, out_dir)
    finally:
        con.close()

    total_rows = sum(w["rows"] for w in written)
    total_bytes = sum(w["bytes"] for w in written)
    sizes = sorted(w["bytes"] for w in written)

    print(f"\n  {len(written)} partitions   {total_rows:,} rows   "
          f"{total_bytes/1_048_576:.1f} MB")
    print(f"  bytes per row: {total_bytes/total_rows:.1f}")
    print(f"  file sizes: min {sizes[0]/1024:.0f} KB   "
          f"median {sizes[len(sizes)//2]/1024:.0f} KB   max {sizes[-1]/1_048_576:.1f} MB")

    sample = max(written, key=lambda w: w["rows"])
    print(f"\n  verifying {sample['path'].parent.name} ({sample['rows']:,} rows):")
    for key, value in verify(sample["path"]).items():
        print(f"    {key:<16} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
