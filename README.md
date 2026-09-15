# Crash-to-Contact

A compliance-gated geospatial lead pipeline over three public motor-vehicle-crash sources.

Ingests Montgomery County MD (Socrata), TxDOT CRIS (ArcGIS) and NHTSA FARS (bulk), conforms
them to a single crash model, enriches them geospatially, applies a versioned eligibility
ruleset, scores the survivors, and emits records conforming to
`contracts/lead_output.schema.json`.

**The headline result is in [`MEMO.md`](MEMO.md): zero leads are lawfully contactable by live
outbound telephone in any of the three jurisdictions.** `output/sample_leads.csv` is
therefore a header row with no data rows. That is the pipeline's honest output, not a
failure to run it.

---

## Setup

Requires Python 3.12+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/settings.example.toml config/settings.toml
```

No API keys are required. `config/settings.toml` is gitignored and holds only a local HMAC
key for phone tokenisation; the example file ships with a placeholder that works for local
runs.

Census ACS is deliberately not used — see [`DECISIONS.md`](DECISIONS.md) 3.1 — so no Census
API key is needed anywhere.

---

## Quickstart (~5 minutes)

Ingests a bounded slice, builds every derived layer, and emits the lead feed.

```bash
# 1. Ingest all three sources         (~4m30s)
python -m src.ingest.montgomery --dataset all
python -m src.ingest.txdot
python -m src.ingest.fars

# 2. Conform and enrich               (~12s)
python -m src.transform.silver
python -m src.geo.enrich

# 3. Decide and emit                  (~2s)
python -m src.compliance.evaluate
python -m src.compliance.emit

# 4. Verify
python -m pytest tests/ -q
```

Expected at step 3: the exclusion table prints 40 records, all ineligible, and
`output/sample_leads.csv` is written with a header and no data rows.

At step 4, four tests report `xfailed` — that is intentional, see **Tests** below. Tests
covering layers this quickstart does not build (census, roads, analysis, scoring) skip rather
than fail. After the full pipeline below, the suite reports **257 passed, 4 xfailed**.

---

## Full pipeline

Roughly 8 minutes, plus ~290 MB of reference data (OSM extract, TIGER shapefiles).

```bash
python -m src.ingest.montgomery --dataset all --full-refresh   # ~2m40s, 352k rows
python -m src.ingest.txdot                                     # ~30s,    14k rows
python -m src.ingest.fars                                      # ~1m40s, 222k rows

python -m src.transform.silver                                 # ~2s
python -m src.geo.enrich                                       # ~10s
python -m src.geo.census                                       # ~60s  (downloads TIGER)
python -m src.geo.roads                                        # ~25s  (downloads OSM, 204MB)
python -m src.geo.analysis                                     # ~6s
python -m src.scoring.model                                    # ~6s
python -m src.scoring.score

python -m src.compliance.emit                                  # live telephone -> empty
python -m src.compliance.emit --channel written                # written        -> 29 rows
```

### Operations

```bash
python -m src.contracts.validate        # layer-boundary data contracts
python -m src.ops.drift --replay        # dictionary drift; fires on 2023-12
python -m src.ops.backfill --prove --amended   # byte-identical rebuild proof
python -m src.ops.geoparquet            # GeoParquet 1.1.0 export
dagster dev -f orchestration/definitions.py    # the asset graph
```

---

## Documents

| File | Contents |
|---|---|
| [`MEMO.md`](MEMO.md) | Findings and recommendation for a business audience |
| [`COMPLIANCE.md`](COMPLIANCE.md) | The eligibility ruleset, with citations, as prose |
| [`DATA_QUALITY.md`](DATA_QUALITY.md) | Every defect found, how it was detected, what the pipeline does |
| [`DECISIONS.md`](DECISIONS.md) | Design decisions and the alternatives rejected |
| [`ANALYSIS.md`](ANALYSIS.md) | Spatial analysis, parameters, and the scoring backtest |
| [`OPERATIONS.md`](OPERATIONS.md) | Orchestration, storage, right-sizing and cost |
| [`AI_USE.md`](AI_USE.md) | Where AI assistance was used |

---

## Layout

```
src/ingest/      one module per source; bronze layer, append-only
src/transform/   bronze -> silver; conformed crash model
src/geo/         CRS discipline, timezone, H3, census, roads, weather, analysis
src/compliance/  eligibility engine, calling windows, tokenisation vault, lineage
src/scoring/     lead prioritisation and the spatial-risk backtest
src/contracts/   layer-boundary contract validation
src/ops/         drift detection, idempotent backfill, GeoParquet export
contracts/       output JSON Schema and per-layer YAML contracts
compliance/      rules.yaml — the versioned ruleset; no legal constant lives in code
orchestration/   Dagster asset graph
output/          sample_leads.csv — synthetic only, no real PII
```

Storage is single-node DuckDB at `data/warehouse.duckdb`. All generated data is gitignored.

---

## Tests

```bash
python -m pytest tests/ -q      # 257 passed, 4 xfailed
```

The four `xfail` results are required by the exercise. `tests/test_known_defects.py` asserts
invariants that the **silver** layer satisfies, evaluated against **bronze**, where real data
violates them — 114 out-of-envelope coordinates, two coexisting code dictionaries, a 7-day
cutover overlap, and 785 crashes absent from the drivers table. Their silver counterparts in
`tests/test_silver_invariants.py` pass.

To see the four fail with live counts:

```bash
python -m pytest tests/test_known_defects.py --runxfail
```

---

## Scope

Three jurisdictions: Maryland, Texas, Florida. FARS is national, so crashes outside those
three carry census and road fields as null rather than a guess.

Road-network enrichment covers Maryland only. Weather is bounded to a recent window.
Socioeconomic data is not ingested at all. Each choice and its cost is recorded in
`DECISIONS.md`.
