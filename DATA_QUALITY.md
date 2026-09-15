# Data Quality

What was found, how it was detected, what the pipeline does.

**Status:** three sources ingested, silver built. Montgomery and TxDOT measured
**2026-09-04** (125,005 incidents / 220,043 drivers / 7,521 non-motorists / 14,535 TxDOT
crashes); FARS and silver measured **2026-09-15** (222,695 FARS rows; 362,402
`silver_crash`, 227,877 `silver_party`).

Montgomery figures are ~11 days stale as of the silver build; re-running the incremental
path refreshes them.

Counts are a snapshot of a live source. Tests assert `violations == 0`, never a fixed
number.

---

## A. Source data defects

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| A1 | **114 crashes outside Montgomery County**, with 0 nulls and 0 zero coordinates. Furthest is ~160 km away in Garrett County. | Bounds check against the county envelope (lat 38.9–39.36, lon −77.54 to −76.87) | Flags, does not drop. Excluded from spatial aggregates, kept in crash counts. Deleting a mis-geocoded crash understates the denominator. |
| A2 | **`driver_substance_abuse` holds two encodings** — old uppercase single values, new comma-joined pairs — and **four spellings of unknown** (`NULL`, `N/A`, `UNKNOWN`, `Unknown, Unknown`). | Distinct-value profile | Branches on the value's shape. Silver splits the pair into separate alcohol/drug columns and collapses all four unknowns to `NULL`. |
| A3 | **The two encodings overlap for 7 days**, 2023-12-28 → 2024-01-03. Both are live on the same crash dates. | Per-day counts of each encoding, filtered to days where both appear | The parser never looks at the date, so the overlap is a non-event. A hardcoded cutover would misclassify both sides. |
| A4 | **785 crashes have no driver row** (drivers ⊆ incidents; the reverse is false). An inner join drops them. | Anti-join on `report_number`, both directions | Incidents is the crash spine; joins to drivers are LEFT. The integrity check runs drivers→incidents only. |
| A5 | **Grain fan-out 1.77×** — 220,043 driver rows over 124,220 crashes, carrying denormalised crash-level fields. | Row count per `report_number` | Crash fact is built from incidents at one row per `report_number`. Crash-level attributes never come from drivers. |
| A6 | **`collision_type` has the same dual-dictionary problem** (`SINGLE VEHICLE` vs `Single Vehicle`). Not mentioned in the brief. | Found while profiling A4 | Case-folded in silver alongside A2. Suggests the drift detector should sweep every categorical, not just the named ones. |
| A7 | **`injury_severity` is the third column hit by the same migration** — `FATAL INJURY` vs `Fatal Injury` across all five KABCO levels, plus `None` as a literal string (3,507 rows). Not mentioned in the brief either. | Distinct-value profile while building the severity crosswalk | Case-folded to a single ordinal; `None` joins the other absent spellings. |

**On A4 — the dropped crashes are not a random sample.** Against a 0.63% baseline,
`collision_type='UNKNOWN'` orphans at 14.4% and single-vehicle at ~1.4–1.7%. 14.1% of them
appear in the non-motorists table against a 5.7% baseline. An inner join would bias against
pedestrian and cyclist crashes specifically.

---

## XS. Cross-source findings (silver)

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| XS1 | **The universes overlap almost completely for fatal crashes.** Within the years FARS covers, **97%** of Montgomery fatal crashes (194/200) and **98%** of TxDOT fatal crashes (443/451) match a FARS row on same-date + ~500 m. | Self-join on `crash_date` with a coordinate window | ~637 duplicate crashes. A naive `UNION ALL` double-counts every fatal crash in scope. Deduplication is **required**, not optional — resolution lands in Phase 3 where spatial enrichment exists. |
| XS2 | **`crash_sev_id` is not the identity mapping.** Code `5` means *no apparent injury*, not most-severe, and `1`/`3` are inverted relative to KABCO. | Grouped `crash_sev_id` against the injury-count columns; code `4` matches `death_cnt>0` at exactly 451 | Mapping derived from the data and asserted in tests, not taken from a CRIS lookup that is not available here. `severity = crash_sev_id` would be badly wrong. |
| XS3 | **FARS `MINUTE` carries a 99 sentinel too**, not just `HOUR`. Loading it raises `Time out of range: 15:99:0`. **1,668 rows have no usable time**, and falling back to date-only put them all at midnight — a fake spike of ~2,000 in any time-of-day distribution. | Build failed on first run; the midnight spike found later by comparing hour 0 (10,599) against its neighbours (~8,100) | A valid hour with a sentinel minute keeps the hour; an invalid hour drops the time and keeps the date. A **`time_known`** flag carries through to `silver_crash_geo`, where `local_hour` is NULL rather than a fabricated 00:00. Day of week survives, since the date is sound. |
| XS4 | **The severity crosswalk is lossy in one direction only.** Montgomery's 3-level `acrs_report_type` cannot separate C/B/A, and is used for **1,811 crashes (1.45%)** — 674 with no party row, 1,129 whose party severities are all `None`. | `severity_basis` column | Every silver row records which path produced its severity, so the lossy subset is queryable rather than described in prose. |
| XS5 | **Montgomery amends a crash by deleting and re-inserting it under a new Socrata `:id`**, not by updating in place. Found 4 `report_number`s holding two `:id` each; one had its crash time amended (`MCP26350097`, 04:47 → 04:34). | Grain check after an incremental run — `rows > distinct crash_sk` | Crash dedup keys on the **business key** (`report_number`), not the bronze record id. Bronze correctly keeps both versions as history. |
| XS6 | **The `person_id` collisions are two different things.** Same-run duplicates are genuinely distinct people sharing an id (C4); later-run duplicates are amendment re-issues. | `_ingested_at` differs between the two cases | Party dedup keeps every row whose ingestion timestamp is newest for its `(report_number, person_id)` — same-run siblings survive, superseded versions drop. Collapsing on the pair would delete real non-motorists. |

---

## GE. Geospatial findings (Phase 3a)

Timezone and H3 applied to all **360,056** geocodable crashes. No key, no downloads.

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| GE1 | **A state-based timezone rule is wrong for 2,030 crashes.** 819 Texas crashes resolve to `America/Denver` (El Paso/Hudspeth) and 1,211 Florida crashes to `America/Chicago` (Panhandle). | `timezonefinder` point lookup vs the state's dominant zone | Zone derived from **coordinates**, never state or area code. This is a Part 5c compliance control — a wrong zone is a call placed outside the legal window. |
| GE2 | **One crash records a wall-clock time that never existed.** FARS `2023:120545` (FL) is timestamped `2023-03-12 02:00:00` — the exact spring-forward instant. | DST gap detection via UTC round-trip | Flagged `DST_GAP`, resolved forward, not dropped. |
| GE3 | **91 crashes fall in the fall-back ambiguous hour**, across all three sources (67 FARS / 22 Montgomery / 2 TxDOT). | `fold=0` vs `fold=1` UTC offsets differ | Flagged `DST_AMBIGUOUS`, resolved to the first occurrence. Recorded rather than silently picked. |
| GE4 | **One US crash geocodes into Mexico.** FARS `2022:483534`, jurisdiction TX, at 31.739/−106.435 — a few hundred metres across the Rio Grande from El Paso, resolving to `America/Ciudad_Juarez`. | 28 distinct zones found where ~25 were expected | Retained with its true zone. Offset happens to match `America/Denver`, so no practical impact — but it is a coordinate-quality signal worth surfacing. |
| GE5 | **Arizona resolves to `America/Phoenix`** (6,054 crashes), which does not observe DST. | Zone profile | Confirms the derivation is zone-based, not offset-based. A pipeline collapsing zones to UTC offsets would lose this and shift every Arizona crash by an hour for half the year. |

**CRS discipline.** Storage is EPSG:4326; every measurement reprojects first.
`measurement_crs()` **raises** on 4326 and 3857 rather than returning them — Part 3a scores
a section zero for a distance computed in Web Mercator, and the error is invisible in the
output (a 500 m buffer near Baltimore measures ~387 m on the ground).

---

## TX. TxDOT source data defects

Measured against ingested bronze, **14,535 rows** (`bicyclist_involved_fl=1`, 2020-01-01
to 2024-12-31).

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| TX1 | **Two competing coordinate pairs.** CRIS-derived `latitude`/`longitude` populated on 12,973 (89.3%); officer-reported `rpt_latitude`/`rpt_longitude` on 2,425. | Null counts on both pairs | Precedence: CRIS-derived first, officer-reported as fallback. Phase 3. |
| TX2 | **The fallback barely helps.** `rpt_*` rescues only **203** of the 1,562 rows where the derived pair is null. **1,359 rows (9.35%) have no usable coordinates at all.** | Cross-tab of the two pairs | Sets a hard geospatial coverage ceiling of ~90.6%. Reported rather than imputed. |
| TX3 | **1,150 rows (7.9%) are amendments** (`amend_supp_fl=1`), and 1,562 (10.7%) are ungeocoded (`located_fl=0`). | Cross-tab | Amendments are the restatement driver; a changed row surfaces as a new content hash on a stable `crash_id`. |
| TX4 | **`secondary_crash_fl` is a String** (`'True'`/`'False'`) while every other `*_fl` is a 0/1 SmallInteger. | Distinct values per column | Type dispatch on the `_fl` suffix breaks. Handled per-column, not by naming convention. |
| TX5 | **`crash_id` uniqueness had to be established client-side** — the service rejects `count_distinct` on it despite advertising support. Confirmed unique: 14,535 of 14,535. | Post-extraction count in DuckDB | `crash_id` is the business key; `ESRI_OID` is a within-run cursor only. |

**The layer is mislabeled, quantified.** One week of the unfiltered table
(2024-12-25 → 12-31) returns **9,600 crashes** — two-thirds of the entire five-year
"bicyclist involved" subset (14,535). Whether the wider scope should be used is argued in
`MEMO.md`; bronze holds the bicyclist scope by default.

---

## FA. FARS source data defects

Measured against ingested bronze, **222,695 crash rows** (`accident`, 2019–2024).

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| FA1 | **Sentinel coordinates are stored at different precision per year** — `77.7777` in 2019 but `77.77770000` in 2020–24. A string-equality check catches 2019 only and **silently misses 805 of 873** sentinel rows. | Grouped raw values where `LATITUDE >= 77` | Sentinel detection is a **numeric cast**, never string equality. Preserved at bronze, excluded at silver. |
| FA2 | **Longitude sentinels are a different scale** — `777.7777` / `888.8888` / `999.9999`, not the `77.7777` set the brief lists. | Same query, both columns | Both scales checked. A latitude-only rule leaves the longitude sentinels in place. |
| FA3 | **873 rows (0.39%) carry sentinel coordinates**, spread across every year (68 / 170 / 122 / 214 / 129 / 170). | Numeric cast per year | Excluded from spatial work at silver, retained in crash counts. |
| FA4 | **Archive layout changes between years.** 2019 puts `accident.CSV` at the ZIP root; 2024 puts `FARS2024NationalCSV/accident.csv` in a subdirectory. Case also varies (`Person.CSV` vs `person.csv`). | Load failed on 2019 | Matches on basename, case-insensitively. |
| FA5 | **Encoding changes between years.** 2021 is UTF-8 with a BOM; the others are latin-1. Decoded as latin-1, the first column name becomes `ï»¿STATE` — a *separate* bronze column from `STATE`, splitting the field across years. | Schema-drift warning fired on 2021 | `utf-8-sig` first, `latin-1` as fallback. Caught by the drift detector, which is what it is for. |
| FA6 | **`ST_CASE` is unique only within a year** — 2019 and 2024 both contain `10001`. | Key profiling | Record key is `{year}:{ST_CASE}`. Verified 222,695 distinct over 222,695 rows. |

---

## GE (cont). Census geography findings (Phase 3b)

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| GE6 | **The bounding-envelope check has false negatives — 92 of them.** A1 caught 114 Montgomery crashes with wildly wrong coordinates, but the envelope is a *rectangle* and Montgomery County is not. Washington DC and parts of northern Virginia sit inside that rectangle, so crashes there pass the envelope check. The tract join rejects them: **45 in DC, 41 in Virginia, 6 on other boundaries.** | Crashes with `geo_quality='OK'` that matched no Maryland block group | Reported, not silently dropped. A polygon test against the county boundary would supersede the envelope entirely — recorded as the better check, not built twice. |
| GE7 | **99.95% census coverage** — 182,734 of 182,832 in-scope crashes assigned a block group. The 0.05% shortfall is GE6 plus 6 FARS/TxDOT boundary points, not noise. | Coverage count after the join | Asserted as a floor in tests, so a regression is visible; the remainder is explained rather than tolerated. |
| GE8 | **Block-group GEOIDs nest exactly**, so the tract is the first 11 characters of the 12-character block group. Verified: 0 mismatches, lengths uniformly (12, 11). | Cross-check against the authoritative MD tract file | Only the BG shapefiles are fetched — half the download, one join. |

**Scope:** boundaries held for MD, TX and FL only. The 177,224 FARS crashes in other states
get `null` — never a nearest match, since a wrong tract is worse than an absent one. Verified
by a test: zero non-null block groups outside the three jurisdictions, and every assigned
FIPS prefix matches its crash's jurisdiction.

---

## GE (cont). Road network findings (Phase 3c)

Montgomery County, 125,058 crashes snapped to 172,311 drivable OSM segments.

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| GE9 | **Snap distance is tightly distributed with a long, absurd tail.** p50 **2.9 m**, p90 9.7 m, p99 31.7 m, p99.5 42.2 m, p99.9 **123.7 m**, max **8,616 m**. | `quantile_cont` over the snapped set | Rejection threshold set at **50 m** — chosen because it falls in the natural gap between p99.5 and p99.9, not picked a priori. 469 crashes (0.37%) flagged `DISTANCE_EXCEEDED`, **47 of them beyond 400 m** — exactly the "fiction" the brief describes. Flagged, never dropped. |
| GE10 | **`maxspeed` is 64.0% null and `lanes` 46.7% null** on accepted snaps — and the sparsity tracks road importance almost perfectly: trunk 14.2% → primary 30.2% → secondary 51.3% → tertiary 73.3% → residential 93.0% → **service 99.4%**. | Null rate grouped by `road_class` | **Reported, never imputed.** The monotonic gradient is the argument against imputing from road class: it would invent the value for 93–99% of exactly the minor roads where speed limits vary most. |
| GE11 | **The fixture's `P020` annotation does not hold against current OSM.** Its `fixture_note` reads *"nearest road is ~400m away"*, but the coordinate (39.1462, −77.4166) sits **9.1 m from Elgin Road (MD 109)** and 18.8 m from Fisher Avenue (MD 107) — a crossroads of two state routes in Poolesville. | Direct distance query against the loaded network | The snap is reported as measured. The code was **not** bent to reproduce the annotation; the rejection rule is instead demonstrated against the 469 real crashes that genuinely exceed the threshold. Either the note predates OSM densification or the coordinate did not land where intended. |

**Not used:** Overpass. The brief caps it at small ad-hoc queries and a county road network is
not one; the Geofabrik state extract (204 MB) is read once and bbox-clipped before anything
else happens.

---

## AN. Analysis-layer findings (Phase 3d)

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| AN1 | **The analysis was not reproducible.** Identical data gave FDR hotspot counts of 0, 12 and 43 on successive runs. Cause: DuckDB guarantees no row order without `ORDER BY`, and row order determines the weights-matrix construction order. | Running the pipeline twice and diffing | `ORDER BY h3_r8` on every contributing query, plus a re-sort after the merge. A test asserts the `ORDER BY` is present. |
| AN2 | **Moran's I was understated by half** — 0.21 rather than 0.42 — by the same row-order bug. The unsorted frame was corrupting the spatial relationship itself, not just the significance tests. | Comparing I before and after the fix | Same fix. Worth separating from AN1 because it shows the bug was distorting the *statistic*, not only its p-value. |
| AN3 | **The permutation count was silently deciding the FDR outcome.** `p_sim` floors at 1/(perms+1); at 999 that is 1e-3, above the BH threshold LISA's 19 floor-valued cells had to clear (8.5e-4). LISA reported zero clusters while Gi\* reported plenty. | Comparing the p-value distributions of the two statistics | 9,999 permutations. A test asserts the floor sits below the FDR threshold for the number of tests actually run. |
| AN4 | **Open-Meteo meters by data volume, not request count.** 30 cells × 11.7 years × 4 variables exhausted the hourly budget outright, having first tripped the minutely one. | HTTP 429 with two different reasons | Window bounded to a recent period, request paced, and 429 given a 62 s cooldown rather than jittered backoff. Recorded as a deliberate scope cut. |

---

## OP. Operability findings (Phase 6a)

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| OP1 | **The obvious drift detector would have missed the graded change.** `driver_substance_abuse` kept its name and its type — only its *values* changed — so a schema-watching detector passes it silently. | Replaying both detectors over the full bronze history | A second detector watches the distinct-value **set** per period. It fires at **2023-12**, naming the first two comma-joined tokens after 107 clean prior periods. |
| OP2 | **A cardinality detector fails in both directions at once.** The distinct-value count holds at **9** straight through 2023-10 → 2024-01, so it never fires on the change; meanwhile it fires in **92 of 140** periods on routine churn. | Comparing both detectors period by period | `cardinality_only()` is kept in the codebase specifically so this comparison is a test rather than a claim. Blind where it matters, noisy where it does not — an operator would have muted it years earlier. |
| OP3 | **Referential integrity runs one way only.** `drivers ⊆ incidents` holds; the reverse is false by design (785 orphan crashes, A4). A contract asserting `incidents ⊆ drivers` would fail on correct data. | Writing the contract and checking which direction passes | The bronze contract asserts the drivers→incidents direction explicitly, with a comment saying why the reverse is wrong. A test pins the direction. |

**Idempotent rebuild, proven.** Rebuilt over 2020-01-05 → 2024-12-30 — a window containing
restated TxDOT reports (1,150 rows carry `amend_supp_fl=1`) — and fingerprinted before and
after: **252,006 rows, md5 `3c745505…` both times.** Byte-identical, which is what Part 6
asks to be *proven* rather than asserted.

---

## CP. Compliance-layer findings (Phase 5)

| # | Finding | How detected | What the pipeline does |
|---|---|---|---|
| CP1 | **`legal_basis` was empty on every decision.** `StrEnum` members discard a docstring written after the assignment, so `ReasonCode.X.__doc__` is `''` for all 31 codes and the citation lookup silently returned "no citation recorded". | A test asserting every code carries a citation | Citations moved into a `CITATIONS` dict Python can read. A test pins the trap so it cannot return. |
| CP2 | **YAML turned the RND rule into a no-op.** `safe_harbour_responses: [NO]` parses as `[False]` under YAML 1.1 — the Norway problem — so no response string ever matched. | Printing the parsed ruleset after writing it | Values quoted, with a comment saying why. A sweep confirmed the five remaining booleans are intentional. |
| CP3 | **The vault was storing 40 NULL names.** The fixture reader already excluded direct identifiers, so the vault received rows with nothing in them — clean-looking separation that was actually data loss. | A test asserting the vault side is populated | `load_identities()` reads raw rows for the vault; `load_fixture()` returns analytic records carrying the NPA only. |
| CP4 | **DNC scrub dates drifted to the wall clock.** `dnc_scrub_asof` was derived from `evaluated_at`, dating every scrub to whenever the job ran rather than the frozen evaluation date. | Inspecting an emitted record: 2026-09-12 against a frozen 2026-09-01 | Derived dates anchor to the frozen date. The 31-day staleness SLA now matches the decision it justified. |
| CP5 | **The fixture's consent has no provenance** — a bare boolean with no disclosure text, hash, URL, timestamp, IP, user agent or seller chain. | Building the contract record and finding nothing to put in `consent.*` | `require_provenance` models it. Setting it true takes the written channel from 25 eligible to **zero**. |

---

## B. Endpoint drift vs the brief

Reported per `config/sources.toml`. Some of this may be deliberate — the brief plants
defects and asks for them back — so these are observations, not claims of error.

| # | Finding | How detected |
|---|---|---|
| B1 | **TxDOT's ObjectID field is `ESRI_OID`, not `OBJECTID`.** Any query using `OBJECTID` returns HTTP 400. Hard blocker. | `curl ".../FeatureServer/0?f=json" \| jq -r .objectIdField` |
| B2 | **`WHERE` clauses did not time out.** Flag predicates 1.4–1.9 s, date predicates 10–12 s. | `curl -w '%{time_total}'` on filtered counts |
| B3 | **TxDOT dates are strings, but ISO `YYYY-MM-DD`** — so range predicates work. The trap: `crash_date >= '01/01/2024'` silently returns all 3,088,450 rows instead of erroring. | Compared row counts from ISO vs US-format literals |
| B4 | **All six FARS years carry 2026 mtimes**, in three batches — 2019+2020, 2022+2023+2024, and 2021 revised alone two months later. Restatement is broader than the brief's single example. | `curl -I` on each year's ZIP |
| B5 | **NHTSA CrashAPI is edge-blocked** (HTTP 403, Akamai), not merely unreliable. | Three probes, all 403 before reaching origin |
| B6 | **Montgomery row counts have drifted** (125,005 vs 124,770 stated, etc.) — ~3 days of new crashes. | `$select=count(1)` per dataset |

**On B2, deliberately:** this is recorded as *"measured 10–12 s on 2026-09-04,"* not as
"the brief is wrong." One client on one day cannot disprove a claim about behaviour under
load. Design response is a 60 s+ read timeout and no aggressive retry — treat it as slow,
not broken.

**On B4:** because three years share a timestamp to the second, `Last-Modified` cannot
order them. **ETag is the version identity.** FARS 2025 returns 404, as expected for the
normal ~15-month lag, so year discovery is a probe loop rather than a fixed list.

---

## C. Operational hazards

| # | Finding | What the pipeline does |
|---|---|---|
| C1 | **Socrata omits null fields from row JSON** — a row's keys show what was populated, not the schema. | Reads the schema from `X-SODA2-Fields`/`X-SODA2-Types` headers and snapshots it per run. |
| C2 | **A publisher reload would look like 220k changed rows.** All three datasets show a min `:updated_at` of 2024-06-12, a reload artefact. | Content hash excludes system fields, so a reload inserts 0 rows. A guard aborts when a delta exceeds 50% of the table. |
| C3 | **No rate-limit headers exist** — no budget to observe. | Unconditional backoff on 429/5xx. |
| C4 | **`person_id` is not unique in non-motorists** — 7,521 rows, 7,512 distinct. Deduping on it would drop 9 rows. | Bronze dedupes on Socrata's `:id`. |
| C5 | **`crash_date_time` carries no timezone** — it is wall-clock local. Attaching UTC shifts every crash 4–5 hours. | Must be localized, not converted. Phase 3. |
| C6 | **All numerics arrive as strings**; TxDOT types `crash_id` as Double (would become a float join key). | Bronze stores TEXT and casts at silver, so a bad value is quarantined rather than failing the load. |

---

## Tests

Each Montgomery defect has a test that fails on bronze and must pass on silver.

| Defect | Test |
|---|---|
| A1 | `test_coordinates_within_montgomery_envelope` |
| A2 | `test_substance_abuse_dictionary_normalised` |
| A3 | `test_dictionary_cutover_overlap_handled` |
| A4 | `test_incidents_drivers_report_number_reconciliation` |

Run `python -m pytest tests/ --runxfail` to see them fire with live counts.
