"""Bronze-layer ingestion for TxDOT CRIS via the public ArcGIS FeatureServer.

    python -m src.ingest.txdot                      # bicyclist scope (default)
    python -m src.ingest.txdot --scope all --allow-large
    python -m src.ingest.txdot --where "crash_date >= '2024-01-01'"

Scope
-----
Defaults to `bicyclist_involved_fl=1` (14,535 rows) — what the layer is named and
described as publishing. The same layer answers `where=1=1` with 3,088,450 rows,
the full statewide `cris_crash` table, which is plainly more than the label
claims. `--scope all` is available and the paging handles it; whether it *should*
be used is argued in MEMO.md, not decided here.

Incrementality
--------------
There is none, by the source's nature. The layer carries no update timestamp, and
`ESRI_OID` is reassigned on republication, so neither can anchor a watermark.
What it does carry is `editingInfo.lastEditDate`, which moves when the layer is
republished (last: 2025-12-22). So:

  unchanged lastEditDate -> skip entirely, no requests spent
  changed                -> re-extract the scope in full

A full re-extract sounds expensive but is not, because bronze dedupes on content
hash: an unchanged row inserts nothing. Amendments (`amend_supp_fl`, ~7.9% of the
bicyclist subset) surface as a changed hash on a stable `crash_id`, which is
exactly the restatement signal Phase 2 needs.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from src.config import Settings, load_settings

from . import arcgis, bronze, http, watermark

log = logging.getLogger("ingest.txdot")

SOURCE = "txdot"
DATASET = "cris_crash"

SCOPES = {
    "bicyclist": "bicyclist_involved_fl=1",
    "all": "1=1",
}

# Above this, require --allow-large. The full table is 3.09M rows / ~1,545
# batches; that should be a deliberate choice, not a typo in --where.
LARGE_SCOPE_THRESHOLD = 100_000

# ESRI_OID is volatile across republications and is not source data.
ARCGIS_SYSTEM_FIELDS = frozenset({arcgis.OID_FIELD})


class ScopeTooLarge(RuntimeError):
    pass


@dataclass
class Result:
    run_id: str
    status: str
    rows_fetched: int
    rows_new: int
    batches: int


def ingest(
    settings: Settings,
    con,
    *,
    where: str,
    batch_size: int,
    force: bool,
    allow_large: bool,
    max_batches: int | None,
    dry_run: bool,
) -> Result:
    layer_url = settings.sources["txdot"]["feature_server"]
    session = http.build_session()
    run_id = watermark.new_run_id()

    metadata = arcgis.layer_metadata(layer_url, session=session)
    fields, types = arcgis.field_schema(metadata)
    edit_date = arcgis.last_edit_date(metadata)
    stored = watermark.read_watermark(con, SOURCE, DATASET)

    log.info(
        "layer %r: %d fields, lastEditDate=%s (stored=%s)",
        metadata.get("name"), len(fields), edit_date, stored,
    )

    if stored and edit_date and stored == edit_date and not force:
        log.info("layer unchanged since last run; nothing to do (--force to override)")
        return Result(run_id, "SKIPPED_UNCHANGED", 0, 0, 0)

    total = arcgis.count_rows(layer_url, where, session=session)
    log.info("scope %r matches %s rows", where, f"{total:,}")

    if total > LARGE_SCOPE_THRESHOLD and not allow_large and max_batches is None:
        raise ScopeTooLarge(
            f"scope matches {total:,} rows (~{-(-total // batch_size):,} batches). "
            "Pass --allow-large to pull it, or --max-batches N to bound it."
        )

    object_ids = arcgis.fetch_object_ids(layer_url, where, session=session)
    log.info("pinned %s object ids", f"{len(object_ids):,}")
    if len(object_ids) != total:
        log.warning(
            "id count (%s) != count query (%s); the layer moved between the two calls",
            f"{len(object_ids):,}", f"{total:,}",
        )

    if dry_run:
        log.info("dry run, stopping before any write")
        return Result(run_id, "DRY_RUN", len(object_ids), 0, 0)

    watermark.start_run(
        con, run_id=run_id, source=SOURCE, dataset=DATASET,
        watermark_from=stored or "", watermark_to=edit_date or "", scope=where,
    )
    watermark.record_schema_snapshot(
        con, run_id=run_id, source=SOURCE, dataset=DATASET, fields=fields, types=types,
    )

    raw = bronze.RawWriter(settings.data_dir, SOURCE, DATASET, run_id)
    writer = bronze.BronzeWriter(
        con, f"bronze_{SOURCE}_{DATASET}", fields,
        # crash_id survives republication; ESRI_OID does not. No update timestamp exists.
        record_id_field=arcgis.BUSINESS_KEY,
        updated_at_field=None,
        skip_fields=ARCGIS_SYSTEM_FIELDS,
    )
    counters = watermark.RunCounters()

    try:
        for batch in arcgis.iter_batches(
            layer_url, where=where, object_ids=object_ids,
            batch_size=batch_size, session=session, max_batches=max_batches,
        ):
            raw.write_page(
                batch.response.body,
                url=batch.response.url,
                params=batch.params,
                status=batch.response.status,
                headers=batch.response.headers,
                row_count=len(batch.records),
            )
            appended = writer.append(batch.records, run_id=run_id)

            counters.pages += 1
            counters.rows_fetched += appended.received
            counters.rows_new += appended.inserted
            counters.bytes_raw += len(batch.response.body)
            log.info(
                "batch %d (oid %d-%d) — %s fetched, %s new",
                counters.pages, batch.lo, batch.hi,
                f"{appended.received:,}", f"{appended.inserted:,}",
            )
    except Exception as exc:
        watermark.finish_run(con, run_id=run_id, status="FAILED", counters=counters, error=str(exc))
        raise

    # Only advance if the whole scope was covered. A bounded run must not claim
    # the layer has been fully ingested at this edit date.
    partial = max_batches is not None and counters.pages >= max_batches
    if edit_date and not partial:
        watermark.commit_watermark(
            con, source=SOURCE, dataset=DATASET,
            field="editingInfo.lastEditDate", value=edit_date,
            run_id=run_id, rows_seen=counters.rows_fetched,
        )
    elif partial:
        log.warning("partial run (--max-batches); watermark left unchanged")

    watermark.finish_run(con, run_id=run_id, status="SUCCESS", counters=counters)
    return Result(run_id, "SUCCESS", counters.rows_fetched, counters.rows_new, counters.pages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest TxDOT CRIS crash data")
    parser.add_argument("--scope", default="bicyclist", choices=sorted(SCOPES))
    parser.add_argument("--where", help="raw predicate; overrides --scope. ISO date literals only")
    parser.add_argument("--batch-size", type=int, default=arcgis.DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, help="bound the run for testing")
    parser.add_argument("--force", action="store_true", help="re-extract even if unchanged")
    parser.add_argument("--allow-large", action="store_true", help="permit a >100k-row scope")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = load_settings()
    con = watermark.connect(settings.warehouse_path)
    where = args.where or SCOPES[args.scope]

    try:
        result = ingest(
            settings, con,
            where=where, batch_size=args.batch_size, force=args.force,
            allow_large=args.allow_large, max_batches=args.max_batches, dry_run=args.dry_run,
        )
    except ScopeTooLarge as exc:
        log.error("%s", exc)
        return 2
    finally:
        con.close()

    print(
        f"\n  txdot/{DATASET:<12} {result.status:<18} "
        f"batches={result.batches:<4} fetched={result.rows_fetched:>8,} new={result.rows_new:>8,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
