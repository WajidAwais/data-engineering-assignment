"""Bronze storage.

Two artefacts per run, per the Part 1 requirement to keep the raw payload
"byte-for-byte ... alongside the parsed form":

  data/bronze/raw/{source}/{dataset}/run_id=.../page_NNNN.json.gz
      The response body exactly as received. Gzip is a container only —
      decompressing returns the original bytes unchanged.
  data/warehouse.duckdb :: bronze_{source}_{dataset}
      The parsed rows, every business column as TEXT.

Bronze is append-only and versioned by content hash. Re-ingesting unchanged
data inserts nothing; a changed record appends a new version alongside the old.
That single mechanism covers both "re-running must not duplicate" (Part 1a) and
the version history Phase 2 needs for slowly-changing dimensions.

Everything stays TEXT here on purpose. Socrata serialises numbers as JSON
strings and TxDOT types 118 conceptually-integer columns as Double; casting at
ingestion would mean one bad value fails the load rather than being quarantined
downstream. Cast at bronze -> silver, where a rejection has somewhere to go.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

log = logging.getLogger(__name__)

# Socrata row-level system fields. Excluded from the content hash so that a
# publisher-side truncate-and-reload — which rewrites every :updated_at without
# changing any actual data — inserts zero new versions instead of 220k.
# ":@computed_region_*" is deliberately NOT in here: those are real values.
SOCRATA_SYSTEM_FIELDS = frozenset({":id", ":created_at", ":updated_at", ":version", ":meta"})

META_COLUMNS = (
    "_source_record_id",
    "_source_updated_at",
    "_content_hash",
    "_ingested_at",
    "_run_id",
    "_raw_record",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _scalarise(value: Any) -> str | None:
    """Flatten one field to TEXT. Nested objects (Socrata `location`) keep their
    JSON form rather than being dropped or exploded."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def content_hash(record: dict, *, exclude: frozenset[str] = SOCRATA_SYSTEM_FIELDS) -> str:
    """sha256 over canonical JSON of the business fields only.

    Null-valued and absent fields must hash identically — Socrata omits null
    fields from the payload entirely, so the same logical record can arrive with
    different key sets on different days.
    """
    payload = {
        k: _scalarise(v)
        for k, v in record.items()
        if k not in exclude and v is not None and v != ""
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RawWriter:
    """Byte-for-byte page capture plus a per-page provenance manifest."""

    def __init__(self, data_dir: Path, source: str, dataset: str, run_id: str) -> None:
        self.dir = data_dir / "bronze" / "raw" / source / dataset / f"run_id={run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "_manifest.jsonl"
        self._page = 0

    def write_page(
        self,
        body: bytes,
        *,
        url: str,
        params: dict | None,
        status: int,
        headers: dict[str, str],
        row_count: int,
    ) -> Path:
        self._page += 1
        path = self.dir / f"page_{self._page:04d}.json.gz"
        with gzip.open(path, "wb") as fh:
            fh.write(body)

        entry = {
            "page": self._page,
            "file": path.name,
            "url": url,
            "params": params or {},
            "status": status,
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "row_count": row_count,
            "fetched_at": utcnow().isoformat(),
            # Socrata's authoritative schema lives in headers, not in the rows.
            "response_headers": {
                k: v
                for k, v in headers.items()
                if k.lower().startswith(("x-soda2", "last-modified", "etag", "x-socrata-requestid"))
            },
        }
        with self.manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path


@dataclass
class AppendResult:
    received: int
    inserted: int

    @property
    def skipped(self) -> int:
        return self.received - self.inserted


class BronzeWriter:
    """Append-only, content-hash-deduplicated writes into one DuckDB table."""

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        table: str,
        business_fields: Iterable[str],
        *,
        record_id_field: str = ":id",
        # None where the source publishes no update timestamp at all (TxDOT).
        updated_at_field: str | None = ":updated_at",
        skip_fields: frozenset[str] = SOCRATA_SYSTEM_FIELDS,
    ) -> None:
        self.con = con
        self.table = table
        self.record_id_field = record_id_field
        self.updated_at_field = updated_at_field
        # System fields are already held in the meta columns; carrying them a
        # second time as business columns would just duplicate them.
        self.skip_fields = skip_fields
        self.business_fields: list[str] = [
            f for f in dict.fromkeys(business_fields) if f not in skip_fields
        ]
        self._create_table()

    @staticmethod
    def _q(identifier: str) -> str:
        """Quote an identifier. Required, not optional: Socrata emits
        `:@computed_region_*` column names and TxDOT has a field called `yield`."""
        return '"' + identifier.replace('"', '""') + '"'

    def _create_table(self) -> None:
        cols = [f"{self._q(c)} VARCHAR" for c in META_COLUMNS]
        cols += [f"{self._q(c)} VARCHAR" for c in self.business_fields]
        self.con.execute(f"CREATE TABLE IF NOT EXISTS {self._q(self.table)} ({', '.join(cols)})")
        self._ensure_columns(self.business_fields)

    def _existing_columns(self) -> set[str]:
        rows = self.con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [self.table],
        ).fetchall()
        return {r[0] for r in rows}

    def _ensure_columns(self, fields: Iterable[str]) -> list[str]:
        """Add columns that appeared after the table was created.

        A new column showing up mid-life is schema drift. Adding it keeps
        ingestion faithful; Phase 7's detector is what turns it into an alert,
        using the per-run snapshots in source_schema_snapshot.
        """
        existing = self._existing_columns()
        added = []
        for f in fields:
            if f not in existing:
                self.con.execute(
                    f"ALTER TABLE {self._q(self.table)} ADD COLUMN {self._q(f)} VARCHAR"
                )
                added.append(f)
        if added:
            log.warning("bronze %s: added %d new column(s): %s", self.table, len(added), added)
        return added

    def append(self, records: list[dict], *, run_id: str) -> AppendResult:
        if not records:
            return AppendResult(0, 0)

        # Fields can appear that weren't in the header-declared schema.
        seen_fields = {k for rec in records for k in rec}
        known = set(self.business_fields) | set(META_COLUMNS) | self.skip_fields
        new_business = [f for f in seen_fields if f not in known]
        if new_business:
            self.business_fields.extend(sorted(new_business))
            self._ensure_columns(sorted(new_business))

        ingested_at = utcnow().isoformat()
        rows = []
        for rec in records:
            row: dict[str, Any] = {
                "_source_record_id": _scalarise(rec.get(self.record_id_field)),
                "_source_updated_at": (
                    _scalarise(rec.get(self.updated_at_field))
                    if self.updated_at_field else None
                ),
                "_content_hash": content_hash(rec),
                "_ingested_at": ingested_at,
                "_run_id": run_id,
                "_raw_record": json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            }
            for f in self.business_fields:
                row[f] = _scalarise(rec.get(f))
            rows.append(row)

        columns = list(META_COLUMNS) + self.business_fields
        staging = pd.DataFrame(rows, columns=columns, dtype=object)

        col_list = ", ".join(self._q(c) for c in columns)
        self.con.register("_bronze_staging", staging)
        try:
            # Dedup within the batch first, then against what is already stored.
            # A record can legitimately appear twice in one run if the publisher
            # rewrote it between pages.
            # DuckDB returns the affected-row count as the result of an INSERT.
            inserted = self.con.execute(
                f"""
                INSERT INTO {self._q(self.table)} ({col_list})
                SELECT {col_list} FROM (
                    SELECT * FROM _bronze_staging
                    QUALIFY row_number() OVER (
                        PARTITION BY "_source_record_id", "_content_hash"
                    ) = 1
                ) s
                WHERE NOT EXISTS (
                    SELECT 1 FROM {self._q(self.table)} t
                    WHERE t."_source_record_id" IS NOT DISTINCT FROM s."_source_record_id"
                      AND t."_content_hash"      =                 s."_content_hash"
                )
                """
            ).fetchone()[0]
        finally:
            self.con.unregister("_bronze_staging")

        return AppendResult(received=len(records), inserted=int(inserted))
