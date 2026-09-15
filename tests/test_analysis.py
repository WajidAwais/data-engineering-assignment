"""Spatial analysis correctness.

The tests that matter here are the ones guarding against plausible-looking wrong
answers: a non-reproducible permutation test, an FDR threshold the permutation
count cannot reach, and an ST-DBSCAN that has quietly become ordinary DBSCAN.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.geo import analysis


# --- multiple testing -------------------------------------------------------

def test_benjamini_hochberg_is_less_permissive_than_raw_alpha():
    """At alpha=0.05 across 1,116 cells you expect ~56 false positives by
    construction. Reporting uncorrected hotspots reports mostly noise."""
    rng = np.random.default_rng(0)
    pvals = rng.uniform(size=1000)          # all nulls true
    raw = (pvals < 0.05).sum()
    corrected = analysis.benjamini_hochberg(pvals, 0.05).sum()
    assert raw > 30, "sanity: uncorrected should flag ~5% of pure noise"
    assert corrected < raw


def test_benjamini_hochberg_rejects_nothing_on_pure_noise():
    rng = np.random.default_rng(1)
    assert analysis.benjamini_hochberg(rng.uniform(size=500), 0.05).sum() == 0


def test_benjamini_hochberg_is_a_step_up_not_a_bonferroni():
    """Many tied small p-values pass together — which is why Gi* (41 cells at
    the permutation floor) rejected while LISA (19) did not."""
    pvals = np.concatenate([np.full(40, 0.001), np.full(960, 0.6)])
    assert analysis.benjamini_hochberg(pvals, 0.05).sum() == 40
    # Bonferroni at the same alpha would need p <= 0.05/1000 = 5e-5 and reject none.
    assert (pvals <= 0.05 / len(pvals)).sum() == 0


def test_permutation_floor_is_below_the_fdr_threshold():
    """p_sim can never be below 1/(permutations+1). If that floor sits above
    (k/n)*alpha, the floor rejects the evidence rather than the other way round.
    This is why PERMUTATIONS is 9,999 and not 999."""
    floor = 1 / (analysis.PERMUTATIONS + 1)
    n_cells = 1116
    smallest_meaningful_k = 19       # LISA's floor-valued cell count, measured
    assert floor < (smallest_meaningful_k / n_cells) * analysis.ALPHA


# --- ST-DBSCAN: two thresholds, not one -------------------------------------

def _points(rows):
    return pd.DataFrame(rows, columns=["x", "y", "t_hours"]).assign(
        crash_sk=lambda d: [f"c{i}" for i in range(len(d))])


def test_points_close_in_space_but_far_in_time_do_not_cluster():
    """The whole point of the second threshold. A single-eps approximation over
    rescaled coordinates would merge these."""
    same_place_months_apart = _points(
        [(0, 0, h) for h in (0, 1, 2, 3, 4, 5)] +          # one week
        [(1, 1, h) for h in (5000, 5001, 5002, 5003, 5004, 5005)]  # ~7 months later
    )
    out = analysis.st_dbscan(same_place_months_apart, eps_spatial_m=50,
                             eps_temporal_h=72, min_samples=4)
    clusters = set(out.loc[out.cluster >= 0, "cluster"])
    assert len(clusters) == 2, "two bursts at one place months apart are two clusters"


def test_points_close_in_time_but_far_in_space_do_not_cluster():
    spread = _points([(i * 5000, 0, 1) for i in range(10)])
    out = analysis.st_dbscan(spread, eps_spatial_m=100, eps_temporal_h=100000,
                             min_samples=3)
    assert (out.cluster >= 0).sum() == 0


def test_a_genuine_space_time_burst_does_cluster():
    burst = _points([(i, i, i) for i in range(10)])
    out = analysis.st_dbscan(burst, eps_spatial_m=50, eps_temporal_h=72, min_samples=3)
    assert (out.cluster >= 0).sum() == 10


def test_no_pairs_within_thresholds_returns_all_noise():
    out = analysis.st_dbscan(_points([(0, 0, 0), (99999, 0, 99999)]),
                             eps_spatial_m=10, eps_temporal_h=1, min_samples=2)
    assert (out.cluster == -1).all()


def test_chosen_temporal_window_avoids_the_year_long_smear():
    """Measured: a one-year window produces a 1,181-crash cluster. The brief
    names that failure mode explicitly."""
    assert analysis.EPS_TEMPORAL_H < 8760, "a one-year window is the smear"
    assert analysis.EPS_TEMPORAL_H >= 720, "too tight finds nothing at all"


# --- reproducibility --------------------------------------------------------

def test_the_analysis_is_seeded():
    """Successive unseeded runs gave FDR hotspot counts of 0, 12 and 43 on
    identical data. An analysis that will not reproduce is not a finding."""
    assert isinstance(analysis.SEED, int)
    assert analysis.PERMUTATIONS >= 999


def test_cell_queries_are_deterministically_ordered():
    """DuckDB gives no row-order guarantee without ORDER BY, and row order
    determines the order cells enter the weights matrix — which changes every
    permutation test downstream even with a fixed seed."""
    import inspect

    for fn in (analysis.cell_counts, analysis.road_km_per_cell):
        assert "ORDER BY" in inspect.getsource(fn), f"{fn.__name__} has no ORDER BY"


def test_road_length_excludes_rejected_snaps():
    """A segment 400 m from the crash is not evidence of road in that cell, and
    it would deflate the crashes-per-km rate exactly where snapping is worst."""
    import inspect

    source = inspect.getsource(analysis.road_km_per_cell)
    assert "snap_quality = 'OK'" in source


def test_road_length_deduplicates_segments():
    """Summing segment_length_m per crash counts a segment once per crash on it,
    so a busy road inflates its own denominator and masks its own hotspot."""
    import inspect

    assert "DISTINCT" in inspect.getsource(analysis.road_km_per_cell)


# --- against the warehouse --------------------------------------------------

@pytest.fixture
def gold(warehouse):
    exists = warehouse.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='gold_hotspot_rate'"
    ).fetchone()[0]
    if not exists:
        pytest.skip("analyses not run; run: python -m src.geo.analysis")
    return warehouse


def test_fdr_reduced_the_hotspot_count(gold):
    raw, corrected = gold.execute("""
        SELECT count(*) FILTER (WHERE significant_uncorrected),
               count(*) FILTER (WHERE significant_fdr) FROM gold_hotspot_rate
    """).fetchone()
    assert corrected < raw, "FDR must be more conservative than raw alpha"
    assert corrected > 0, "if FDR rejects everything, check the permutation floor"


def test_the_denominator_changes_which_cells_are_hot(gold):
    """Part 3c asks to show normalisation changes the answer. Measured: 19 cells
    are hot per road-km but not on raw counts, and 2 the other way."""
    only_rate, only_raw = gold.execute("""
        SELECT count(*) FILTER (WHERE r.class <> 'hot' AND t.class = 'hot'),
               count(*) FILTER (WHERE r.class = 'hot' AND t.class <> 'hot')
          FROM gold_hotspot_raw r JOIN gold_hotspot_rate t USING (h3_r8)
    """).fetchone()
    assert only_rate > 0 or only_raw > 0, "the two denominators agree exactly — suspicious"


def test_lisa_quadrants_are_labelled(gold):
    quadrants = {q for (q,) in gold.execute(
        "SELECT DISTINCT quadrant FROM gold_lisa").fetchall()}
    assert quadrants <= {"HH", "HL", "LH", "LL", "ns"}
    assert "HH" in quadrants, "no HH cells — check the permutation floor against FDR"


def test_clusters_are_smaller_than_a_year_long_smear(gold):
    largest = gold.execute("""
        SELECT max(n) FROM (SELECT count(*) n FROM gold_stdbscan
                             WHERE cluster >= 0 GROUP BY cluster)
    """).fetchone()[0]
    assert largest < 200, f"largest cluster is {largest} — that is a smear, not a cluster"
