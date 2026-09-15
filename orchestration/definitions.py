"""Dagster asset graph.

Part 6 asks for "the dependency graph, retry semantics, and how a failed
partition is recovered without a full rebuild". Dagster was chosen over Airflow
and Prefect for the third of those: partitioned assets are a first-class
concept, so recovering one failed day is `dagster asset materialize --partition
2026-09-04` rather than a bespoke backfill script.

    dagster dev -f orchestration/definitions.py

Shape of the graph
------------------
    bronze_montgomery ─┐
    bronze_txdot       ├─> silver_crash ─> silver_crash_geo ─┬─> gold_analysis
    bronze_fars        ─┘                                    ├─> gold_crash_risk
                                                             └─> gold_lead_decision

Retry policy differs by layer, deliberately:

  **Ingest assets retry.** They cross a network, and every failure mode we
  actually hit was transient or rate-limited — Socrata throttling, Open-Meteo's
  volume quota, ArcGIS latency.

  **Transform assets do not.** They are pure functions of local data. A failure
  there is a bug or a contract violation, and retrying it just produces the same
  failure more slowly while hiding when it started.
"""

# NOTE: no `from __future__ import annotations` here, deliberately. It turns
# annotations into strings, and Dagster resolves the `context` parameter type at
# runtime — with postponed evaluation it sees "AssetExecutionContext" as a bare
# string and rejects it with a confusingly self-contradictory error.
from dagster import (
    AssetExecutionContext,
    AssetKey,
    Backoff,
    DailyPartitionsDefinition,
    Definitions,
    RetryPolicy,
    ScheduleDefinition,
    asset,
    define_asset_job,
)

from src.config import load_settings
from src.ingest import watermark

# Montgomery publishes daily and is the only source with a real incremental
# watermark, so it is the one partitioned by day. TxDOT republishes wholesale
# and FARS is annual — partitioning those by day would create thousands of
# partitions that all rebuild the same object.
DAILY = DailyPartitionsDefinition(start_date="2026-09-01")

NETWORK_RETRY = RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL)


def _connection():
    return watermark.connect(load_settings().warehouse_path)


# --- bronze -----------------------------------------------------------------

@asset(group_name="bronze", partitions_def=DAILY, retry_policy=NETWORK_RETRY,
       description="Montgomery County crashes via Socrata. Incremental on :updated_at.")
def bronze_montgomery(context: AssetExecutionContext) -> None:
    """One partition per day.

    A failed day is recovered on its own — the watermark only advances after
    every page of a run commits, so a partial run leaves the next one to redo
    exactly the window that failed rather than skipping it.
    """
    from src.ingest import montgomery

    day = context.partition_key
    montgomery.main(["--dataset", "all", "--since", day])


@asset(group_name="bronze", retry_policy=NETWORK_RETRY,
       description="TxDOT CRIS via ArcGIS. Unpartitioned: the layer is republished whole.")
def bronze_txdot() -> None:
    from src.ingest import txdot

    txdot.main([])


@asset(group_name="bronze", retry_policy=NETWORK_RETRY,
       description="NHTSA FARS annual bulk files, versioned on ETag.")
def bronze_fars() -> None:
    from src.ingest import fars

    fars.main([])


# --- silver -----------------------------------------------------------------

@asset(group_name="silver", deps=[bronze_montgomery, bronze_txdot, bronze_fars],
       description="Conformed crash fact. Pure function of bronze — no retry.")
def silver_crash() -> None:
    from src.transform import silver

    con = _connection()
    try:
        silver.build(con)
    finally:
        con.close()


@asset(group_name="silver", deps=[silver_crash],
       description="Contract enforcement at the bronze->silver boundary.")
def silver_contracts(context: AssetExecutionContext) -> None:
    """Fails the run on violation.

    Placed as an asset rather than a check step so a downstream asset cannot
    materialise over a boundary that did not hold.
    """
    from src.contracts import validate

    con = _connection()
    try:
        violations = validate.validate(con)
    finally:
        con.close()
    if violations:
        raise ValueError(f"{len(violations)} contract violation(s): "
                         + "; ".join(str(v) for v in violations[:5]))
    context.log.info("all layer contracts passed")


# --- enrichment -------------------------------------------------------------

@asset(group_name="geo", deps=[silver_crash],
       description="Coordinate-derived timezone and H3 indexing. Feeds the calling window.")
def silver_crash_geo() -> None:
    from src.geo import enrich

    con = _connection()
    try:
        enrich.enrich(con)
    finally:
        con.close()


@asset(group_name="geo", deps=[silver_crash_geo], retry_policy=NETWORK_RETRY,
       description="TIGER census tract and block group.")
def census_geography() -> None:
    from src.geo import census

    census.run()


@asset(group_name="geo", deps=[silver_crash_geo], retry_policy=NETWORK_RETRY,
       description="OSM road snapping and linear referencing (Maryland).")
def road_snapping() -> None:
    from src.geo import roads

    roads.run()


# --- gold -------------------------------------------------------------------

@asset(group_name="gold", deps=[road_snapping, census_geography],
       description="Getis-Ord, Moran's I/LISA and ST-DBSCAN. Seeded and reproducible.")
def gold_analysis() -> None:
    from src.geo import analysis

    analysis.run()


@asset(group_name="gold", deps=[road_snapping],
       description="Spatial-risk model. Backtested under spatial blocking.")
def gold_crash_risk() -> None:
    from src.scoring import model

    model.run()


@asset(group_name="gold", deps=[silver_contracts, gold_crash_risk],
       description="Eligibility decisions and the contract-conformant lead feed.")
def gold_lead_decision() -> None:
    from src.compliance import emit

    emit.run()


@asset(group_name="ops", deps=[silver_crash],
       description="Schema and dictionary drift. Alerts rather than failing the run.")
def drift_report(context: AssetExecutionContext) -> None:
    """Deliberately non-fatal.

    Drift is information, not corruption: the December 2023 dictionary change
    was legitimate upstream behaviour. Failing the pipeline on it would have
    blocked a correct load; alerting is what Part 6 actually asks for.
    """
    from src.ops import drift

    con = _connection()
    try:
        findings = drift.run_all(con)
    finally:
        con.close()
    for finding in findings:
        context.log.warning(str(finding))
    context.add_output_metadata({"alerts": len(findings)})


daily_job = define_asset_job("daily_refresh", selection="*")

defs = Definitions(
    assets=[
        bronze_montgomery, bronze_txdot, bronze_fars,
        silver_crash, silver_contracts, silver_crash_geo,
        census_geography, road_snapping,
        gold_analysis, gold_crash_risk, gold_lead_decision, drift_report,
    ],
    jobs=[daily_job],
    schedules=[ScheduleDefinition(job=daily_job, cron_schedule="0 6 * * *")],
)
