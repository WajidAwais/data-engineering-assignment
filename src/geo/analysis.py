"""Spatial analysis: Getis-Ord Gi*, Moran's I / LISA, and ST-DBSCAN.

Scoped to Montgomery County — the only jurisdiction with an all-severity source
(not a fatality census) and an enriched road network, which is what makes a
road-length denominator possible at all.

Results land in `gold_*` tables; the interpretation lives in ANALYSIS.md.
Part 3c is explicit that "an unlabeled heatmap is not an analysis", so a number
without a written reading of it is not finished work.
"""

from __future__ import annotations

import argparse
import logging
import sys
import warnings

import duckdb
import numpy as np
import pandas as pd
from esda import G_Local, Moran, Moran_Local
from libpysal.weights import W
from scipy.spatial import cKDTree
from sklearn.cluster import DBSCAN

from src.config import load_settings
from src.ingest import watermark

from . import h3_index
from .crs import measurement_crs

log = logging.getLogger("geo.analysis")

MIN_CRASHES_PER_CELL = 5   # below this, a rate is noise dressed as a statistic
ALPHA = 0.05
# The permutation count is not a free parameter once FDR is involved: p_sim can
# never fall below 1/(permutations+1), and Benjamini-Hochberg compares the k-th
# smallest p against (k/n)*alpha. With 999 permutations the floor is 1e-3, and
# LISA's 19 floor-valued cells needed to clear 19/1116*0.05 = 8.5e-4 — so the
# floor, not the data, was rejecting them. 9,999 permutations puts the floor at
# 1e-4 and lets the evidence decide.
PERMUTATIONS = 9999

# Gi* and LISA derive significance from random permutations. Unseeded, the same
# data yields a different hotspot count on every run — measured: 16 hot cells on
# one pass and 0 on the next. An analysis that will not reproduce is not a
# finding, so the generator is pinned and the seed is reported.
SEED = 20260915

# ST-DBSCAN thresholds. Two of them, deliberately — see st_dbscan().
#
# Chosen from a sweep over 19,892 injury+ crashes spanning 11.7 years, because
# the brief names the failure mode rather than the parameter: clusters should be
# "tight in space and time ... not a year-long smear".
#
#   eps_t     clustered   clusters   largest
#      72h            0          0         0   too tight to find anything
#     720h          100         17        12   high precision, very few
#    2160h        2,288        305        35   <- chosen
#    8760h       12,038        501     1,181   the smear, exactly as warned
#
# A one-year temporal window produces a 1,181-crash cluster: that is a map of
# where crashes happen, not a space-time cluster. 90 days keeps clusters
# interpretable as recurring conditions rather than a decade of accumulation.
EPS_SPATIAL_M = 200.0
EPS_TEMPORAL_H = 2160.0   # 90 days
MIN_SAMPLES = 5


# --- multiple testing -------------------------------------------------------

def benjamini_hochberg(pvals: np.ndarray, alpha: float = ALPHA) -> np.ndarray:
    """BH step-up. Returns a boolean mask of rejected nulls.

    Written out rather than pulled from statsmodels: it is five lines, and one
    fewer dependency for a project that already carries plenty.
    """
    n = len(pvals)
    order = np.argsort(pvals)
    ranked = pvals[order]
    thresholds = alpha * (np.arange(1, n + 1) / n)
    passed = ranked <= thresholds
    if not passed.any():
        return np.zeros(n, dtype=bool)
    cutoff = ranked[passed].max()
    return pvals <= cutoff


# --- cell aggregation -------------------------------------------------------

def cell_counts(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Crash counts per H3 r8 cell, with a road-length denominator.

    Road length is attributed by the matched segment's MIDPOINT cell, so a
    segment straddling two cells is credited wholly to one. An approximation,
    and labelled as one — at r8 (~0.74 km²) against a mean segment of ~480 m
    most segments sit in one or two cells and the error averages out.
    """
    return con.execute(f"""
        WITH crashes AS (
            SELECT g.h3_r8,
                   count(*) AS crashes,
                   count(*) FILTER (WHERE c.severity_ordinal >= 3) AS serious,
                   -- Only accepted snaps contribute road length. A segment
                   -- 400 m from the crash is not evidence of road in the cell.
                   sum(CASE WHEN g.snap_quality = 'OK'
                            THEN g.segment_length_m END) AS snapped_length_m,
                   count(DISTINCT CASE WHEN g.snap_quality = 'OK'
                                       THEN g.road_name END) AS distinct_roads
              FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
             WHERE c.source_system = 'MONTGOMERY_MD' AND g.h3_r8 IS NOT NULL
             GROUP BY 1
        )
        -- ORDER BY is load-bearing, not cosmetic. DuckDB gives no row-order
        -- guarantee without it, and the row order determines the order cells
        -- enter the weights matrix, which changes every permutation test
        -- downstream even with a fixed seed. Measured: FDR hotspots swung
        -- between 0 and 12 across runs before this was added.
        SELECT * FROM crashes WHERE crashes >= {MIN_CRASHES_PER_CELL}
         ORDER BY h3_r8
    """).fetch_df()


def road_km_per_cell(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Distinct road length per cell, de-duplicated by segment.

    Summing `segment_length_m` over crashes would count a segment once per crash
    on it — so a busy road would inflate its own denominator and mask exactly
    the hotspots we are looking for. Distinct segment identity is the fix.
    """
    return con.execute("""
        SELECT h3_r8,
               sum(segment_length_m) / 1000.0 AS road_km
          FROM (
            SELECT DISTINCT g.h3_r8, g.road_name, g.road_class, g.segment_length_m
              FROM silver_crash_geo g JOIN silver_crash c USING (crash_sk)
             WHERE c.source_system = 'MONTGOMERY_MD'
               AND g.snap_quality = 'OK' AND g.h3_r8 IS NOT NULL
          ) GROUP BY 1 ORDER BY h3_r8
    """).fetch_df()


def build_weights(cells: list[str]) -> W:
    # Islands are expected: a cell whose six H3 neighbours all fall below the
    # crash floor has no neighbourhood, and a local statistic needs one.
    # libpysal prints these directly, so they are muted at the source.
    """Queen-style contiguity from H3's own grid. Every r8 cell has six
    neighbours by construction, so no distance-band tuning is needed."""
    index = set(cells)
    neighbours = {
        cell: [n for n in h3_index.neighbours(cell, 1) if n != cell and n in index]
        for cell in cells
    }
    # Islands (no neighbour in the study area) get dropped by libpysal with a
    # warning; that is correct — a local statistic needs a neighbourhood.
    import libpysal
    libpysal.weights.util.silence_warnings = True
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return W(neighbours, silence_warnings=True)


# --- 1. Getis-Ord Gi* -------------------------------------------------------

def getis_ord(frame: pd.DataFrame, value_col: str, weights: W) -> pd.DataFrame:
    """Gi* with FDR correction.

    `star=True` includes the cell itself in its own neighbourhood, which is what
    makes it Gi* rather than Gi and is the right choice for "is this cell and its
    surroundings a hotspot".
    """
    values = frame[value_col].to_numpy(dtype=float)
    gi = G_Local(values, weights, star=True,
                 permutations=PERMUTATIONS, seed=SEED)

    out = frame[["h3_r8", value_col]].copy()
    out["z_score"] = gi.Zs
    out["p_value"] = gi.p_sim
    out["significant_uncorrected"] = out["p_value"] < ALPHA
    out["significant_fdr"] = benjamini_hochberg(out["p_value"].to_numpy(), ALPHA)
    out["class"] = np.where(~out["significant_fdr"], "not significant",
                     np.where(out["z_score"] > 0, "hot", "cold"))
    return out


# --- 2. Moran's I, then LISA ------------------------------------------------

def morans_i(frame: pd.DataFrame, value_col: str, weights: W) -> dict:
    """Global first. The point of running it is that it can come back
    uninteresting — asserting that crashes cluster without testing it is the
    error this guards against."""
    values = frame[value_col].to_numpy(dtype=float)
    np.random.seed(SEED)   # Moran takes no seed argument
    mi = Moran(values, weights, permutations=PERMUTATIONS)
    return {"I": float(mi.I), "expected_I": float(mi.EI),
            "z_score": float(mi.z_sim), "p_value": float(mi.p_sim)}


def lisa(frame: pd.DataFrame, value_col: str, weights: W) -> pd.DataFrame:
    values = frame[value_col].to_numpy(dtype=float)
    np.random.seed(SEED)
    local = Moran_Local(values, weights, permutations=PERMUTATIONS)
    labels = {1: "HH", 2: "LH", 3: "LL", 4: "HL"}
    out = frame[["h3_r8", value_col]].copy()
    out["quadrant"] = [labels.get(q, "?") for q in local.q]
    out["p_value"] = local.p_sim
    out["significant_fdr"] = benjamini_hochberg(local.p_sim, ALPHA)
    out.loc[~out["significant_fdr"], "quadrant"] = "ns"
    return out


# --- 3. ST-DBSCAN -----------------------------------------------------------

def st_dbscan(points: pd.DataFrame, *, eps_spatial_m: float = EPS_SPATIAL_M,
              eps_temporal_h: float = EPS_TEMPORAL_H,
              min_samples: int = MIN_SAMPLES) -> pd.DataFrame:
    """Birant & Kut ST-DBSCAN: TWO thresholds, not one.

    Two points are neighbours only if they are within eps_spatial AND within
    eps_temporal. Rescaling time into the spatial metric and running ordinary
    DBSCAN with a single eps is a *different* algorithm — it lets a large spatial
    gap be bought back by temporal closeness, which is exactly the "year-long
    smear" the brief warns about.

    Built sparsely: a KD-tree finds spatial neighbours, time filters them, and
    the resulting graph goes to DBSCAN(metric="precomputed"). A dense 125k x 125k
    matrix is not an option.
    """
    from scipy.sparse import coo_matrix

    coords = points[["x", "y"]].to_numpy()
    times = points["t_hours"].to_numpy()

    tree = cKDTree(coords)
    pairs = tree.query_pairs(eps_spatial_m, output_type="ndarray")
    if len(pairs) == 0:
        points = points.copy()
        points["cluster"] = -1
        return points

    close_in_time = np.abs(times[pairs[:, 0]] - times[pairs[:, 1]]) <= eps_temporal_h
    pairs = pairs[close_in_time]

    n = len(points)
    # Distance 1 marks "is a neighbour"; eps=1 then reproduces the joint rule.
    rows = np.concatenate([pairs[:, 0], pairs[:, 1], np.arange(n)])
    cols = np.concatenate([pairs[:, 1], pairs[:, 0], np.arange(n)])
    data = np.ones(len(rows))
    graph = coo_matrix((data, (rows, cols)), shape=(n, n)).tocsr()

    labels = DBSCAN(eps=1, min_samples=min_samples, metric="precomputed").fit_predict(graph)
    out = points.copy()
    out["cluster"] = labels
    return out


def severe_points(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Injury-or-worse crashes, projected so distances are in metres."""
    crs = measurement_crs("MD")
    frame = con.execute("""
        SELECT c.crash_sk, c.latitude, c.longitude, g.crash_timestamp_utc,
               c.severity_ordinal, g.road_name
          FROM silver_crash c JOIN silver_crash_geo g USING (crash_sk)
         WHERE c.source_system = 'MONTGOMERY_MD' AND c.geo_quality = 'OK'
           AND c.severity_ordinal >= 2 AND g.crash_timestamp_utc IS NOT NULL
    """).fetch_df()

    import geopandas as gpd
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.points_from_xy(frame.longitude, frame.latitude),
        crs="EPSG:4326").to_crs(epsg=crs)
    frame["x"] = gdf.geometry.x
    frame["y"] = gdf.geometry.y
    epoch = pd.Timestamp(frame["crash_timestamp_utc"].min())
    frame["t_hours"] = (
        pd.to_datetime(frame["crash_timestamp_utc"]) - epoch
    ).dt.total_seconds() / 3600.0
    return frame


# --- driver -----------------------------------------------------------------

def run() -> dict:
    settings = load_settings()
    con = watermark.connect(settings.warehouse_path)
    try:
        counts = cell_counts(con)
        roads = road_km_per_cell(con)
        frame = counts.merge(roads, on="h3_r8", how="left")
        frame["road_km"] = frame["road_km"].fillna(0.0)
        frame["per_road_km"] = np.where(
            frame["road_km"] > 0.1, frame["crashes"] / frame["road_km"], np.nan)
        # Sorted again after the merge: a pandas merge does not promise to
        # preserve order, and the whole point is a deterministic cell sequence.
        frame = (frame.dropna(subset=["per_road_km"])
                      .sort_values("h3_r8").reset_index(drop=True))

        # Islands — cells whose six H3 neighbours all fall below the crash
        # floor — have no neighbourhood, so their permutation distribution is
        # degenerate and the z-score divides by zero. Dropped before the
        # statistics run rather than filtered out of NaNs afterwards.
        provisional = build_weights(frame["h3_r8"].tolist())
        islands = {c for c, nb in provisional.neighbors.items() if not nb}
        if islands:
            log.info("dropping %d island cells (no neighbours above the floor)", len(islands))
            frame = (frame[~frame["h3_r8"].isin(islands)]
                     .sort_values("h3_r8").reset_index(drop=True))

        weights = build_weights(frame["h3_r8"].tolist())
        log.info("%d cells analysed", len(frame))

        hot_raw = getis_ord(frame, "crashes", weights)
        hot_rate = getis_ord(frame, "per_road_km", weights)
        mi_raw = morans_i(frame, "crashes", weights)
        mi_rate = morans_i(frame, "per_road_km", weights)
        lisa_rate = lisa(frame, "per_road_km", weights)

        points = severe_points(con)
        clustered = st_dbscan(points)

        for name, df in (("gold_hotspot_raw", hot_raw), ("gold_hotspot_rate", hot_rate),
                         ("gold_lisa", lisa_rate),
                         ("gold_stdbscan", clustered[["crash_sk", "cluster", "road_name",
                                                      "severity_ordinal"]])):
            con.register("_analysis_staging", df)
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _analysis_staging")
            con.unregister("_analysis_staging")

        n_clusters = int((clustered["cluster"] >= 0).sum())
        result = {
            "cells": len(frame), "islands_dropped": len(islands),
            "hot_raw": int((hot_raw["class"] == "hot").sum()),
            "hot_rate": int((hot_rate["class"] == "hot").sum()),
            "hot_raw_uncorrected": int(hot_raw["significant_uncorrected"].sum()),
            "hot_rate_uncorrected": int(hot_rate["significant_uncorrected"].sum()),
            "agree": int(((hot_raw["class"] == "hot") & (hot_rate["class"] == "hot")).sum()),
            "moran_raw": mi_raw, "moran_rate": mi_rate,
            "lisa_hh": int((lisa_rate["quadrant"] == "HH").sum()),
            "st_points": len(points), "st_clustered": n_clusters,
            "st_clusters": int(clustered.loc[clustered["cluster"] >= 0, "cluster"].nunique()),
        }
    finally:
        con.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spatial analysis over Montgomery crashes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")

    r = run()
    print(f"\n  H3 r8 cells analysed        {r['cells']:>6,}  (>= {MIN_CRASHES_PER_CELL} crashes)")
    print(f"\n  Getis-Ord Gi* hotspots")
    print(f"    raw counts   uncorrected {r['hot_raw_uncorrected']:>6,}  ->  FDR {r['hot_raw']:>4,}")
    print(f"    per road-km  uncorrected {r['hot_rate_uncorrected']:>6,}  ->  FDR {r['hot_rate']:>4,}")
    print(f"    cells hot under BOTH denominators  {r['agree']:>4,}")
    print(f"\n  Moran's I")
    for label, mi in (("raw counts", r["moran_raw"]), ("per road-km", r["moran_rate"])):
        print(f"    {label:<12} I={mi['I']:+.4f}  z={mi['z_score']:+.2f}  p={mi['p_value']:.4f}")
    print(f"    LISA HH cells (per road-km, FDR)   {r['lisa_hh']:>4,}")
    print(f"\n  ST-DBSCAN  (eps_s={EPS_SPATIAL_M:.0f}m, eps_t={EPS_TEMPORAL_H:.0f}h, min={MIN_SAMPLES})")
    print(f"    injury+ crashes             {r['st_points']:>6,}")
    print(f"    in a space-time cluster     {r['st_clustered']:>6,}  in {r['st_clusters']} clusters")
    return 0


if __name__ == "__main__":
    sys.exit(main())
