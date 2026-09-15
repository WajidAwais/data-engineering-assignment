# Spatial Analysis

Three techniques over Montgomery County crashes. Part 3c asks for the output to be
interpreted in prose — *"an unlabeled heatmap is not an analysis"* — so each section says
what was run, why the parameters are what they are, and what the result actually means.

**Scope:** Montgomery County, 125,058 crashes, 2015-01-01 → 2026-09-09. The only
jurisdiction in our data that is all-severity rather than a fatality census, and the only one
with an enriched road network — which is what makes a road-length denominator possible.

**Reproducibility:** seed `20260915`, 9,999 permutations, 1,116 H3 r8 cells. Repeated runs
produce identical output; see "Two things that nearly went wrong" below for why that
sentence is not free.

---

## 1. Getis-Ord Gi\* — where are the hotspots, and hot relative to what?

**Run over** H3 resolution-8 cells (~0.74 km²) with ≥5 crashes, Queen contiguity from H3's
own `grid_disk` adjacency, `star=True` so a cell is included in its own neighbourhood.

**Two denominators, deliberately.** Part 3c asks to contrast raw counts against a normalised
rate. We do not hold ACS population — a standing decision, because block-group income and
its correlates are protected-characteristic proxies and Part 4 calls ranking on them
redlining-adjacent. Normalising *equal-area* hexagons by area is close to a no-op, so area
is not a substitute.

**Road length is one**, and arguably a better exposure measure for crash risk than
population anyway: crashes per kilometre of road asks "is this stretch unusually dangerous"
rather than "do many people live here".

| | Uncorrected (α=0.05) | After FDR | 
|---|---|---|
| Raw crash counts | 327 | **31** |
| Crashes per road-km | 339 | **48** |
| Hot under both | — | **29** |

**What this means.**

*FDR is not a formality.* At α=0.05 across 1,116 cells you expect ~56 false positives by
construction. Uncorrected, the method reports 327 hotspots; after Benjamini-Hochberg, 31.
Reporting the uncorrected figure would have been reporting mostly noise.

*The denominator changes the answer, and the direction is informative.* 19 cells are hot per
road-km but not on raw counts, while only 2 go the other way. Those 19 are places with few
crashes but very little road — short residential stretches and intersections carrying
disproportionate risk per kilometre. Raw counts miss them entirely because counting rewards
large arterials for being large.

*The top cells are corridors, not intersections.* The strongest are along Georgia Avenue
(80.9 crashes/km, z=3.3), the 16th Street corridor, and the Blair Circle area near the DC
line. These are exactly where a corridor-level intervention is plausible and a
point-level one is not.

---

## 2. Moran's I, then LISA — do crashes cluster at all?

**Global first, deliberately.** The statistic is worth running precisely because it can come
back uninteresting; asserting that crashes cluster without testing it is the error this
guards against.

| Variable | Moran's I | z | p |
|---|---|---|---|
| Raw counts | **+0.4239** | +20.68 | 0.0001 |
| Per road-km | **+0.4536** | +22.03 | 0.0001 |

**What this means.** Both are strongly positive and significant, so crash intensity is
spatially autocorrelated — neighbouring cells resemble each other far more than chance
allows. That licenses the local analysis; had I been near zero, the Gi\* hotspots above would
have been pattern-matching on noise.

Notably the rate clusters *slightly more* than the count (0.454 vs 0.424). Road density is
itself spatially smooth, so dividing by it does not destroy spatial structure — it sharpens it.

**LISA** (`Moran_Local`, same weights, FDR-corrected) finds **38 HH cells** — high-crash
cells surrounded by high-crash cells. These overlap heavily with the Gi\* hotspots, which is
the expected agreement between two related statistics rather than an independent confirmation.

**A caveat worth stating:** positive spatial autocorrelation means any model fitted on these
cells must not use random cross-validation splits. Neighbouring cells leak into each other
and inflate the score. Blocking by county or H3 parent cell would be required — noted
because we are not fitting a model, not because the problem goes away.

---

## 3. ST-DBSCAN — clusters tight in space *and* time

**Run over** 19,892 injury-or-worse crashes (`severity_ordinal ≥ 2`), projected to
EPSG:26985 so distances are metres.

**Two thresholds, not one.** Birant & Kut's ST-DBSCAN treats two points as neighbours only
if they are within ε_spatial **and** within ε_temporal. The common shortcut — rescale time
into the spatial metric and run ordinary DBSCAN with a single ε — is a different algorithm:
it lets a large spatial gap be bought back by temporal closeness, which produces exactly the
smear the brief warns about. Implemented sparsely: KD-tree for spatial neighbours, time
filter, then `DBSCAN(metric="precomputed")` on the resulting graph. A dense 19,892² matrix
would be 3 GB.

**Parameters chosen from a sweep, because the brief names the failure mode rather than the
parameter:**

| ε_temporal | Clustered | Clusters | Largest |
|---|---|---|---|
| 72 h | 0 | 0 | 0 |
| 720 h | 100 | 17 | 12 |
| **2,160 h (90 d)** | **2,288** | **305** | **35** |
| 8,760 h (1 yr) | 12,038 | 501 | **1,181** |

At a one-year window the largest cluster swallows 1,181 crashes. That is not a space-time
cluster, it is a map of where crashes happen — the "year-long smear" made concrete. At 90
days clusters stay interpretable as recurring conditions.

**Final parameters:** ε_spatial 200 m, ε_temporal 90 days, min_samples 5 →
**2,288 crashes in 305 clusters** (11.5% of injury crashes).

**What this means.** The largest clusters are arterial corridors with sustained elevated
injury rates over a season: Rockville Pike (35), Georgia Avenue at Connecticut (31), the
Russell Avenue / North Frederick corridor in Gaithersburg (30), Wisconsin Avenue through
Bethesda (27).

These are *different objects* from the Gi\* hotspots. Gi\* answers "which cells are
persistently dangerous across eleven years"; ST-DBSCAN answers "where and when did injuries
concentrate in a single season". A cell can be a Gi\* hotspot without ever forming a
space-time cluster — steady background risk — and a corridor can cluster hard for one
quarter without being a long-run hotspot, which is the signature of a temporary condition
like construction or a signal-timing change.

---

## Two things that nearly went wrong

Both are worth recording because both produced plausible-looking numbers.

**1. The analysis was not reproducible, and the cause was row order.** Successive runs gave
FDR hotspot counts of 0, then 12, then 43. `G_Local` with a fixed seed is deterministic — I
checked in isolation — but DuckDB gives no row-order guarantee without `ORDER BY`, and row
order determines the order cells enter the weights matrix, which changes every permutation
test downstream. Adding `ORDER BY h3_r8` fixed it, and **Moran's I went from ~0.21 to
0.42**: the unsorted frame had been corrupting the spatial structure itself, not just the
significance tests.

**2. The permutation count was silently deciding the FDR result.** `p_sim` can never fall
below 1/(permutations+1). With 999 permutations that floor is 1e-3, while Benjamini-Hochberg
compares the k-th smallest p against (k/n)·α. LISA had 19 cells at the floor needing to clear
19/1116 × 0.05 = 8.5e-4 — so the floor, not the evidence, was rejecting them, and LISA
reported zero clusters while Gi\* reported plenty. At 9,999 permutations the floor drops to
1e-4 and LISA finds 38. The permutation count is not a free parameter once FDR is involved.

---

## 4. Lead scoring backtest — and a leak that spatial blocking did not catch

**The claim under test.** Only one part of the priority score predicts anything: a
spatial-risk estimate claiming that crash-scene conditions — hour, road class, posted speed —
carry signal about whether a crash produces serious injury. Severity and recency are *known*
at scoring time and predict nothing, so there is nothing there to backtest.

**Target:** `severity_ordinal >= 3`, 2,848 positives of 125,058 (2.28%).
**Model:** logistic regression — coefficients × standardised values *are* the named
contributions Part 4 requires. A boosted ensemble would score better and be the opaque blob
the brief rules out.

### Result: the signal is weak, and that is the finding

| Split | ROC AUC | PR AUC |
|---|---|---|
| Random 5-fold | 0.5763 | 0.0302 |
| **H3-blocked 5-fold** | **0.5741** | **0.0299** |

Against a 2.28% base rate, a PR AUC of 0.030 is barely above chance. Crash-scene conditions
essentially do not predict injury severity — which is plausible, because severity is driven
by impact dynamics, vehicle mass and occupant factors, none of which appear in a public crash
feed. The model is kept and weighted at **0.10** in the score, as a tiebreaker. Presenting a
0.574 model as the core of a lead ranking would be dressing a coin-flip as a finding.

The leakage inflation here is **+0.0022 AUC** — negligible, because none of these features is
cell-specific, so holding out a cell removes almost nothing.

### The experiment that mattered

Part 3c warns that "random splits leak across spatially autocorrelated neighbors and inflate
your score", and Moran's I of 0.42 says that applies here. So I added the feature that should
trigger it: a cell-level historical severity rate, target-encoded on `h3_r8`.

| Encoding built on | Random CV | H3-blocked | Gap |
|---|---|---|---|
| whole dataset | 0.7005 | 0.6998 | +0.0006 |
| training fold only | 0.5651 | 0.5699 | −0.0048 |

**The +0.124 AUC gain is entirely leakage.** Encoded correctly — from training rows only,
with unseen cells falling back to the training base rate — the feature adds nothing at all.

**And spatial blocking alone did not catch it.** When the encoding is built on the whole
dataset, held-out cells still see their own target through the feature, so *both* splits are
inflated equally and the gap stays at +0.0006. A reviewer checking only "did you block
spatially?" would have passed this.

The lesson is narrower and more useful than the brief's framing: spatial cross-validation is
necessary but not sufficient. What matters is whether every feature is constructed **inside**
the fold. The feature is not in the shipped model.

---

## What is not here, and why

**Weather.** The ERA5 join is built (`src/geo/weather.py`) but bounded to a recent window:
Open-Meteo's free tier meters by data volume, and 11.7 years × 4 variables × 30 cells
exhausts the hourly budget outright. Recorded in DECISIONS as a deliberate cut — an
operational pipeline enriching *new* crashes needs recent weather, and backfilling the full
history needs either the paid tier or a job chunked across days.

**What ERA5 is.** Reanalysis on a ~9–25 km grid: a physical model constrained by
observations, spatially smooth and **never missing**. That last property is why it is the
backbone rather than NOAA GHCNh station observation, which is accurate at a point and
frequently absent — a weather feature that is null across the rural half of the county is
worse for analysis than a slightly smoothed one that is always present. A crash's
"visibility" here is the model's estimate for an 11 km cell, not what the driver could see.

**KDE, isochrones, spatial cross-validation.** Three techniques is the requirement. Isochrones
serve a trauma-centre access question that is not in scope, and spatial CV matters when a
model is being fitted — the caveat is recorded above instead.
