"""Bronze-layer ingestion for Montgomery County crash data (Socrata / SODA).

Thin driver: the pagination and storage logic lives in socrata.py, bronze.py and
watermark.py so TxDOT and FARS reuse it rather than copying it.

    python -m src.ingest.montgomery --dataset all
    python -m src.ingest.montgomery --dataset incidents --since 2026-01-01

Note on --since: it is a floor on `:updated_at` (when the row was published),
not on `crash_date_time` (when the crash happened). Those differ by ~2 days and
the distinction is deliberate — see the module docstring in socrata.py.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import Settings, load_settings

from . import bronze, http, socrata, watermark

log = logging.getLogger("ingest.montgomery")

SOURCE = "montgomery"

# Config key in [montgomery] -> logical dataset name.
DATASETS = ("incidents", "drivers", "non_motorists")

# If a delta claims more than this share of the table, the publisher has almost
# certainly truncated and reloaded rather than genuinely changed every row.
# Observed precedent: all three datasets carry a min :updated_at of 2024-06-12,
# which is a reload artefact, not a creation date.
RELOAD_THRESHOLD = 0.5


class ReloadSuspected(RuntimeError):
    pass


@dataclass
class DatasetResult:
    dataset: str
    run_id: str
    status: str
    rows_fetched: int
    rows_new: int
    pages: int


def _floor_for(
    con, dataset: str, since: str | None, full_refresh: bool
) -> str:
    if full_refresh:
        return watermark.EPOCH
    if since:
        dt = datetime.fromisoformat(since)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return socrata.format_ts(dt.astimezone(timezone.utc))
    stored = watermark.read_watermark(con, SOURCE, dataset)
    return stored or watermark.EPOCH


def ingest_dataset(
    settings: Settings,
    con,
    dataset: str,
    *,
    since: str | None,
    page_size: int,
    full_refresh: bool,
    allow_full_reload: bool,
    dry_run: bool,
) -> DatasetResult:
    moco = settings.sources["montgomery"]
    base, dataset_id = moco["base"], moco[dataset]
    session = http.build_session(settings.socrata_app_token or moco.get("app_token", ""))

    run_id = watermark.new_run_id()
    floor = _floor_for(con, dataset, since, full_refresh)
    ceiling = socrata.format_ts(datetime.now(timezone.utc))

    fields, types = socrata.fetch_schema(base, dataset_id, session=session)
    log.info("%s/%s: %d fields declared by source", dataset, dataset_id, len(fields))

    delta_where = socrata.build_where(floor, ceiling, None)
    delta = socrata.count_rows(base, dataset_id, where=delta_where, session=session)
    total = socrata.count_rows(base, dataset_id, session=session)
    log.info(
        "%s: %s rows pending of %s total (watermark %s -> %s)",
        dataset, f"{delta:,}", f"{total:,}", floor, ceiling,
    )

    cold_start = floor == watermark.EPOCH
    if not cold_start and total and (delta / total) > RELOAD_THRESHOLD and not allow_full_reload:
        raise ReloadSuspected(
            f"{dataset}: delta is {delta:,} of {total:,} rows ({delta / total:.0%}). "
            "That is a publisher-side reload, not an incremental change. "
            "Re-run with --allow-full-reload to proceed deliberately."
        )

    if dry_run:
        log.info("%s: dry run, stopping before any write", dataset)
        return DatasetResult(dataset, run_id, "DRY_RUN", delta, 0, 0)

    watermark.start_run(
        con, run_id=run_id, source=SOURCE, dataset=dataset,
        watermark_from=floor, watermark_to=ceiling, scope=delta_where,
    )
    watermark.record_schema_snapshot(
        con, run_id=run_id, source=SOURCE, dataset=dataset, fields=fields, types=types,
    )

    raw = bronze.RawWriter(settings.data_dir, SOURCE, dataset, run_id)
    writer = bronze.BronzeWriter(con, f"bronze_{SOURCE}_{dataset}", fields)
    counters = watermark.RunCounters()

    try:
        for page in socrata.paginate(
            base, dataset_id,
            floor=floor, ceiling=ceiling, page_size=page_size, session=session,
        ):
            raw.write_page(
                page.response.body,
                url=page.response.url,
                params=page.params,
                status=page.response.status,
                headers=page.response.headers,
                row_count=len(page.records),
            )
            result = writer.append(page.records, run_id=run_id)

            counters.pages += 1
            counters.rows_fetched += result.received
            counters.rows_new += result.inserted
            counters.bytes_raw += len(page.response.body)
            log.info(
                "%s: page %d — %s fetched, %s new (%s already held)",
                dataset, counters.pages, f"{result.received:,}",
                f"{result.inserted:,}", f"{result.skipped:,}",
            )
    except Exception as exc:
        watermark.finish_run(con, run_id=run_id, status="FAILED", counters=counters, error=str(exc))
        raise

    # Only now, with every page committed. A partial run must be resumable from
    # where it started, not from wherever it happened to stop.
    watermark.commit_watermark(
        con, source=SOURCE, dataset=dataset,
        field=socrata.UPDATED_AT_FIELD, value=ceiling,
        run_id=run_id, rows_seen=counters.rows_fetched,
    )
    watermark.finish_run(con, run_id=run_id, status="SUCCESS", counters=counters)

    return DatasetResult(
        dataset, run_id, "SUCCESS", counters.rows_fetched, counters.rows_new, counters.pages
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest Montgomery County crash data")
    parser.add_argument("--dataset", default="all", choices=(*DATASETS, "all"))
    parser.add_argument("--since", help="ISO date/datetime; floor on :updated_at, not crash date")
    parser.add_argument("--page-size", type=int, default=socrata.DEFAULT_PAGE_SIZE)
    parser.add_argument("--full-refresh", action="store_true", help="ignore the stored watermark")
    parser.add_argument("--allow-full-reload", action="store_true", help="proceed past the reload guard")
    parser.add_argument("--dry-run", action="store_true", help="report volumes, write nothing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = load_settings()
    targets = list(DATASETS) if args.dataset == "all" else [args.dataset]
    con = watermark.connect(settings.warehouse_path)

    results: list[DatasetResult] = []
    try:
        for dataset in targets:
            results.append(
                ingest_dataset(
                    settings, con, dataset,
                    since=args.since,
                    page_size=args.page_size,
                    full_refresh=args.full_refresh,
                    allow_full_reload=args.allow_full_reload,
                    dry_run=args.dry_run,
                )
            )
    except ReloadSuspected as exc:
        log.error("%s", exc)
        return 2
    finally:
        con.close()

    print()
    for r in results:
        print(
            f"  {r.dataset:<14} {r.status:<8} "
            f"pages={r.pages:<4} fetched={r.rows_fetched:>8,} new={r.rows_new:>8,}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
