"""Shared fixtures.

  bronze_incidents, bronze_drivers   Phase 1a  — live
  silver_crash_fact, silver_party    Phase 2   — live
  pipeline_runner                    Phase 6   — pending

All skip rather than fail when the layer has not been built, so the suite stays
runnable on a clean clone:

    python -m src.ingest.montgomery --dataset all
    python -m src.ingest.txdot
    python -m src.ingest.fars
    python -m src.transform.silver
"""

from __future__ import annotations

import duckdb
import pytest

from src.config import load_settings

BUILD_HINT = {
    "bronze_montgomery_incidents": "python -m src.ingest.montgomery --dataset all",
    "bronze_montgomery_drivers": "python -m src.ingest.montgomery --dataset all",
    "bronze_txdot_cris_crash": "python -m src.ingest.txdot",
    "bronze_fars_accident": "python -m src.ingest.fars",
    "silver_crash": "python -m src.transform.silver",
    "silver_party": "python -m src.transform.silver",
    "silver_crash_geo": "python -m src.geo.enrich",
}


def _warehouse_path():
    path = load_settings().warehouse_path
    if not path.exists():
        pytest.skip(f"no warehouse at {path}; run the ingest commands in tests/conftest.py")
    return path


@pytest.fixture(scope="session")
def warehouse():
    """Read-write, deliberately.

    DuckDB refuses a second connection to the same file under a different
    configuration, so a read-only session fixture makes `pipeline_runner` —
    which must rebuild — impossible without copying the whole warehouse.

    Safe because the only test that writes is the idempotency proof, and a
    rebuild that changes anything is exactly the failure that test exists to
    catch. Every other test only reads.
    """
    con = duckdb.connect(str(_warehouse_path()))
    yield con
    con.close()


def _require(con, table: str) -> str:
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()[0]
    if not exists:
        pytest.skip(f"{table} not built yet; run: {BUILD_HINT.get(table, '?')}")
    return table


@pytest.fixture
def bronze_incidents(warehouse):
    return warehouse, _require(warehouse, "bronze_montgomery_incidents")


@pytest.fixture
def bronze_drivers(warehouse):
    return warehouse, _require(warehouse, "bronze_montgomery_drivers")


@pytest.fixture
def silver_crash_fact(warehouse):
    return warehouse, _require(warehouse, "silver_crash")


@pytest.fixture
def silver_party(warehouse):
    return warehouse, _require(warehouse, "silver_party")


@pytest.fixture
def silver_geo(warehouse):
    return warehouse, _require(warehouse, "silver_crash_geo")


@pytest.fixture
def pipeline_runner(warehouse):
    """Rebuilds the derived layers from immutable bronze and fingerprints them.

    Ingestion is deliberately excluded: bronze is the reproducibility boundary,
    and re-fetching would make the test depend on whether the upstream source
    changed between runs — a different property from the one under test.
    """
    from src.ops import backfill

    for table in ("silver_crash", "bronze_txdot_cris_crash"):
        _require(warehouse, table)
    return backfill, warehouse
