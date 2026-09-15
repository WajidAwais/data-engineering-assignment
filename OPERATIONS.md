# Operations

Orchestration, storage, right-sizing and cost. Part 6 of the brief.

---

## Orchestration

**Dagster**, chosen over Airflow and Prefect for one specific reason: Part 6 asks *"how a
failed partition is recovered without a full rebuild"*, and Dagster's partitioned assets make
that a built-in rather than a bespoke backfill script.

```bash
dagster dev -f orchestration/definitions.py
```

### The graph

```
bronze_montgomery (daily) ─┐
bronze_txdot               ├─> silver_crash ─┬─> silver_contracts ─┐
bronze_fars                ┘                 │                     │
                                             ├─> drift_report      │
                                             │                     │
                                             └─> silver_crash_geo ─┼─> census_geography ─┐
                                                                   ├─> road_snapping ────┼─> gold_analysis
                                                                   │                     └─> gold_crash_risk ─┐
                                                                   └─────────────────────────────────────────┴─> gold_lead_decision
```

12 assets, 4 groups, scheduled daily at 06:00.

### Retry semantics differ by layer, deliberately

| Layer | Policy | Why |
|---|---|---|
| Ingest | 3 retries, exponential from 30 s | They cross a network, and every failure we actually hit was transient: Socrata throttling, Open-Meteo's volume quota, ArcGIS latency. |
| Transform / enrichment | **None** | Pure functions of local data. A failure is a bug or a contract violation; retrying reproduces it more slowly while obscuring when it started. |

### Failed-partition recovery

Only `bronze_montgomery` is daily-partitioned, because it is the only source with a genuine
incremental watermark. TxDOT republishes its layer wholesale and FARS is annual — partitioning
those by day would create thousands of partitions that each rebuild the same object.

Recovering one failed day:

```bash
dagster asset materialize -f orchestration/definitions.py \
  --select bronze_montgomery --partition 2026-09-04
```

This is safe rather than merely convenient, because of a Phase 1a decision: **the watermark
only advances after every page of a run commits.** A run that dies halfway leaves the
watermark untouched, so the retry redoes exactly the window that failed instead of skipping
past it. Content-hash deduplication then makes the redone pages free — they insert nothing.

### Contracts as a gate, drift as a signal

`silver_contracts` is an asset, not a check step, so `gold_lead_decision` cannot materialise
over a boundary that did not hold. `drift_report` is deliberately **non-fatal**: the December
2023 dictionary change was legitimate upstream behaviour, and failing the pipeline on it
would have blocked a correct load. Part 6 asks for an alert, and an alert is what it emits.

---

## Storage

### GeoParquet 1.1.0

Targeted explicitly — 2.0 is still a release candidate and GeoParquet remains an incubating
OGC standard. Files carry a `bbox` covering column with the 1.1.0 `covering` metadata entry,
which is what lets a reader prune row groups on ordinary Parquet statistics instead of
decoding geometry.

**Partitioning:** `source_system` / crash year. Both are how the data is actually filtered —
a jurisdiction-scoped compliance query never wants FARS's other 47 states, and every
statutory window is date-anchored.

**Measured:** 23 partitions, 360,056 rows, **24.4 MB**, 71 bytes/row. Median file 696 KB,
largest 2.8 MB.

### The bbox column does not pay for itself at this size — and the reason is counter-intuitive

Mean row-group bbox width, as a fraction of each file's full extent:

| Partition | Row groups | Mean group width |
|---|---|---|
| `NHTSA_FARS` / 2023 | 5 | **44.4%** |
| `MONTGOMERY_MD` / 2025 | 2 | 67.1% |
| `TXDOT_CRIS` / 2024 | 1 | 100% |

Two findings, and the second is the interesting one:

1. **A partition small enough to fit one row group cannot be pruned at all.** Most of ours
   are; the median file is under 700 KB.
2. **The national partition prunes better than the county one.** The intuition says a small
   geographic extent should give tight groups — but FARS spans a continent with dense urban
   clustering, so H3-sorted rows concentrate, while Montgomery's crashes spread fairly evenly
   across a small county so consecutive cells still cover much of its extent. What matters is
   how clustered the points are *relative to* the partition, not the absolute area.

Row groups are set to 8,000 rather than the conventional 50,000 specifically so that
partitions produce more than one group; rows are sorted by H3 cell before writing, because
without spatial ordering every group's bbox spans the whole file and pruning is theoretical.

Even so: at this scale the **partition scheme is doing essentially all the pruning**. The
covering column is implemented because the spec requires it and because it matters at
national multi-year volume — not because the measurement supports it here.

---

## Right-sizing

Single-node **DuckDB + GeoPandas**, which the brief calls the correct answer at this scale,
and the measurements agree:

| Stage | Rows | Wall clock |
|---|---|---|
| Montgomery full backfill | 352,569 | 2 m 39 s |
| FARS, 6 years | 222,695 | 1 m 40 s |
| TxDOT | 14,535 | 31 s |
| **silver build** | **362,402** | **2.3 s** |
| Geo enrichment | 360,056 | 9.6 s |
| Census point-in-polygon | 182,832 | 58 s |
| Road snapping | 125,058 | 22 s |
| Analysis + scoring | — | ~12 s |

Total footprint **1.25 GB**: 0.67 GB warehouse, 0.25 GB raw bronze, 0.30 GB static reference
data (OSM + TIGER), 26 MB GeoParquet.

The entire derived layer rebuilds in **under 3 seconds**. Nothing here is near a distributed
system's break-even point — the coordination overhead alone would exceed the current total
runtime.

### The escape hatch, named

Part 6 rewards naming it rather than pretending it does not exist: **Spark with Apache Sedona**
is the right tool for national multi-year scale, and here is where it starts to pay.

The binding constraint is not row count but **memory during the spatial joins**. The census
point-in-polygon holds 36,105 block-group polygons and 183k points simultaneously; road
snapping holds 172k line geometries. Extrapolating to all 50 states:

- ~220,000 census block groups nationally (≈6× current)
- ~30 M road segments nationally (≈175× the Maryland subset)
- FARS at 40 years rather than 6: ~1.4 M crashes
- All-severity data for all 50 states: **~10⁸ crash rows**, three orders of magnitude up

**The threshold is roughly 10⁷ rows or the point where the road network stops fitting in
memory** — whichever comes first, and on commodity hardware the road network fits first.
Below that DuckDB wins on simplicity and on not having a cluster to operate; above it, the
spatial joins stop fitting on one machine and Sedona's distributed spatial partitioning is
the answer rather than a bigger box.

We are at 590,288 bronze rows and 950,335 derived rows — about 1% of that threshold.

---

## Cost at 10× daily volume

10× current volume is ~5.9 M bronze rows, ~9.5 M derived. Still single-node.

| Item | Spec | Monthly |
|---|---|---|
| Compute | 1 × `t4g.xlarge` (4 vCPU, 16 GB), ~25 min/day | **$8** |
| Object storage | 15 GB raw + warehouse + exports, S3 standard | **$0.35** |
| Static reference | 3 GB OSM/TIGER, refreshed quarterly | **$0.07** |
| Egress | ~2 GB/month source pulls (ingress is free) | **$0.18** |
| Orchestration | Dagster OSS on the same instance | **$0** |
| **Total** | | **≈ $9 / month** |

Assumptions worth challenging: the instance runs ~25 min/day and is stopped otherwise (a
persistent instance is ~$100/month); Dagster Cloud would add ~$100/month and buys little at
one pipeline; and the figure excludes the **Census API key and Open-Meteo paid tier** —
Open-Meteo's free tier already proved insufficient for a full weather backfill (Phase 3d), so
a full-history weather join would add roughly $30/month at their commercial tier.

The dominant cost at this scale is not infrastructure. It is the engineer.
