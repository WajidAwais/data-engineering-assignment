"""Spatial-risk model and its backtest.

The one falsifiable claim in the scoring stack: that crash-scene conditions —
time of day, road class, posted speed — carry signal about whether a crash
produces a serious injury. Everything else in the priority score is deterministic
arithmetic over known facts and needs no backtest because it predicts nothing.

Logistic regression, deliberately. Coefficients times standardised feature values
*are* the named contributions Part 4 demands. A gradient-boosted model would
score better and would be exactly the opaque blob the brief rules out.

Spatial cross-validation
------------------------
Phase 3d measured Moran's I = 0.42 over these cells: crash intensity is strongly
spatially autocorrelated. Part 3c names the consequence — "random splits leak
across spatially autocorrelated neighbors and inflate your score" — so the
backtest reports **both** splits and the gap between them is the finding.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import GroupKFold, KFold
from sklearn.preprocessing import StandardScaler

from src.config import load_settings
from src.ingest import watermark

from . import features as F

log = logging.getLogger("scoring.model")

SERIOUS_THRESHOLD = 3      # suspected serious injury or fatal
SEED = 20260915
FOLDS = 5


@dataclass
class Backtest:
    random_auc: float
    blocked_auc: float
    random_ap: float
    blocked_ap: float
    n: int
    positives: int
    groups: int

    @property
    def inflation(self) -> float:
        """How much a random split would have overstated the score."""
        return self.random_auc - self.blocked_auc


def target(frame: pd.DataFrame) -> np.ndarray:
    return (frame["severity_ordinal"] >= SERIOUS_THRESHOLD).astype(int).to_numpy()


def _fit(matrix: pd.DataFrame, y: np.ndarray) -> tuple[LogisticRegression, StandardScaler]:
    scaler = StandardScaler().fit(matrix)
    model = LogisticRegression(
        max_iter=2000, class_weight="balanced", random_state=SEED
    ).fit(scaler.transform(matrix), y)
    return model, scaler


def backtest(matrix: pd.DataFrame, y: np.ndarray, groups: np.ndarray) -> Backtest:
    """Random 5-fold against H3-blocked 5-fold.

    The random number is the one you would wrongly report; the blocked one is
    honest. Both are printed so the difference is visible rather than asserted.
    """
    def evaluate(splitter, split_args) -> tuple[float, float]:
        oof = np.zeros(len(y), dtype=float)
        for train_idx, test_idx in splitter.split(matrix, y, *split_args):
            model, scaler = _fit(matrix.iloc[train_idx], y[train_idx])
            oof[test_idx] = model.predict_proba(scaler.transform(matrix.iloc[test_idx]))[:, 1]
        return roc_auc_score(y, oof), average_precision_score(y, oof)

    random_auc, random_ap = evaluate(KFold(FOLDS, shuffle=True, random_state=SEED), ())
    blocked_auc, blocked_ap = evaluate(GroupKFold(FOLDS), (groups,))

    return Backtest(random_auc, blocked_auc, random_ap, blocked_ap,
                    len(y), int(y.sum()), len(np.unique(groups)))


def contributions(model: LogisticRegression, scaler: StandardScaler,
                  matrix: pd.DataFrame) -> pd.DataFrame:
    """Per-record named contributions: coefficient x standardised value.

    These sum (plus the intercept) to the log-odds, which is what makes the
    score decomposable rather than a blob.
    """
    standardised = scaler.transform(matrix)
    return pd.DataFrame(standardised * model.coef_[0], columns=matrix.columns)


def train(con, source: str = "MONTGOMERY_MD"):
    frame = F.load_frame(con, source)
    matrix = F.build(frame)
    y = target(frame)
    groups = frame["h3_r8"].to_numpy()
    model, scaler = _fit(matrix, y)
    return frame, matrix, y, groups, model, scaler


def run(source: str = "MONTGOMERY_MD") -> dict:
    settings = load_settings()
    con = watermark.connect(settings.warehouse_path)
    try:
        frame, matrix, y, groups, model, scaler = train(con, source)
        result = backtest(matrix, y, groups)

        risk = model.predict_proba(scaler.transform(matrix))[:, 1]
        parts = contributions(model, scaler, matrix)
        out = pd.DataFrame({"crash_sk": frame["crash_sk"], "spatial_risk": risk})
        out["components"] = [json.dumps(r) for r in parts.round(5).to_dict("records")]

        con.register("_risk_staging", out)
        con.execute("CREATE OR REPLACE TABLE gold_crash_risk AS SELECT * FROM _risk_staging")
        con.unregister("_risk_staging")

        coefs = pd.DataFrame({
            "feature": matrix.columns, "coefficient": model.coef_[0]
        }).sort_values("coefficient", key=abs, ascending=False)
    finally:
        con.close()
    return {"backtest": result, "coefficients": coefs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train and backtest the spatial-risk model")
    parser.add_argument("--source", default="MONTGOMERY_MD")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")

    r = run(args.source)
    b = r["backtest"]
    print(f"\n  target: severity >= {SERIOUS_THRESHOLD}   "
          f"{b.positives:,} positives of {b.n:,}  ({b.positives/b.n:.2%})")
    print(f"  spatial blocking: {b.groups:,} H3 r8 cells\n")
    print(f"  {'split':<22} {'ROC AUC':>9} {'PR AUC':>9}")
    print(f"  {'random 5-fold':<22} {b.random_auc:>9.4f} {b.random_ap:>9.4f}   <- optimistic")
    print(f"  {'H3-blocked 5-fold':<22} {b.blocked_auc:>9.4f} {b.blocked_ap:>9.4f}   <- honest")
    print(f"\n  leakage inflation: {b.inflation:+.4f} AUC")
    print("\n  coefficients (standardised):")
    for row in r["coefficients"].itertuples(index=False):
        print(f"    {row.feature:<20} {row.coefficient:+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
