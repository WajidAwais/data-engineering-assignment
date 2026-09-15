# Decisions

Design decisions and what was rejected.

---

## Phase 0 — Scaffolding (2026-09-04)

| Decision | Why |
|---|---|
| Kept the provided scaffold's layout and `contracts/lead_output.schema.json` unchanged | Not worth diverging from the expected shape |
| Added `pytest` to `requirements.txt` | The provided test file imports it; it was missing |

---

## Phase 1a — Ingestion infra + Montgomery County (2026-09-04)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 1.1 | Watermark on `:updated_at` | `crash_date_time` | Crash date trails publish date by ~2 days. A business-date watermark never sees records arriving for dates already loaded. |
| 1.2 | Keyset pagination on `:id` | `$offset` | `$offset` skips and repeats rows under concurrent writes. Keyset is immune by construction. |
| 1.3 | Freeze an upper bound `:updated_at <= T` at run start | Floor only | Without it, a row inserted mid-run below the cursor is unreachable and lost when the watermark advances. |
| 1.4 | Bronze append-only, deduped on content hash | MERGE in place | Re-runs write nothing, and every version is kept for Phase 2's SCD work. One mechanism, both requirements. |
| 1.5 | Exclude Socrata system fields from the hash | Hash whole record | A publisher reload rewrites every `:updated_at` without changing data. Excluding them means such a reload inserts 0 rows, not 220k. |
| 1.6 | Abort when a delta exceeds 50% of the table | Always proceed | A reload should be a deliberate decision, not something that quietly streams through the incremental path. |
| 1.7 | All bronze columns TEXT, cast at silver | Typed bronze | Socrata sends numbers as strings. Casting at ingestion fails the whole load on one bad value; casting at silver quarantines it. |
| 1.8 | Read schema from `X-SODA2-*` headers, snapshot per run | Infer from rows | Socrata omits null fields from rows, so a row-derived schema changes every run. Also seeds Phase 7's drift detector. |
| 1.9 | Commit the watermark only after all pages succeed | Per-page commits | A failed run re-does cheap work; a half-advanced watermark is a silent gap. |
| 1.10 | Retry 429/5xx; treat other 4xx as fatal | Retry everything | A malformed query never succeeds on retry — it just burns the rate budget and hides the bug. |
| 1.11 | Store raw pages gzipped, one per page, with a manifest | Store parsed only | Gzip is a container; decompression returns the exact bytes. Part 1 requires the raw payload preserved. |

---

## Phase 1b — TxDOT CRIS (2026-09-04)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 1b.1 | Pin the ID set with `returnIdsOnly`, then fetch by `ESRI_OID` range | `resultOffset` paging | Offset shifts under concurrent edits. Bounds derived from a pinned snapshot are deterministic. |
| 1b.2 | Range predicates, not an `objectIds=` list | ID list | A 2000-ID list is ~12.9 kB of URL, past what many servers accept. Ranges are short and equivalent. |
| 1b.3 | `crash_id` is the record key; `ESRI_OID` is a within-run cursor | `ESRI_OID` as key | OIDs are service-generated and reassigned when the layer is republished. Verified `crash_id` unique client-side (the service rejects `count_distinct` on it). |
| 1b.4 | Watermark on `editingInfo.lastEditDate`; unchanged = skip, changed = full re-extract | Incremental by date | The layer has no update timestamp. A full re-extract is cheap because unchanged rows insert nothing. Amendments surface as a changed hash on a stable `crash_id`. |
| 1b.5 | Default scope `bicyclist_involved_fl=1` | `1=1` | It is what the layer is named and described as publishing. The wider scope works via `--where`; whether it *should* be used is a MEMO.md argument, not a code default. |
| 1b.6 | Guard scopes over 100k rows behind `--allow-large` | Pull whatever is asked | The full table is 3.09M rows / ~1,545 batches. That should be deliberate, not a typo in `--where`. |
| 1b.7 | Every query sends `outSR=4326` | Accept the default | Geometry defaults to EPSG:3081, a projected Texas Lambert. Without it, coordinates are metres. |
| 1b.8 | Treat `exceededTransferLimit` as a fatal stale snapshot | Log and continue | Bounds come from the pinned ID set, so exceeding the limit means the layer moved mid-run and the batch is incomplete. |
| 1b.9 | Record the scope predicate on every run | Nothing | Without it, a bronze table that has held more than one scope cannot say which run contributed what. |

**Note:** ArcGIS reports failures as HTTP 200 with an `{"error": ...}` body. Unhandled,
a failed run is indistinguishable from an empty one — so the parser raises on it.

---

## Phase 1c — NHTSA FARS (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 1c.1 | Detect change per year on the **ETag** | `Last-Modified` | 2022/23/24 share a timestamp to the second, so it cannot even order them. ETag changes when the object is rewritten. |
| 1c.2 | `fars_file_version` keeps every version seen | Overwrite in place | It is both the skip-check and the restatement evidence for `DATA_QUALITY.md`. |
| 1c.3 | Load `accident` only by default | All 34 tables | Each yearly ZIP is ~389 MB over 34 tables; six years of everything is ~2.3 GB nothing models. `accident` is the crash-level census and carries the coordinates. `--tables` adds more when Phase 2 needs party grain. |
| 1c.4 | Record key is `{year}:{ST_CASE}` | `ST_CASE` alone | `ST_CASE` repeats across years — 2019 and 2024 both have `10001`. |
| 1c.5 | Match CSVs on basename, case-insensitively | Fixed path | Layout and case vary by year (root vs subdirectory, `.CSV` vs `.csv`). |
| 1c.6 | Decode `utf-8-sig` first, `latin-1` as fallback | latin-1 only | 2021 is UTF-8 with a BOM. latin-1 never raises, so it has to be the fallback, not the default. |
| 1c.7 | Skip the CrashAPI entirely | Use it as a supplement | Edge-blocked (HTTP 403, Akamai) for programmatic clients. Building on UA-spoofing past a WAF is not defensible. |
| 1c.8 | Year discovery probes N+1 | Hardcoded list | A 404 on the next year is the steady state given FARS's ~15-month lag, not an error. |

---

## Phase 2 — Bronze → Silver (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 2.1 | Silver is current-state over event-sourced bronze | SCD2 tables in silver | Bronze already retains every version by content hash. Part 2 names event-sourcing as an acceptable strategy; duplicating the history adds machinery without adding information. |
| 2.2 | Full `CREATE OR REPLACE` rebuild | Incremental merge | 2.3s for 362k rows. Makes silver a pure function of bronze, so reproducibility is checkable rather than asserted. |
| 2.3 | Tie-break latest-version selection on content hash | Order by `_ingested_at` alone | Two versions ingested in the same run share a timestamp and would order arbitrarily — the rebuild would differ run to run. |
| 2.4 | Normalisers dispatch on **value shape**, never date | Date-based cutover | The two encodings are both live 2023-12-28 → 2024-01-03, so no cutover date separates them. A test asserts the function body contains no date logic. |
| 2.5 | Old substance-abuse scheme maps drugs to `NULL`, not `False` | Infer `False` | `ALCOHOL PRESENT` says nothing about drugs. Inventing a negative would manufacture information the old dictionary never carried. |
| 2.6 | TxDOT severity mapping derived from injury counts | Assume `crash_sev_id` is ordinal | It is not: `5` is *no injury* and `1`/`3` are inverted. Code `4` matches `death_cnt>0` at exactly 451 rows, which anchors the derivation. |
| 2.7 | Crash severity = max party injury, falling back to report type | Report type alone | Party data gives full KABCO; the 3-level fallback cannot separate C/B/A. `severity_basis` marks which path each row took. |
| 2.8 | Bad geometry is **flagged**, never dropped | Filter out | A mis-geocoded crash is still a crash; dropping it understates the denominator. `geo_quality` ∈ OK / OUT_OF_ENVELOPE / SENTINEL / MISSING. |
| 2.9 | Sentinel detection by numeric cast, both scales | String equality on the documented values | Stored precision changes per year (`77.7777` vs `77.77770000`), so string equality finds 68 of 873 and looks like it worked. Longitude uses a different scale again. |
| 2.10 | Normalisers registered as DuckDB UDFs | Pull to pandas and apply | Keeps the transform set-based and the functions unit-testable in isolation. Needs `null_handling='special'` since `'N/A'` legitimately maps to NULL. |
| 2.11 | Crash dedup keys on the **business key** (`report_number` / `crash_id`), not the bronze record id | `_source_record_id` | Montgomery amends by delete-and-reinsert under a new `:id`, so keying on `:id` carries the crash twice and breaks its own grain. |
| 2.12 | Party dedup keeps rows whose `_ingested_at` is newest for `(report_number, person_id)` | Collapse on that pair; or key on `:id` | Same-run `person_id` collisions are *distinct people* (C4) and must both survive; later-run duplicates are amendments and must not. The ingestion timestamp is what separates the two cases. |

---

## Phase 3a — Geospatial foundation (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 3.1 | **ACS socioeconomic variables are not ingested at all** | Ingest and promise not to rank on them | Block-group income is a protected-characteristic proxy. Part 4 calls ranking on it redlining-adjacent; the strongest answer is not to hold the data. Costs the population-normalised Getis-Ord contrast — normalisation is by cell area, stated as such. |
| 3.2 | TIGER tract/block-group identifiers **are** ingested | Skip census geography entirely | The output contract requires `geo.census_tract` and `geo.census_bg`, and identifiers need no key. Geographic identifiers yes, socioeconomic attributes no — the same line DPPA §2725(3) draws around ZIP5. |
| 3.3 | `measurement_crs()` raises on 4326/3857 | A comment saying "don't use 3857" | The failure is invisible in output. A guard that raises beats one that asks nicely. |
| 3.4 | Timezone derived from coordinates | State or area code | Gets 2,030 crashes right that a state rule gets wrong. It is a compliance control, not a convenience. |
| 3.5 | DST gap tested **before** ambiguity | Ambiguity first | `fold` changes the UTC offset for both cases, so testing ambiguity first labels every spring-forward gap as ambiguous. Caught in development. |
| 3.6 | DST edges flagged, not silently resolved | Pick one and move on | 92 affected crashes. `tz_resolution` makes the subset queryable; a calling-window decision on an ambiguous timestamp should be visible. |
| 3.7 | Enrichment in a separate `silver_crash_geo` table | Columns on `silver_crash` | Re-runs independently of the transform, and a slow spatial join never blocks the fast path. |
| 3.8 | H3 call sites confined to one module | Call `h3.*` inline | h3 is at 4.x and v3's `geo_to_h3` raises rather than warning. One file to bump, with a test asserting the v4 API. |
| 3.9 | OSM road snapping scoped to Maryland | All three states (1.5 GB) | Same technique, demonstrated on the densest available data (125k crashes). TX/FL deferred and stated rather than attempted badly. |

---

## Phase 5 — Compliance gate (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 5.1 | Ruleset v1.1.0 models `live_telephone`, the channel the business asked for | A generic or best-case channel | The answer on that channel is zero eligible. Modelling an easier channel would answer a question nobody asked. |
| 5.2 | Every legal constant lives in `compliance/rules.yaml` or `blackout_windows.csv` | Constants in Python | Part 5 tests this live. A test walks the engine AST and fails if a statute number or time window appears in executable code. |
| 5.3 | Reason codes carry citations in a `CITATIONS` dict, not docstrings | `ReasonCode.X.__doc__` | Enum members silently discard a docstring written after the assignment, so `__doc__` is empty for all 31 and `legal_basis` was emitting "no citation recorded" on every decision. |
| 5.4 | Quote `"NO"` / `"YES"` in YAML | Bare tokens | YAML 1.1 parses bare `NO` as boolean `False`, so `safe_harbour_responses: [NO]` became `[False]` and the RND rule matched nothing. |
| 5.5 | Maryland blackout row is `days_from = 0` | Leave it blank, or omit MD | Maryland has no accident waiting period; the constraint is on the *method*. Zero days is correct and is not the same as "no restriction". |
| 5.6 | RND safe harbour attaches only to `"NO"` | Treat `NO_DATA` as passing | §64.1200(m) — the single most common engineering misreading of the RND. |
| 5.7 | Accumulate all applicable reason codes | Short-circuit on the first | The memo's exclusion table is a count by reason; a record blocked four ways must report four. |
| 5.8 | Analytic records carry the **NPA only**, never a full phone number | Carry `phone_e164` and exclude it at emit time | The calling-window fallback needs three digits. Structural separation beats remembering to exclude a field. |
| 5.9 | `phone_token` is HMAC-keyed | Plain SHA-256 | ~10^10 NANP numbers is a trivial rainbow table. An unkeyed hash is an encoding, not a token. |
| 5.10 | ZIP5 stays in the analytic record | Tokenise it with the rest | 18 U.S.C. §2725(3) expressly excludes ZIP5 from "personal information" — it is the legitimate aggregation key, and discarding the carve-out buys nothing. |
| 5.11 | `consent.require_provenance` defaults **false**, documented | Silently accept the fixture's boolean | The fixture supplies consent with no provenance. Flipping the flag takes the written channel from 25 eligible to **zero** — the honest measure of how thin a bare boolean is. |
| 5.12 | `sample_leads.csv` is header-only under live telephone | Emit ineligible records, or switch to the written channel | The brief blesses the empty file explicitly. A companion `sample_leads_written.csv` (29 rows) proves the pipeline emits conformant records when a lawful channel exists. |
| 5.13 | Derived dates anchor to the **frozen** evaluation date | `decision.evaluated_at` | Using the wall clock re-dated every DNC scrub to whenever the job ran, drifting the 31-day SLA out from under the decision made against it. |
| 5.14 | `BLOCKED_UNTIL` records stay in the feed; `INELIGIBLE` are withheld | Emit all, or emit only ELIGIBLE | The contract carries `blocked_until_date` for exactly this. An ineligible record is a decision, not a lead. |

---

## Phase 3b — Census geography (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 3b.1 | Fetch block-group shapefiles only; derive the tract from `bg[:11]` | Download both BG and tract files | GEOIDs nest exactly — verified against the authoritative MD tract file, 0 mismatches. Halves the download and the join. |
| 3b.2 | GeoPandas `sjoin` for the point-in-polygon | DuckDB spatial | `sjoin` builds an STRtree index; 182k points against 36k polygons without one is a nested loop. DuckDB stays the storage layer. |
| 3b.3 | Align TIGER's NAD83 to WGS84 before joining | Rely on the 1–2 m difference being negligible | It *is* negligible at block-group precision, but alignment is free. Note this is a containment test, not a measurement, so `crs.py`'s guard does not apply. |
| 3b.4 | Out-of-scope states get `null` | Nearest-polygon match | A wrong tract is worse than an absent one, and indistinguishable downstream. |
| 3b.5 | `census.lookup()` degrades to null when the TIGER cache is absent | Raise | A clean clone must still be able to emit. An absent tract is honest; a crashed emitter is not. |
| 3b.6 | Keep the rectangle envelope check *and* the tract join | Replace the envelope with a county polygon | They catch different things — the envelope finds wildly wrong coordinates cheaply, the tract join finds the 92 that are subtly wrong (DC, Virginia). Replacing one with the other would lose the cheap check. |

---

## Phase 3c — Road snapping (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 3c.1 | Read the Geofabrik extract with GDAL's OSM driver, bbox-clipped | `pyrosm`; Overpass | GDAL is already present — no new dependency. The bbox turns a 204 MB statewide file into a county network in 15 s. Overpass is capped at small ad-hoc queries by the brief. |
| 3c.2 | Parse `maxspeed`/`lanes`/`ref` out of GDAL's `other_tags` HSTORE | Ship a custom `osmconf.ini` | The driver promotes only `highway` and `name` to columns. Parsing the HSTORE is a documented quirk, not a workaround, and keeps the config surface at zero. |
| 3c.3 | Snap in EPSG:26985, enforced by `assert_measurable` | Snap in 4326 | A nearest-neighbour search in degrees returns degrees. 0.0003 looks like a small number. The 3a guard does real work here. |
| 3c.4 | Threshold at 50 m, derived from the distribution | A round number chosen up front | p99 is 31.7 m and p99.9 is 123.7 m; 50 m is the natural gap. Same approach as the TxDOT severity mapping — take the number from the data. |
| 3c.5 | Exclude footway/cycleway/path from candidates | Snap to any way | Snapping a car crash to a pavement produces a plausible distance and a wrong road class. |
| 3c.6 | Withhold `road_class` when the snap is rejected, but keep `snap_distance_m` | Emit the class with a quality flag | A class inherited from a segment 400 m away is fiction. The distance is kept because it is the evidence. |
| 3c.7 | Report `maxspeed`/`lanes` null rates; never impute | Fill from road class | Sparsity is monotonic in road importance (trunk 14% → service 99%), so imputation would invent the value precisely where it varies most. |
| 3c.8 | Report P020's measured 9.1 m rather than reproducing its `fixture_note` | Tune the network until the note holds | Fitting code to a fixture annotation rather than to the data is backwards. The rejection rule is demonstrated on 469 real crashes instead. |

---

## Phase 3d — Weather and spatial analysis (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 3d.1 | `ORDER BY` on every frame feeding a spatial statistic | Rely on DuckDB's row order | **Reproducibility bug.** Unseeded row order changes the weights-matrix construction order, which changes every permutation test. FDR hotspots swung 0 → 12 → 43 across identical runs, and Moran's I was depressed from 0.42 to ~0.21 — the unsorted frame was corrupting the spatial structure itself. |
| 3d.2 | 9,999 permutations, not 999 | The library default | `p_sim` cannot fall below 1/(perms+1). At 999 the floor is 1e-3, above the BH threshold LISA's 19 floor-valued cells needed (8.5e-4) — so the floor rejected them, not the evidence. The permutation count is not free once FDR is involved. |
| 3d.3 | Seed the permutation tests and report the seed | Leave them unseeded | An analysis that gives a different answer each run is not a finding. |
| 3d.4 | Drop island cells before the statistics run | Filter NaNs afterwards | A cell with no neighbours has a degenerate permutation distribution and divides by zero. Dropping up front is honest; filtering NaNs later hides how many were excluded. |
| 3d.5 | Crashes per **road-km** as the normalised denominator | Area; ACS population | ACS is deliberately not held (protected-characteristic proxy), and normalising equal-area hexagons by area is a no-op. Road length is a real exposure measure and arguably better than population for crash risk. |
| 3d.6 | De-duplicate segments in the road-length denominator | Sum `segment_length_m` per crash | Otherwise a busy road counts its segment once per crash, inflating its own denominator and masking its own hotspot. |
| 3d.7 | ST-DBSCAN with two thresholds | Single ε over rescaled coordinates | The shortcut lets a spatial gap be bought back by temporal closeness — the exact smear the brief warns about. |
| 3d.8 | ε_temporal = 90 days, chosen from a sweep | A round number | At one year the largest cluster holds 1,181 crashes: a map of where crashes happen, not a space-time cluster. |
| 3d.9 | Weather bounded to a recent window | Full 11.7-year backfill | Open-Meteo's free tier meters by data volume; 11.7 years × 4 variables × 30 cells exhausts the hourly budget. An operational pipeline enriching new crashes needs recent weather. |
| 3d.10 | 429 handled with a 62 s cooldown | Exponential backoff alone | A volume-metered API answering "try again in one minute" is not helped by jittered retries that top out well short of it. |

---

## Phase 4 — Lead scoring (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 4.1 | `FORBIDDEN_FEATURES` raises when a geographic or demographic identifier reaches the matrix | A documented policy of not ranking on geography | The constraint is graded, and a policy lasts only as long as institutional memory of it. Same shape as `crs.py`'s measurement guard. |
| 4.2 | Hold `census_tract`/`census_bg`/`zip5` for the contract; forbid them as features | Drop them, or allow them | Holding and ranking are different acts. ZIP5 is expressly carved out of DPPA "personal information" at §2725(3) and is the legitimate aggregation key. |
| 4.3 | Priority score is deterministic; only `spatial_risk` is modelled | One model producing the whole score | Severity and recency are **known** at scoring time. A model that predicts severity from features including severity is circular. |
| 4.4 | Logistic regression | Gradient boosting | Coefficients × standardised values *are* the named contributions. A better AUC in exchange for the blob Part 4 forbids is a bad trade. |
| 4.5 | `spatial_risk` weighted 0.10 | Weight it like the others | It backtests at AUC 0.574 — real but weak. Weighting a coin-flip highly would oversell it. |
| 4.6 | `h3_r8` is the CV blocking key and a forbidden feature | Use it as both | Using the held-out unit as a predictor leaks block identity into the model meant to hold it out. |
| 4.7 | Report both random and H3-blocked AUC | Report the blocked one only | The gap is the deliverable. Reporting one number hides whether it was checked. |
| 4.8 | Reject the cell-history feature | Ship it at +0.124 AUC | The gain is entirely leakage from encoding on the full dataset. Correctly encoded it adds nothing. |
| 4.9 | Score real crashes as well as fixture leads | Fixture only | Fixture parties have no crash link, so severity and spatial risk are unknown and their scores are recency-only. Scoring 125,058 real crashes shows the full decomposition on data that has the inputs. |
| 4.10 | Weights live in `compliance/rules.yaml` | Constants in Python | The relative worth of a severe lead versus a fresh one is a business judgement, and belongs with the other policy. |

---

## Phase 6a — Contracts, drift, idempotency (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 6.1 | Two drift detectors: schema **and** value-set | Schema only | The graded example — `driver_substance_abuse` — changed values, not schema. A schema detector passes it silently. |
| 6.2 | Watch the distinct-value **set**, not its cardinality | Count-based detection | The count holds at 9 across the whole transition, so a cardinality detector never fires on it — while firing 92 times on routine churn. Kept in the code as `cardinality_only()` so the comparison is testable. |
| 6.3 | Value-drift baseline is cumulative, not the prior period | Compare to the previous period | A token that appears, vanishes for a month and returns is not new. Alerting on it trains the operator to mute the detector. |
| 6.4 | Hand-rolled contracts in YAML | Great Expectations / Soda | Part 6 explicitly permits hand-rolling, and GE is a large dependency for type/null/range/referential checks. |
| 6.5 | A contract violation **fails**, never warns | Warn and continue | A contract that warns is documentation with extra steps. |
| 6.6 | Bronze contracts check structure, not value cleanliness | Assert clean values everywhere | Bronze is deliberately all-TEXT and faithful to the source; asserting cleanliness there would fail on correctly-preserved data. |
| 6.7 | Referential expectations are directional and documented | A generic FK check | `drivers → incidents` holds and the reverse does not. Getting this backwards fails on correct data. |
| 6.8 | Idempotency proven by fingerprint over an **amended** window | Any date range | A window with no restatements proves much less. TxDOT carries 1,150 amended rows. |
| 6.9 | The rebuild excludes ingestion | Re-run the whole pipeline | Bronze is the reproducibility boundary. Re-fetching would make the test depend on whether the upstream source changed — a different property. |
| 6.10 | The fingerprint excludes `built_at`/`enriched_at` | Hash every column | Those change on every rebuild by construction; including them makes the test permanently red for no signal. |
| 6.11 | The test `warehouse` fixture opens read-write | Read-only, copy for the rebuild test | DuckDB refuses two connections with different configurations, so read-only makes `pipeline_runner` impossible without copying the warehouse. Safe because the only writing test is the idempotency proof, and a rebuild that changes anything is exactly what it exists to catch. |

---

## Phase 6b — Orchestration, storage, cost (2026-09-15)

| # | Decision | Rejected | Why |
|---|---|---|---|
| 6.12 | Dagster | Airflow, Prefect | Part 6 asks how a failed partition recovers without a full rebuild. Dagster's partitioned assets make that a built-in; Airflow would need a scheduler and metadata DB for a single-node DuckDB pipeline, which is the over-engineering the right-sizing trap watches for. |
| 6.13 | Only `bronze_montgomery` is daily-partitioned | Partition everything by day | TxDOT republishes its layer wholesale and FARS is annual. Daily partitions there would be thousands of partitions rebuilding the same object. |
| 6.14 | Retry the ingest assets; never the transforms | One uniform policy | Ingest crosses a network and every observed failure was transient. A transform failure is a bug or a contract violation — retrying reproduces it more slowly and hides when it began. |
| 6.15 | `silver_contracts` is an asset, not a check step | A post-hoc validation job | As an asset it gates the gold layer: nothing downstream can materialise over a boundary that did not hold. |
| 6.16 | `drift_report` alerts and does not fail the run | Fail on drift | The December 2023 dictionary change was legitimate upstream behaviour. Failing on it would have blocked a correct load; Part 6 asks for an alert. |
| 6.17 | No `from __future__ import annotations` in the Dagster module | Keep it for consistency | Dagster resolves the `context` parameter type at runtime; postponed evaluation turns it into a string and it rejects it with a self-contradictory error. Noted in the file so nobody adds it back. |
| 6.18 | Row groups at 8,000, rows sorted by H3 | 50,000, insertion order | At 50,000 a typical partition is one row group and cannot be pruned at all; unsorted, every group's bbox spans the whole file and pruning is theoretical. Both are needed for the covering column to mean anything. |
| 6.19 | State plainly that the bbox column does not pay off at this size | Present it as an optimisation | Measured: the partition scheme does essentially all the pruning, and the *national* partition prunes better than the county one (44% vs 67% group width) because clustering matters more than extent. Implemented because the spec requires it and it matters at scale — claiming a benefit the measurement does not support would be overselling. |
| 6.20 | Name Spark/Sedona as the escape hatch with a threshold | Ignore it, or use it | Part 6 rewards naming it. The binding constraint is memory during spatial joins, not row count — roughly 10⁷ rows or when the road network stops fitting in memory. Current scale is ~1% of that. |

---

## Deferred, deliberately

- **Cross-source entity resolution** (Phase 3) — the overlap is now *quantified* (XS1: 97%
  of MD and 98% of TX fatal crashes duplicate into FARS) but not resolved. Doing it properly
  wants the spatial enrichment Phase 3 adds; matching on date and raw coordinates alone
  would mean redoing it.
- **`crash_date_time` localization** (Phase 3) — no timezone on the source value, so it must
  be localized rather than converted. Silver stores naive wall-clock.
- **Dimension tables for date / time / geography** (Phase 6, if the contracts want them) —
  the crash fact carries real timestamps and coordinates; a date dimension earns nothing in
  DuckDB yet.
- **GeoParquet output** (Phase 6) — a storage concern, not a transform concern.

---

## Phase 3a follow-up — verification fixes (2026-09-15)

| # | Decision | Why |
|---|---|---|
| 3.10 | Carry a `time_known` flag from `silver_crash` into `silver_crash_geo`; `local_hour` is NULL where the source time was a sentinel | 1,668 FARS rows fall back to midnight. Without the flag they are indistinguishable from genuine 00:00 crashes, and time-of-day feeds the Part 5c calling window. Day of week is retained — the date is sound. |
| 3.11 | Assert the cross-source overlap as a floor (>85%), not the measured 97/98% | The sources are live and the match is a proximity heuristic; pinning the exact value would rot. The floor still fails loudly if the universes stop overlapping. |
