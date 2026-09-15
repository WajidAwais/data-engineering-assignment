"""Durable ingestion state, kept in DuckDB rather than a JSON sidecar.

Three tables:

  ingestion_watermark      where each (source, dataset) got to
  ingestion_run            one row per run — the lineage record
  source_schema_snapshot   the schema as the source declared it, per run

The schema snapshot is captured now because it costs nothing at ingestion time
and is the entire evidence base for Part 6's schema-drift detector. The
`driver_substance_abuse` dictionary change is only detectable retrospectively if
someone recorded what the schema looked like before it happened.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

EPOCH = "1970-01-01T00:00:00.000Z"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingestion_watermark (
    source           VARCHAR NOT NULL,
    dataset          VARCHAR NOT NULL,
    watermark_field  VARCHAR NOT NULL,
    watermark_value  VARCHAR NOT NULL,
    last_run_id      VARCHAR,
    last_run_at      TIMESTAMPTZ,
    rows_seen        BIGINT DEFAULT 0,
    PRIMARY KEY (source, dataset)
);

CREATE TABLE IF NOT EXISTS ingestion_run (
    run_id          VARCHAR PRIMARY KEY,
    source          VARCHAR NOT NULL,
    dataset         VARCHAR NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    status          VARCHAR NOT NULL,
    watermark_from  VARCHAR,
    watermark_to    VARCHAR,
    pages           BIGINT DEFAULT 0,
    rows_fetched    BIGINT DEFAULT 0,
    rows_new        BIGINT DEFAULT 0,
    bytes_raw       BIGINT DEFAULT 0,
    scope           VARCHAR,
    error           VARCHAR
);

-- Added after the table shipped; keeps existing warehouses readable.
ALTER TABLE ingestion_run ADD COLUMN IF NOT EXISTS scope VARCHAR;

-- One row per (year, version) of a FARS bulk file. NHTSA revises prior years in
-- place, so this table is both the skip-check and the restatement evidence.
CREATE TABLE IF NOT EXISTS fars_file_version (
    year            INTEGER NOT NULL,
    etag            VARCHAR NOT NULL,
    last_modified   VARCHAR,
    content_length  BIGINT,
    first_seen      TIMESTAMPTZ NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (year, etag)
);

CREATE TABLE IF NOT EXISTS source_schema_snapshot (
    run_id       VARCHAR NOT NULL,
    source       VARCHAR NOT NULL,
    dataset      VARCHAR NOT NULL,
    fields_json  VARCHAR NOT NULL,
    types_json   VARCHAR,
    captured_at  TIMESTAMPTZ NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    """Sortable and greppable — it also names the raw-page directory on disk."""
    return f"{utcnow():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(_SCHEMA)
    return con


def read_watermark(con: duckdb.DuckDBPyConnection, source: str, dataset: str) -> str | None:
    row = con.execute(
        "SELECT watermark_value FROM ingestion_watermark WHERE source = ? AND dataset = ?",
        [source, dataset],
    ).fetchone()
    return row[0] if row else None


def commit_watermark(
    con: duckdb.DuckDBPyConnection,
    *,
    source: str,
    dataset: str,
    field: str,
    value: str,
    run_id: str,
    rows_seen: int,
) -> None:
    """Advance the watermark. Call only after every page has been committed —
    a run that dies halfway must be resumable from where it started, not from
    however far it happened to get."""
    con.execute(
        """
        INSERT INTO ingestion_watermark
            (source, dataset, watermark_field, watermark_value, last_run_id, last_run_at, rows_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, dataset) DO UPDATE SET
            watermark_field = excluded.watermark_field,
            watermark_value = excluded.watermark_value,
            last_run_id     = excluded.last_run_id,
            last_run_at     = excluded.last_run_at,
            rows_seen       = ingestion_watermark.rows_seen + excluded.rows_seen
        """,
        [source, dataset, field, value, run_id, utcnow(), rows_seen],
    )


@dataclass
class RunCounters:
    pages: int = 0
    rows_fetched: int = 0
    rows_new: int = 0
    bytes_raw: int = 0


def start_run(
    con: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    source: str,
    dataset: str,
    watermark_from: str,
    watermark_to: str,
    scope: str | None = None,
) -> None:
    """`scope` records the predicate this run extracted — without it, a bronze
    table holding several scopes cannot say which run contributed what."""
    con.execute(
        """
        INSERT INTO ingestion_run
            (run_id, source, dataset, started_at, status, watermark_from, watermark_to, scope)
        VALUES (?, ?, ?, ?, 'RUNNING', ?, ?, ?)
        """,
        [run_id, source, dataset, utcnow(), watermark_from, watermark_to, scope],
    )


def finish_run(
    con: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    status: str,
    counters: RunCounters,
    error: str | None = None,
) -> None:
    con.execute(
        """
        UPDATE ingestion_run
           SET finished_at = ?, status = ?, pages = ?, rows_fetched = ?,
               rows_new = ?, bytes_raw = ?, error = ?
         WHERE run_id = ?
        """,
        [
            utcnow(),
            status,
            counters.pages,
            counters.rows_fetched,
            counters.rows_new,
            counters.bytes_raw,
            error,
            run_id,
        ],
    )


def record_schema_snapshot(
    con: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    source: str,
    dataset: str,
    fields: list[str],
    types: list[str] | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO source_schema_snapshot
            (run_id, source, dataset, fields_json, types_json, captured_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [run_id, source, dataset, json.dumps(fields), json.dumps(types or []), utcnow()],
    )
