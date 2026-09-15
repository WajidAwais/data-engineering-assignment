"""Idempotent rebuild, and the proof that it is idempotent.

Part 6: *"A documented command that rebuilds any date range and produces
byte-identical output. Prove it."*

"Prove it" is the operative phrase, so the proof is a fingerprint comparison
rather than an assertion in prose: rebuild, hash the output, rebuild again, and
compare the hashes.

Why this holds structurally
--------------------------
Bronze is append-only and content-hash deduplicated, so re-ingesting changes
nothing unless the source changed. Silver is a full `CREATE OR REPLACE` that is
a pure function of bronze, with deterministic ordering and a content-hash
tie-break on version selection. Those two properties together are what make the
rebuild reproducible — neither alone is enough, which is why the Phase 2 grain
bug and the Phase 3d row-order bug both broke it.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass
from datetime import date

import duckdb

from src.config import load_settings
from src.ingest import watermark

log = logging.getLogger("ops.backfill")


@dataclass(frozen=True)
class Fingerprint:
    rows: int
    digest: str

    def __eq__(self, other) -> bool:
        return isinstance(other, Fingerprint) and \
            (self.rows, self.digest) == (other.rows, other.digest)


def fingerprint(con: duckdb.DuckDBPyConnection, table: str,
                key: str = "crash_sk",
                start: date | None = None, end: date | None = None,
                date_column: str = "crash_date") -> Fingerprint:
    """Content hash over a table, optionally windowed.

    Ordered by the key because a hash over an unordered aggregation is not
    reproducible — the same lesson the Phase 3d analysis bug taught.
    """
    where = ""
    params: list = []
    if start and end:
        where = f'WHERE "{date_column}" BETWEEN ? AND ?'
        params = [start, end]

    columns = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ? "
        "AND column_name NOT IN ('built_at', 'enriched_at', 'emitted_at') "
        "ORDER BY column_name", [table]).fetchall()]
    concat = " || '|' || ".join(f'coalesce(CAST("{c}" AS VARCHAR), chr(0))' for c in columns)

    rows, digest = con.execute(
        f'SELECT count(*), md5(string_agg({concat}, chr(10) ORDER BY "{key}")) '
        f'FROM "{table}" {where}', params).fetchone()
    return Fingerprint(rows=rows, digest=digest or "")


def rebuild(con: duckdb.DuckDBPyConnection) -> None:
    """Rebuild every derived layer from immutable bronze.

    Ingestion is deliberately NOT re-run: bronze is the reproducibility boundary,
    and re-fetching would make the test depend on whether the source changed
    between runs — which is a different property from the one being proved.
    """
    from src.transform import silver

    silver.build(con)


def prove(con: duckdb.DuckDBPyConnection, table: str = "silver_crash",
          start: date | None = None, end: date | None = None) -> dict:
    before = fingerprint(con, table, start=start, end=end)
    rebuild(con)
    after = fingerprint(con, table, start=start, end=end)
    return {"table": table, "before": before, "after": after,
            "identical": before == after, "window": (start, end)}


def amended_window(con: duckdb.DuckDBPyConnection) -> tuple[date, date]:
    """A date range containing restated TxDOT reports.

    `amend_supp_fl=1` marks a supplemental or amended report — 1,150 of the
    14,535 rows in scope (7.9%). Idempotency over a window with no restatements
    proves much less.
    """
    row = con.execute("""
        SELECT min(c.crash_date), max(c.crash_date)
          FROM silver_crash c
          JOIN bronze_txdot_cris_crash b ON b.crash_id = c.source_record_id
         WHERE c.source_system = 'TXDOT_CRIS' AND b.amend_supp_fl = '1'
    """).fetchone()
    return row[0], row[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild derived layers and prove idempotency")
    parser.add_argument("--table", default="silver_crash")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--amended", action="store_true",
                        help="use the date range containing restated TxDOT reports")
    parser.add_argument("--prove", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    con = watermark.connect(load_settings().warehouse_path)
    try:
        start, end = args.start, args.end
        if args.amended:
            start, end = amended_window(con)
            log.info("amended TxDOT window: %s .. %s", start, end)
        result = prove(con, args.table, start, end) if args.prove else None
        if result is None:
            rebuild(con)
            print("\n  rebuilt; pass --prove to compare fingerprints")
            return 0
    finally:
        con.close()

    before, after = result["before"], result["after"]
    print(f"\n  table   {result['table']}")
    if start:
        print(f"  window  {start} .. {end}")
    print(f"\n  before  {before.rows:>9,} rows  md5 {before.digest}")
    print(f"  after   {after.rows:>9,} rows  md5 {after.digest}")
    print(f"\n  {'IDENTICAL' if result['identical'] else 'DIVERGED'}\n")
    return 0 if result["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
