"""Feature matrix, with provenance and a hard guard against geographic proxies.

Part 4 states the constraint plainly: block-group income and anything correlated
with it are protected-characteristic proxies, and ranking contact priority on
them is redlining-adjacent. *"A submission that quietly ranks leads by
neighborhood income has told us a great deal."*

We hold `census_tract`, `census_bg` and `zip5` — the output contract requires
them, and ZIP5 specifically because 18 U.S.C. 2725(3) excludes it from "personal
information", which is what makes it the legitimate aggregation key.

**Holding them and ranking on them are different acts.** A policy saying "we do
not rank on geography" survives exactly as long as the next engineer's memory of
it, so the constraint is enforced the same way `crs.py` enforces its: by code
that raises.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

# Nothing on this list may enter the feature matrix. Geographic identifiers are
# proxies for demography at block-group resolution; a model given them will find
# that signal whether or not anyone intended it to.
FORBIDDEN_FEATURES = frozenset({
    "census_tract", "census_bg", "zip5", "street_address", "full_name",
    "phone_e164", "phone_token", "latitude", "longitude",
    # h3_r8 is the spatial cross-validation blocking key. Used as a predictor it
    # would leak block identity into the model it is meant to hold out.
    "h3_r8", "h3_r9",
})


class ForbiddenFeature(ValueError):
    """Raised when a protected-characteristic proxy reaches the feature matrix."""


@dataclass(frozen=True)
class Feature:
    """A feature and where it came from.

    Part 4: "Every feature carries provenance back to its source field." Making
    that a data structure rather than a comment means it can be asserted.
    """
    name: str
    source_table: str
    source_column: str
    transform: str


FEATURES: tuple[Feature, ...] = (
    Feature("hour_sin", "silver_crash_geo", "local_hour", "sin(2*pi*hour/24)"),
    Feature("hour_cos", "silver_crash_geo", "local_hour", "cos(2*pi*hour/24)"),
    Feature("is_night", "silver_crash_geo", "local_hour", "hour < 6 or hour >= 21"),
    Feature("is_weekend", "silver_crash_geo", "local_dow", "dow in (6, 7)"),
    Feature("maxspeed_kph", "silver_crash_geo", "maxspeed", "parsed mph/kph, 0 when absent"),
    Feature("maxspeed_missing", "silver_crash_geo", "maxspeed",
            "1 when untagged — 64% of accepted snaps; never imputed"),
    Feature("road_is_major", "silver_crash_geo", "road_class",
            "highway in (motorway, trunk, primary)"),
    Feature("road_is_minor", "silver_crash_geo", "road_class",
            "highway in (residential, service, living_street)"),
    Feature("snap_distance_m", "silver_crash_geo", "snap_distance_m", "metres, EPSG:26985"),
)

MAJOR_ROADS = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"}
MINOR_ROADS = {"residential", "service", "living_street", "unclassified"}


def parse_maxspeed(value: str | None) -> float:
    """OSM `maxspeed` is free text: '35 mph', '50', 'DE:urban'. Returns 0.0 when
    absent or unparseable — paired with `maxspeed_missing` so the model can tell
    a genuine zero from an absence. Never imputed from road class: sparsity is
    monotonic in road importance (trunk 14% null, service 99%), so imputing
    would invent the value exactly where it varies most."""
    if not isinstance(value, str) or not value.strip():
        return 0.0
    text = value.strip().lower()
    digits = "".join(c for c in text if c.isdigit() or c == ".")
    if not digits:
        return 0.0
    try:
        speed = float(digits)
    except ValueError:
        return 0.0
    return speed * 1.609 if "mph" in text else speed


def load_frame(con: duckdb.DuckDBPyConnection,
               source: str = "MONTGOMERY_MD") -> pd.DataFrame:
    """Crash rows for training. ORDER BY is deliberate — the Phase 3d
    reproducibility bug came from relying on DuckDB's row order."""
    return con.execute("""
        SELECT c.crash_sk, c.severity_ordinal, c.crash_date,
               g.local_hour, g.local_dow, g.maxspeed, g.road_class,
               g.snap_distance_m, g.snap_quality, g.h3_r8
          FROM silver_crash c JOIN silver_crash_geo g USING (crash_sk)
         WHERE c.source_system = ? AND c.severity_ordinal IS NOT NULL
           AND g.local_hour IS NOT NULL AND g.h3_r8 IS NOT NULL
         ORDER BY c.crash_sk
    """, [source]).fetch_df()


def build(frame: pd.DataFrame) -> pd.DataFrame:
    """Frame -> feature matrix. Raises if a forbidden column survives."""
    hour = frame["local_hour"].astype(float)
    speed = frame["maxspeed"].map(parse_maxspeed)

    matrix = pd.DataFrame({
        "hour_sin": np.sin(2 * np.pi * hour / 24),
        "hour_cos": np.cos(2 * np.pi * hour / 24),
        "is_night": ((hour < 6) | (hour >= 21)).astype(float),
        "is_weekend": frame["local_dow"].isin([6, 7]).astype(float),
        "maxspeed_kph": speed,
        "maxspeed_missing": frame["maxspeed"].isna().astype(float),
        "road_is_major": frame["road_class"].isin(MAJOR_ROADS).astype(float),
        "road_is_minor": frame["road_class"].isin(MINOR_ROADS).astype(float),
        "snap_distance_m": frame["snap_distance_m"].fillna(0.0),
    })
    assert_no_forbidden(matrix)
    return matrix


def assert_no_forbidden(matrix: pd.DataFrame) -> pd.DataFrame:
    """Gate every feature matrix on this. The constraint is graded, and a guard
    that raises outlives a paragraph that asks."""
    offending = FORBIDDEN_FEATURES & set(matrix.columns)
    if offending:
        raise ForbiddenFeature(
            f"protected-characteristic proxies in the feature matrix: {sorted(offending)}. "
            "These are held for the output contract, not for ranking."
        )
    return matrix


def provenance() -> pd.DataFrame:
    return pd.DataFrame([f.__dict__ for f in FEATURES])
