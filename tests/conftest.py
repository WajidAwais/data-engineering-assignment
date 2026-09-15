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
}


def _warehouse_path():
    path = load_settings().warehouse_path
    if not path.exists():
        pytest.skip(f"no warehouse at {path}; run the ingest commands in tests/conftest.py")
    return path


@pytest.fixture(scope="session")
def warehouse():
    con = duckdb.connect(str(_warehouse_path()), read_only=True)
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
def pipeline_runner():
    pytest.skip("pipeline_runner not implemented yet (Phase 6 orchestration)")
