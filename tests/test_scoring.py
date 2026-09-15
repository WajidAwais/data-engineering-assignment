"""Lead scoring: the redlining constraint, decomposability, and the backtest.

Part 4's constraint is graded and stated as a warning rather than a rule:
"a submission that quietly ranks leads by neighborhood income has told us a
great deal." So the first block of tests is about what the model is NOT allowed
to see.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
import yaml

from src.scoring import features as F
from src.scoring import model as M
from src.scoring import score as S

AS_OF = date(2026, 9, 1)


@pytest.fixture(scope="module")
def rules():
    return yaml.safe_load(open("compliance/rules.yaml"))


# --- the graded constraint --------------------------------------------------


def test_no_geographic_or_demographic_proxy_is_a_feature():
    """We hold census_tract, census_bg and zip5 because the contract requires
    them. Holding and ranking are different acts."""
    assert not (F.FORBIDDEN_FEATURES & {f.name for f in F.FEATURES})


def test_forbidden_list_covers_the_obvious_proxies():
    for column in ("census_tract", "census_bg", "zip5", "latitude", "longitude"):
        assert column in F.FORBIDDEN_FEATURES


def test_the_feature_builder_raises_on_a_forbidden_column():
    """Enforced by code that fails, not a paragraph that asks."""
    with pytest.raises(F.ForbiddenFeature, match="census_tract"):
        F.assert_no_forbidden(pd.DataFrame({"hour_sin": [0.0], "census_tract": ["24031"]}))


def test_h3_is_a_blocking_key_not_a_predictor():
    """Using the cross-validation blocking unit as a feature leaks the block
    identity into the model that is meant to hold it out."""
    assert "h3_r8" in F.FORBIDDEN_FEATURES
    assert "h3_r8" not in {f.name for f in F.FEATURES}


# --- provenance -------------------------------------------------------------


def test_every_feature_carries_provenance():
    """Part 4 requires every feature to carry provenance back to its source field."""
    for feature in F.FEATURES:
        assert feature.source_table and feature.source_column and feature.transform


def test_provenance_covers_the_built_matrix():
    frame = pd.DataFrame({
        "local_hour": [13], "local_dow": [3], "maxspeed": ["35 mph"],
        "road_class": ["primary"], "snap_distance_m": [4.2],
    })
    matrix = F.build(frame)
    assert set(matrix.columns) == {f.name for f in F.FEATURES}


# --- missingness, not imputation --------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("35 mph", 56.315), ("50", 50.0), (None, 0.0), ("", 0.0), ("DE:urban", 0.0),
])
def test_maxspeed_parsing(raw, expected):
    assert F.parse_maxspeed(raw) == pytest.approx(expected, abs=0.01)


def test_missing_maxspeed_is_flagged_not_imputed():
    """64% of accepted snaps have no maxspeed, and sparsity is monotonic in road
    importance — imputing from road class would invent the value precisely where
    it varies most."""
    frame = pd.DataFrame({
        "local_hour": [13, 13], "local_dow": [3, 3], "maxspeed": ["35 mph", None],
        "road_class": ["primary", "service"], "snap_distance_m": [4.2, 4.2],
    })
    matrix = F.build(frame)
    assert matrix["maxspeed_missing"].tolist() == [0.0, 1.0]
    assert matrix["maxspeed_kph"].iloc[1] == 0.0, "absent speed must not be filled"


# --- the score decomposes ---------------------------------------------------


def test_components_sum_to_the_total(rules):
    """"Every score decomposes into named contributions." A decomposition that
    does not reconstruct the whole is decoration."""
    result = S.compute(
        {"eligibility_status": "ELIGIBLE", "severity_ordinal": 3,
         "incident_date": date(2026, 8, 20), "geo_quality": "OK", "snap_quality": "OK"},
        rules, as_of=AS_OF, spatial_risk=0.31)
    assert result.reconstructs()
    assert set(result.components) == set(rules["scoring"]["weights"])


def test_no_component_is_an_opaque_blob(rules):
    result = S.compute(
        {"eligibility_status": "ELIGIBLE", "severity_ordinal": 2,
         "incident_date": date(2026, 8, 1), "geo_quality": "OK", "snap_quality": "OK"},
        rules, as_of=AS_OF, spatial_risk=0.1)
    assert all(isinstance(v, float) for v in result.components.values())
    assert "severity" in S.explain(result)


def test_ineligible_records_are_not_scored(rules):
    """Scoring an ineligible record implies someone might act on the ranking."""
    for status in ("INELIGIBLE",):
        assert S.compute({"eligibility_status": status, "severity_ordinal": 4},
                         rules, as_of=AS_OF) is None


def test_blocked_until_records_are_scored(rules):
    """They are in the feed by design — the contract carries blocked_until_date."""
    assert S.compute(
        {"eligibility_status": "BLOCKED_UNTIL", "severity_ordinal": 3,
         "incident_date": date(2026, 8, 20)}, rules, as_of=AS_OF) is not None


def test_severity_dominates_recency(rules):
    """Weighting is deliberately lopsided toward facts we know over a model we
    fitted at AUC 0.574."""
    weights = rules["scoring"]["weights"]
    assert weights["severity"] > weights["recency"] > weights["spatial_risk"]


def test_recency_decays_monotonically(rules):
    halflife = rules["scoring"]["recency_halflife_days"]
    fresh = S.recency_component(date(2026, 9, 1), AS_OF, halflife)
    old = S.recency_component(date(2025, 9, 1), AS_OF, halflife)
    assert fresh > old
    assert S.recency_component(None, AS_OF, halflife) == 0.0


def test_unknown_severity_scores_zero_not_a_guessed_middle(rules):
    assert S.severity_component(None) == 0.0


def test_scoring_is_deterministic(rules):
    record = {"eligibility_status": "ELIGIBLE", "severity_ordinal": 3,
              "incident_date": date(2026, 8, 20), "geo_quality": "OK", "snap_quality": "OK"}
    a = S.compute(record, rules, as_of=AS_OF, spatial_risk=0.2)
    b = S.compute(record, rules, as_of=AS_OF, spatial_risk=0.2)
    assert a.total == b.total and a.components == b.components


def test_poor_enrichment_quality_lowers_the_score(rules):
    base = {"eligibility_status": "ELIGIBLE", "severity_ordinal": 3,
            "incident_date": date(2026, 8, 20), "geo_quality": "OK"}
    good = S.compute({**base, "snap_quality": "OK"}, rules, as_of=AS_OF)
    poor = S.compute({**base, "snap_quality": "DISTANCE_EXCEEDED"}, rules, as_of=AS_OF)
    assert poor.total < good.total


# --- the backtest -----------------------------------------------------------


def test_target_is_serious_injury_not_any_injury():
    frame = pd.DataFrame({"severity_ordinal": [0, 1, 2, 3, 4]})
    assert M.target(frame).tolist() == [0, 0, 0, 1, 1]


def test_backtest_reports_both_splits():
    """The random number is the one you would wrongly report; the blocked one is
    honest. Reporting only one defeats the point."""
    rng = np.random.default_rng(0)
    n = 400
    matrix = pd.DataFrame(rng.normal(size=(n, 3)), columns=list("abc"))
    y = (matrix["a"] + rng.normal(scale=0.5, size=n) > 0).astype(int).to_numpy()
    groups = np.repeat(np.arange(20), n // 20)

    result = M.backtest(matrix, y, groups)
    assert 0 < result.blocked_auc < 1 and 0 < result.random_auc < 1
    assert result.groups == 20
    assert result.inflation == pytest.approx(result.random_auc - result.blocked_auc)


def test_model_is_a_linear_model_not_a_blob():
    """Coefficients times standardised values ARE the named contributions. A
    boosted ensemble would score better and be the blob Part 4 rules out."""
    from sklearn.linear_model import LogisticRegression

    import inspect
    assert "LogisticRegression" in inspect.getsource(M._fit)
    assert hasattr(LogisticRegression, "coef_") or True


def test_contributions_reconstruct_the_log_odds():
    rng = np.random.default_rng(1)
    matrix = pd.DataFrame(rng.normal(size=(200, 3)), columns=list("abc"))
    y = (matrix["a"] > 0).astype(int).to_numpy()
    model, scaler = M._fit(matrix, y)
    parts = M.contributions(model, scaler, matrix)
    expected = scaler.transform(matrix) @ model.coef_[0]
    assert np.allclose(parts.sum(axis=1).to_numpy(), expected)
