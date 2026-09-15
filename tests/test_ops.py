"""Operability: contracts, drift detection, idempotent rebuild.

The interesting tests here are negative ones — that a contract can actually
fail, and that the drift detector people would reach for first does not work.
"""

from __future__ import annotations

import duckdb
import pytest
import yaml

from src.contracts import validate as C
from src.ops import backfill, drift


# --- contracts must be capable of failing -----------------------------------

@pytest.fixture
def toy():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE t (id VARCHAR, status VARCHAR, score INTEGER);
        INSERT INTO t VALUES ('a','OK',3), ('a','BAD',99), (NULL,'OK',1);
    """)
    yield con
    con.close()


def _contract(**overrides):
    base = {"table": "t", "layer": "test", "columns": {}}
    base.update(overrides)
    return base


def test_not_null_violation_is_detected(toy):
    v = C.validate(toy, [_contract(columns={"id": {"nullable": False}})])
    assert len(v) == 1 and "not_null" in v[0].check and v[0].count == 1


def test_allowed_values_violation_is_detected(toy):
    v = C.validate(toy, [_contract(columns={"status": {"allowed": ["OK"]}})])
    assert len(v) == 1 and v[0].count == 1


def test_range_violation_is_detected(toy):
    v = C.validate(toy, [_contract(columns={"score": {"max": 5}})])
    assert len(v) == 1 and "max" in v[0].check


def test_uniqueness_violation_is_detected(toy):
    v = C.validate(toy, [_contract(columns={"id": {"unique": True}})])
    assert any("unique" in x.check for x in v)


def test_grain_violation_is_detected(toy):
    v = C.validate(toy, [_contract(grain={"unique_by": ["id"]})])
    assert any(x.check == "grain" for x in v)


def test_missing_column_is_detected(toy):
    v = C.validate(toy, [_contract(columns={"nope": {"nullable": False}})])
    assert len(v) == 1 and "exists" in v[0].check


def test_a_satisfied_contract_produces_no_violations(toy):
    assert C.validate(toy, [_contract(columns={"status": {"allowed": ["OK", "BAD"]}})]) == []


# --- the shipped contracts --------------------------------------------------

def test_shipped_contracts_parse():
    contracts = C.load_contracts()
    assert len(contracts) >= 3
    for contract in contracts:
        assert contract["table"] and contract["layer"]


def test_referential_expectation_points_the_right_way():
    """drivers subset-of incidents holds; the reverse is false by design (785
    orphans, DATA_QUALITY A4). A contract asserting the reverse would fail on
    correct data, so the direction is the whole point."""
    bronze = next(c for c in C.load_contracts()
                  if c["table"] == "bronze_montgomery_incidents")
    check = next(e for e in bronze["expectations"]
                 if e["name"] == "drivers_reference_a_known_crash")
    sql = " ".join(check["sql"].split())
    assert "FROM bronze_montgomery_drivers" in sql
    assert sql.index("bronze_montgomery_drivers") < sql.index("EXCEPT")


def test_live_contracts_pass(warehouse):
    violations = C.validate(warehouse)
    assert violations == [], "\n".join(str(v) for v in violations)


# --- drift detection --------------------------------------------------------

def test_value_drift_fires_on_a_new_token():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE t (crash_date_time VARCHAR, col VARCHAR);
        INSERT INTO t VALUES
          ('2023-01-01T00:00:00','A'), ('2023-02-01T00:00:00','A'),
          ('2023-03-01T00:00:00','A'), ('2023-04-01T00:00:00','A'),
          ('2023-05-01T00:00:00','NEW');
    """)
    findings = drift.value_drift(con, "t", "col")
    con.close()
    assert len(findings) == 1
    assert findings[0].period == "2023-05" and findings[0].tokens == ["NEW"]


def test_value_drift_ignores_a_returning_token():
    """A value that appears, vanishes and returns is not new. Alerting on it
    trains the operator to mute the detector."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE t (crash_date_time VARCHAR, col VARCHAR);
        INSERT INTO t VALUES
          ('2023-01-01T00:00:00','A'), ('2023-01-02T00:00:00','B'),
          ('2023-02-01T00:00:00','A'), ('2023-03-01T00:00:00','A'),
          ('2023-04-01T00:00:00','A'), ('2023-05-01T00:00:00','B');
    """)
    findings = drift.value_drift(con, "t", "col")
    con.close()
    assert findings == []


def test_value_drift_needs_a_baseline():
    """Everything is novel in period one. Alerting there is noise."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE t (crash_date_time VARCHAR, col VARCHAR);
        INSERT INTO t VALUES ('2023-01-01T00:00:00','A'), ('2023-02-01T00:00:00','B');
    """)
    assert drift.value_drift(con, "t", "col") == []
    con.close()


def test_drift_fires_on_the_real_dictionary_change(warehouse):
    """The graded demonstration: Part 6 asks for the detector that would have
    caught the driver_substance_abuse change, shown firing on historical data."""
    findings = drift.value_drift(
        warehouse, "bronze_montgomery_drivers", "driver_substance_abuse")
    periods = {f.period for f in findings}
    assert "2023-12" in periods, f"expected an alert in 2023-12, got {sorted(periods)}"

    first = next(f for f in findings if f.period == "2023-12")
    assert any("," in token for token in first.tokens), (
        "the alert should name the new comma-joined pair tokens"
    )


def test_the_cardinality_detector_misses_it(warehouse):
    """Kept so the comparison is testable rather than asserted.

    The distinct-value count holds at 9 straight through the transition, so a
    cardinality detector never fires on the change — while firing ~92 times
    elsewhere on routine churn. Blind where it matters, noisy where it does not.
    """
    findings = drift.cardinality_only(
        warehouse, "bronze_montgomery_drivers", "driver_substance_abuse")
    periods = {f.period for f in findings}
    assert "2023-12" not in periods, "cardinality unexpectedly caught the change"
    assert "2024-01" not in periods
    assert len(findings) > 20, "and it is noisy enough to have been muted"


def test_schema_drift_would_not_have_caught_it(warehouse):
    """The column kept its name and type. Only its values changed."""
    findings = drift.schema_drift(warehouse)
    assert all("driver_substance_abuse" not in (f.detail or "") for f in findings)


# --- idempotent rebuild -----------------------------------------------------

def test_fingerprint_is_order_independent_of_insertion(warehouse):
    """Hashed with an explicit ORDER BY — an unordered aggregate is not
    reproducible, which is the Phase 3d lesson applied here."""
    a = backfill.fingerprint(warehouse, "silver_crash")
    b = backfill.fingerprint(warehouse, "silver_crash")
    assert a == b and a.rows > 0


def test_fingerprint_excludes_build_timestamps(warehouse):
    """built_at changes on every rebuild by construction. Including it would
    make the idempotency test permanently red."""
    import inspect
    assert "built_at" in inspect.getsource(backfill.fingerprint)


def test_amended_window_covers_restated_reports(warehouse):
    start, end = backfill.amended_window(warehouse)
    assert start is not None and end < end.replace(year=end.year + 1)
    amended = warehouse.execute(
        "SELECT count(*) FROM bronze_txdot_cris_crash WHERE amend_supp_fl = '1'"
    ).fetchone()[0]
    assert amended > 0


# --- GeoParquet -------------------------------------------------------------

def test_geoparquet_targets_1_1_0():
    """The brief is version-specific: 1.1.0 is stable, 2.0 is a release
    candidate, and the `covering` metadata entry only exists from 1.1.0."""
    from src.ops import geoparquet

    assert geoparquet.GEOPARQUET_VERSION == "1.1.0"


def test_geo_metadata_declares_a_covering_column():
    """Without `covering`, a reader sees an ordinary struct column and has no
    reason to consult it — the bbox column becomes decoration."""
    from src.ops import geoparquet

    meta = geoparquet.geo_metadata("geometry", (-77.5, 38.9, -76.9, 39.4))
    column = meta["columns"]["geometry"]
    assert meta["version"] == "1.1.0"
    assert column["covering"]["bbox"]["xmin"] == ["bbox", "xmin"]
    assert column["encoding"] == "WKB"
    assert column["crs"] == "EPSG:4326"


def test_row_group_size_permits_more_than_one_group():
    """At the conventional 50,000 a typical partition here is a single row
    group, and a reader can then only skip the whole file or none of it."""
    from src.ops import geoparquet

    assert geoparquet.ROW_GROUP_SIZE < 50_000


def test_export_is_spatially_sorted():
    """Pruning compares a query box against each group's extent. Unsorted, every
    group spans the whole partition and nothing is ever skipped."""
    import inspect

    from src.ops import geoparquet

    assert "ORDER BY h3_r8" in inspect.getsource(geoparquet.export)


def test_written_geoparquet_round_trips(tmp_path):
    import pandas as pd

    from src.ops import geoparquet

    frame = pd.DataFrame({
        "crash_sk": ["a", "b"], "latitude": [39.1, 39.2], "longitude": [-77.1, -77.2],
    })
    result = geoparquet.export_partition(frame, tmp_path / "part-0.parquet")
    assert result["rows"] == 2

    verified = geoparquet.verify(result["path"])
    assert verified["version"] == "1.1.0" and verified["has_covering"]
    assert verified["rows"] == 2


# --- orchestration ----------------------------------------------------------

def test_the_asset_graph_loads_and_is_wired():
    import warnings

    warnings.filterwarnings("ignore")
    from orchestration.definitions import defs

    assets = defs.get_repository_def().assets_defs_by_key
    names = {k.path[-1] for k in assets}
    assert {"bronze_montgomery", "silver_crash", "gold_lead_decision"} <= names

    spec = next(s for ad in assets.values() for s in ad.specs
                if s.key.path[-1] == "silver_crash")
    deps = {d.asset_key.path[-1] for d in spec.deps}
    assert deps == {"bronze_montgomery", "bronze_txdot", "bronze_fars"}


def test_only_the_incremental_source_is_partitioned():
    """TxDOT republishes wholesale and FARS is annual; partitioning those daily
    would create thousands of partitions rebuilding the same object."""
    import warnings

    warnings.filterwarnings("ignore")
    from orchestration.definitions import defs

    assets = defs.get_repository_def().assets_defs_by_key
    partitioned = {k.path[-1] for k, ad in assets.items() if ad.partitions_def}
    assert partitioned == {"bronze_montgomery"}


def test_network_assets_retry_and_pure_transforms_do_not():
    """A transform failure is a bug or a contract violation. Retrying it
    reproduces the failure more slowly while obscuring when it started."""
    import inspect

    from orchestration import definitions

    source = inspect.getsource(definitions)
    ingest_block = source[source.index("def bronze_montgomery"):source.index("def silver_crash")]
    transform_block = source[source.index("def silver_crash"):source.index("def silver_contracts")]
    assert "NETWORK_RETRY" in ingest_block
    assert "retry_policy" not in transform_block


def test_contracts_gate_the_gold_layer():
    """An asset rather than a check step, so nothing downstream can materialise
    over a boundary that did not hold."""
    import warnings

    warnings.filterwarnings("ignore")
    from orchestration.definitions import defs

    assets = defs.get_repository_def().assets_defs_by_key
    spec = next(s for ad in assets.values() for s in ad.specs
                if s.key.path[-1] == "gold_lead_decision")
    assert "silver_contracts" in {d.asset_key.path[-1] for d in spec.deps}
