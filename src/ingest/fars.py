"""Bronze-layer ingestion for NHTSA FARS annual bulk files.

    python -m src.ingest.fars                       # all configured years
    python -m src.ingest.fars --year 2024 --force
    python -m src.ingest.fars --tables accident,person

Restatement
-----------
NHTSA revises prior-year files in place. Measured 2026-09-04, all six years
2019-2024 carry 2026 mtimes in three separate batches, and 2021 was revised
alone two months after its neighbours — so neither "if year N changed, N-1 did
too" nor its converse holds.

Change is therefore detected per year, on the **ETag**, not on Last-Modified:
2022, 2023 and 2024 share a timestamp to the second, so Last-Modified cannot
even order them. ETags here are multipart (the `-4`/`-5` suffix is a part count)
so they are not locally computable, but they are stable per stored object and
change when it is rewritten. `fars_file_version` keeps the history, which is
both the skip-check and the restatement evidence for DATA_QUALITY.md.

Scope
-----
Each yearly ZIP holds 34 tables, ~389 MB uncompressed. Only `accident` is loaded
by default: it is the crash-level census, and it carries the coordinates and the
sentinel values. `person` (party grain) and the rest are one `--tables` flag
away if Phase 2 needs them. Loading all 34 for six years would be ~2.3 GB of
tables nothing models.

Sentinel coordinates (77.7777 / 88.8888 / 99.9999) are preserved verbatim here
and excluded at silver — bronze's job is faithfulness.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import requests

from src.config import Settings, load_settings

from . import bronze, http, watermark

log = logging.getLogger("ingest.fars")

SOURCE = "fars"
DEFAULT_TABLES = ("accident",)

# Natural keys. ST_CASE is unique only within a year, so the year is always part
# of the record id.
TABLE_KEYS = {
    "accident": ("ST_CASE",),
    "person": ("ST_CASE", "VEH_NO", "PER_NO"),
    "vehicle": ("ST_CASE", "VEH_NO"),
}


@dataclass
class YearResult:
    year: int
    status: str
    rows_fetched: int = 0
    rows_new: int = 0


def file_url(settings: Settings, year: int) -> str:
    return settings.sources["fars"]["bulk_pattern"].format(year=year)


def head_version(url: str, session: requests.Session) -> dict | None:
    """ETag/Last-Modified/size, or None if the year isn't published yet."""
    resp = session.head(url, timeout=60, allow_redirects=True)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return {
        "etag": resp.headers.get("ETag", ""),
        "last_modified": resp.headers.get("Last-Modified", ""),
        "content_length": int(resp.headers.get("Content-Length", 0)),
    }


def stored_etag(con: duckdb.DuckDBPyConnection, year: int) -> str | None:
    row = con.execute(
        "SELECT etag FROM fars_file_version WHERE year = ? ORDER BY last_seen DESC LIMIT 1",
        [year],
    ).fetchone()
    return row[0] if row else None


def record_version(con: duckdb.DuckDBPyConnection, year: int, version: dict) -> None:
    con.execute(
        """
        INSERT INTO fars_file_version
            (year, etag, last_modified, content_length, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (year, etag) DO UPDATE SET last_seen = excluded.last_seen
        """,
        [
            year, version["etag"], version["last_modified"], version["content_length"],
            watermark.utcnow(), watermark.utcnow(),
        ],
    )


def download(url: str, dest: Path, session: requests.Session) -> int:
    """Stream the ZIP to disk. It *is* the raw payload — no re-encoding."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    return dest.stat().st_size


def read_table(zip_path: Path, year: int, table: str) -> tuple[list[dict], list[str]]:
    """Extract one CSV and read it as all-VARCHAR.

    Two things vary across years and both have to be absorbed here:
      * archive layout — 2019 puts `accident.CSV` at the root, 2024 puts
        `FARS2024NationalCSV/accident.csv` in a subdirectory
      * case — `.CSV` vs `.csv`, `Person.CSV` vs `person.csv`
    So the match is on basename, case-insensitively.

    Encoding also varies: most years are latin-1, but 2021 is UTF-8 with a BOM.
    Decoding that as latin-1 turns the first column name into `ï»¿STATE`, which
    then lands as a *separate* bronze column from `STATE` and silently splits the
    field across years. utf-8-sig is tried first because it strips the BOM;
    latin-1 never raises, so it has to be the fallback rather than the default.
    """
    target = f"{table}.csv".lower()
    with zipfile.ZipFile(zip_path) as zf:
        matches = [n for n in zf.namelist() if Path(n).name.lower() == target]
        if not matches:
            raise FileNotFoundError(
                f"{table}.csv not in {zip_path.name} "
                f"(has: {sorted(Path(n).name for n in zf.namelist())[:5]}...)"
            )
        raw = zf.read(matches[0])

    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    text = text.lstrip("﻿")

    tmp = zip_path.parent / f"_{table}.csv"
    tmp.write_text(text, encoding="utf-8")
    try:
        con = duckdb.connect()
        df = con.execute(
            "SELECT * FROM read_csv(?, all_varchar=true, ignore_errors=true)", [str(tmp)]
        ).fetch_df()
        con.close()
    finally:
        tmp.unlink(missing_ok=True)

    fields = list(df.columns)
    records = df.where(df.notna(), None).to_dict("records")
    for rec in records:
        rec["_fars_year"] = str(year)
    return records, fields + ["_fars_year"]


def record_id(table: str, year: int, rec: dict) -> str:
    parts = [str(year)] + [str(rec.get(k, "")) for k in TABLE_KEYS.get(table, ("ST_CASE",))]
    return ":".join(parts)


def ingest_year(
    settings: Settings,
    con: duckdb.DuckDBPyConnection,
    year: int,
    *,
    tables: tuple[str, ...],
    session: requests.Session,
    force: bool,
    dry_run: bool,
) -> YearResult:
    url = file_url(settings, year)
    version = head_version(url, session)
    if version is None:
        log.info("%d: not published (404)", year)
        return YearResult(year, "NOT_PUBLISHED")

    previous = stored_etag(con, year)
    mb = version["content_length"] / 1_048_576
    log.info(
        "%d: %.1f MB, modified %s, etag %s",
        year, mb, version["last_modified"], version["etag"][:24],
    )

    if previous == version["etag"] and not force:
        record_version(con, year, version)  # refresh last_seen
        log.info("%d: unchanged since last run", year)
        return YearResult(year, "SKIPPED_UNCHANGED")

    if previous and previous != version["etag"]:
        log.warning("%d: RESTATED — file was rewritten since last ingest", year)

    if dry_run:
        return YearResult(year, "DRY_RUN")

    zip_path = settings.data_dir / "bronze" / "raw" / SOURCE / f"year={year}" / Path(url).name
    size = download(url, zip_path, session)
    (zip_path.parent / "_manifest.jsonl").open("a", encoding="utf-8").write(
        json.dumps({
            "year": year, "url": url, "bytes": size,
            "etag": version["etag"], "last_modified": version["last_modified"],
            "fetched_at": watermark.utcnow().isoformat(),
        }) + "\n"
    )

    result = YearResult(year, "SUCCESS")
    for table in tables:
        run_id = watermark.new_run_id()
        records, fields = read_table(zip_path, year, table)
        watermark.start_run(
            con, run_id=run_id, source=SOURCE, dataset=table,
            watermark_from=previous or "", watermark_to=version["etag"], scope=f"year={year}",
        )
        watermark.record_schema_snapshot(
            con, run_id=run_id, source=SOURCE, dataset=table, fields=fields,
        )

        for rec in records:
            rec["_record_id"] = record_id(table, year, rec)

        writer = bronze.BronzeWriter(
            con, f"bronze_{SOURCE}_{table}", fields,
            record_id_field="_record_id",
            updated_at_field=None,
            skip_fields=frozenset({"_record_id"}),
        )
        appended = writer.append(records, run_id=run_id)
        watermark.finish_run(
            con, run_id=run_id, status="SUCCESS",
            counters=watermark.RunCounters(
                pages=1, rows_fetched=appended.received,
                rows_new=appended.inserted, bytes_raw=size,
            ),
        )
        log.info(
            "%d/%s — %s rows, %s new", year, table,
            f"{appended.received:,}", f"{appended.inserted:,}",
        )
        result.rows_fetched += appended.received
        result.rows_new += appended.inserted

    record_version(con, year, version)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest NHTSA FARS bulk files")
    parser.add_argument("--year", type=int, action="append", help="repeatable; default: config")
    parser.add_argument("--tables", default=",".join(DEFAULT_TABLES),
                        help=f"comma-separated; default {','.join(DEFAULT_TABLES)}")
    parser.add_argument("--probe-next", action="store_true",
                        help="also probe the year after the configured range")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = load_settings()
    years = args.year or list(settings.sources["fars"]["years"])
    if args.probe_next:
        # A 404 on year N+1 is the steady state, not an error — FARS runs ~15
        # months behind. Probing keeps the range from going stale silently.
        years = years + [max(years) + 1]

    tables = tuple(t.strip() for t in args.tables.split(",") if t.strip())
    con = watermark.connect(settings.warehouse_path)
    session = http.build_session()

    results = []
    try:
        for year in sorted(years):
            results.append(
                ingest_year(settings, con, year, tables=tables,
                            session=session, force=args.force, dry_run=args.dry_run)
            )
    finally:
        con.close()

    print()
    for r in results:
        print(f"  {r.year}  {r.status:<18} rows={r.rows_fetched:>8,} new={r.rows_new:>8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
