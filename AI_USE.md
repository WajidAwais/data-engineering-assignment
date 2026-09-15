# AI Use

## Disclosure choice

This file takes the transparent option. It records where AI assistance was used, what it got
wrong, and what caught each error — including mistakes that survived several drafts. Nothing is
smoothed over. Where a drafted claim and a measurement disagreed, the measurement won and the
disagreement stayed in the record rather than being quietly resolved.

The claims made here are checkable against the repository rather than offered on trust. Every
reconnaissance finding in `DATA_QUALITY.md` §B carries the command that reproduces it, and each
correction listed below is pinned to a test or a query.

## Tool and working pattern

Claude, via Claude Code, working directly in the repository. Work was organised as one plan
per phase, reviewed before implementation, followed by a build and a verification pass. The
tool inspected live sources, proposed designs, wrote Python and SQL, drafted tests, ran
measurements, and drafted documentation.

None of that made the output trustworthy on its own. The gates were executable: defect tests
that must fail on bronze and pass on silver, layer-boundary data contracts with negative tests,
a byte-identical rebuild proof, a PII scan over emitted files, and human acceptance of every
scope and legal judgment.

## Assistance by phase

| Phase | AI-assisted work | Verification boundary |
|---|---|---|
| 0 — scaffolding | Read and consolidated the instruction files scattered across the repo; drafted `src/config.py`, ingest skeletons, `conftest.py` | Running the suite, which exposed a missing `pytest` declaration |
| 1a — ingestion, Montgomery | Live read-only reconnaissance of all three endpoints; `http`/`bronze`/`watermark`/`socrata`/`montgomery` modules | Re-verified against raw responses; every finding carries a reproducible command in `DATA_QUALITY.md` §B |
| 1b — TxDOT, FARS | ArcGIS ID-planned batch extraction; ETag conditional fetch for annual files | Manifest row counts, content-hash dedup, schema-drift warning on encoding change |
| 2 — silver | Profiling queries, normalisation, severity crosswalk, grain resolution | Four defect tests asserted against bronze and satisfied by silver; grain assertions |
| 3a — geo core | CRS discipline, coordinate-derived timezones, H3 indexing | Guard raising on 3857/4326; UTC round-trip test separating DST gap from ambiguity |
| 3b — census | TIGER block-group fetch and polygon join | GEOID nesting checked against the authoritative tract file — 0 mismatches |
| 3c — roads | OSM extract, snapping, linear referencing | Snap distances measured; the fixture's `P020` annotation contradicted and reported, not accommodated |
| 3d — analysis | Moran's I / LISA, Getis-Ord Gi*, ST-DBSCAN, Benjamini-Hochberg FDR | Deterministic ordering plus fixed seed; permutation sweep; repeat runs compared |
| 4 — scoring | Additive features, spatially-blocked cross-validation | `FORBIDDEN_FEATURES` raises on geographic or demographic inputs; held-out AUC reported as measured |
| 5 — compliance | Statute research, rules-as-data, engine, tokenisation vault, consent and lineage | Golden fixture, PII grep over output, live ruleset change exercised without a code edit, ruleset hash bound to each decision |
| 6 — operability | Dagster graph, drift detection, backfill proof, GeoParquet export | Byte-identical rebuild over a window containing restatements; six contract-violation tests plus a satisfied-contract control; drift replay |
| 7–8 — documents | Consolidation and cross-referencing | Every figure re-queried from the warehouse; every documented command and flag checked against source |

## Human decisions

The decisions that change product meaning or risk were retained by the owner, not delegated.
These include: not ingesting ACS socioeconomic data at all rather than holding it under a
promise not to rank on it; scoping road enrichment to Maryland; bounding the weather window
after the free tier refused a full backfill; retaining sentinel-coordinate crashes as flagged
rather than dropping them; deriving the TxDOT severity mapping from injury-count columns rather
than assuming the codes are ordinal; leaving the cross-source overlap quantified but not
deduplicated; and allowing the live-telephone output file to stay empty rather than relaxing a
gate to produce a number.

Legal judgment was human-owned throughout. The default disposition is ineligible, and
eligibility is proven per record with a citation rather than assumed. `COMPLIANCE.md` and
`DECISIONS.md` record both the readings adopted and the alternatives rejected.

## Legal research and its limits

Statutory research was AI-assisted, and this is the weakest-verified area in the submission —
stated plainly rather than glossed.

What was done: each citation was encoded as data in `compliance/rules.yaml`,
`config/blackout_windows.csv` or the `CITATIONS` registry in `src/compliance/reason_codes.py`,
never as a constant in executable code, so that the cited text and the rule that depends on it
cannot drift apart. A ruleset version and hash bind every decision to the body of rules that
produced it. Contradictory or unsettled authority is recorded rather than resolved — the
vacated one-to-one consent rule and the *Bradford* circuit split both appear with their limits
attached.

What was not done: no attorney reviewed any of it. `COMPLIANCE.md` states in its opening that
it is an engineering reading rather than legal advice, and `MEMO.md` §6 names the scope of ABA
Model Rule 7.3 as applied to a non-attorney call centre as the weakest link in the chain. The
conclusion survives that weakness — four independent bars produce the same answer, so three
would remain if that one fell — but the individual citations deserve review by counsel before
anyone relies on them.

## Mistakes caught by tests and measurements

AI assistance produced plausible, confidently-worded mistakes. Keeping the list is part of the
disclosure.

**Ingestion.** `changes()` was used to count affected rows — that is SQLite, not DuckDB.
Socrata system fields leaked into the business columns until a skip list was added. The FARS
archive layout varies by year, so a path match had to fall back to the lowercased basename, and
the 2021 file is UTF-8-with-BOM where the others are latin-1 — caught by the schema-drift
warning rather than by reading.

**Grain.** Montgomery County amends a record by deleting and reinserting it under a new row id,
so the obvious dedup key silently kept both versions. Crash dedup moved to `report_number`;
party dedup keeps the rows whose ingest timestamp is greatest for each
`(report_number, person_id)`, so genuinely distinct people in the same run survive.

**Time.** 1,668 FARS rows carry sentinel times that parsed into a convincing midnight. A
`time_known` flag now carries the distinction through to the enriched layer and nulls the local
hour. Separately, a DST *gap* was initially reported as ambiguity, because `fold` changes the
offset in both cases; the gap test must run first.

**Reproducibility.** Spatial statistics were not reproducible. DuckDB guarantees no row order
without `ORDER BY`, and the unsorted frame was changing the construction order of the weights
matrix — so significant hotspots swung 0 → 12 → 43 across identical runs and Moran's I read
0.21 instead of 0.42. The bug was distorting the statistic itself, not merely its p-value.
Separately, `p_sim` cannot fall below `1/(permutations+1)`, so at 999 permutations the
permutation floor rather than the data was deciding the FDR threshold; it was raised to 9,999.

**Compliance.** `legal_basis` came back empty on every decision because a `StrEnum` discards
docstrings after assignment — replaced with an explicit `CITATIONS` dict. YAML parsed the bare
token `NO` as boolean false, so a reassigned-number safe-harbour rule never fired; the value is
now quoted. The vault stored 40 null names until identity loading was separated from fixture
loading, leaving the analytic record carrying only the tokenised number. DNC scrub dates
drifted against the wall clock until they were anchored to the frozen evaluation date.

**Operability.** `from __future__ import annotations` broke Dagster's runtime resolution of
context types and had to be removed from that module. A read-only session fixture made the
rebuild proof impossible, because DuckDB refuses a second connection under a different
configuration.

**Overstated claims, corrected against measurement.** A draft asserted that the 785 orphaned
crashes were "disproportionately single-vehicle and non-motorist"; checking found the real
signal was `collision_type='UNKNOWN'` at 14.4% against a 0.63% baseline, and that check
incidentally surfaced an undocumented dual-dictionary defect. A claim that the cardinality
drift detector "never moves" was wrong in both directions — it fires 92 times in 140 periods
and still misses the transition that matters. An assumption that county-level partitions prune
better than national ones was contradicted by measurement, 67% against 44%. Reconnaissance
reported that TxDOT `WHERE` clauses do not time out, contradicting the brief; one client on one
day cannot disprove a claim about behaviour under load, so it was recorded as a timestamped
measurement rather than as a correction.

The pattern was consistent: strengthen or add a test, rerun it, record the measured correction.
Drafted prose was never accepted as evidence for a number or a legal rule.
