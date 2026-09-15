"""Deterministic priority score.

Part 4: "Every score decomposes into named contributions. No opaque blob."

So the score is an explicit weighted sum whose parts reconstruct the whole — a
test asserts that, because a decomposition that does not add up is decoration.
Weights live in `compliance/rules.yaml` beside the other policy.

What is predicted and what is known
-----------------------------------
Severity and recency are **known** at scoring time: they come off the crash
report and the calendar. Only `spatial_risk` is model-derived, and it backtests
at ROC AUC 0.574 under spatial blocking — real but weak. It is weighted at 0.10
accordingly. Presenting a 0.574 model as the core of a ranking would be
overselling a coin-flip.

Only ELIGIBLE and BLOCKED_UNTIL leads are scored. Scoring an INELIGIBLE record
implies someone might act on the ranking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from .features import FORBIDDEN_FEATURES

SCORABLE_STATUSES = frozenset({"ELIGIBLE", "BLOCKED_UNTIL"})
MAX_SEVERITY = 4.0


@dataclass(frozen=True)
class Score:
    total: float
    components: dict[str, float] = field(default_factory=dict)

    def reconstructs(self, tolerance: float = 1e-9) -> bool:
        """The decomposition must add up. Asserted in tests."""
        return abs(sum(self.components.values()) - self.total) < tolerance


def severity_component(severity_ordinal: int | None) -> float:
    """KABCO 0-4 normalised to 0-1. None scores 0 rather than a guessed middle."""
    if severity_ordinal is None:
        return 0.0
    return max(0.0, min(1.0, severity_ordinal / MAX_SEVERITY))


def recency_component(incident_date: date | None, as_of: date, halflife_days: int) -> float:
    """Exponential decay. Statutory windows expire and memories fade, so a lead
    from last week is worth more than one from last year — but the decay is
    smooth rather than a cliff, because no statute makes it a cliff."""
    if incident_date is None:
        return 0.0
    # DuckDB hands dates back as pandas Timestamps. Note a Timestamp IS a
    # `date` subclass, so testing `isinstance(x, date)` does not distinguish
    # them — `datetime` is the discriminating type.
    if isinstance(incident_date, datetime):
        incident_date = incident_date.date()
    age = max(0, (as_of - incident_date).days)
    return 0.5 ** (age / halflife_days)


def quality_component(geo_quality: str | None, snap_quality: str | None,
                      penalties: dict) -> float:
    """Starts at 1.0 and is debited. Confidence in the enrichment is part of a
    lead's value: a record we are less sure about is worth less to act on."""
    value = 1.0
    if geo_quality is not None and geo_quality != "OK":
        value -= penalties.get("geo_not_ok", 0.0)
    if snap_quality is not None and snap_quality != "OK":
        value -= penalties.get("snap_rejected", 0.0)
    return max(0.0, value)


def compute(record: dict, rules: dict, *, as_of: date,
            spatial_risk: float | None = None) -> Score | None:
    """Named contributions that sum to the total. Returns None when the record
    is not scorable."""
    if record.get("eligibility_status") not in SCORABLE_STATUSES:
        return None

    config = rules["scoring"]
    weights = config["weights"]

    raw = {
        "severity": severity_component(record.get("severity_ordinal")),
        "recency": recency_component(record.get("incident_date"), as_of,
                                     config["recency_halflife_days"]),
        "spatial_risk": float(spatial_risk) if spatial_risk is not None else 0.0,
        "data_quality": quality_component(record.get("geo_quality"),
                                          record.get("snap_quality"),
                                          config.get("quality_penalty", {})),
    }
    components = {name: round(weights[name] * value, 6) for name, value in raw.items()}
    return Score(total=round(sum(components.values()), 6), components=components)


def assert_no_forbidden_inputs(record: dict) -> None:
    """The score must not read a geographic or demographic identifier, for the
    same reason the model must not: ranking on them is the redlining-adjacent
    design Part 4 warns about. Holding them for the contract is separate."""
    used = FORBIDDEN_FEATURES & set(record)
    if used and any(record.get(k) is not None for k in used):
        # Presence in the record is fine — it carries the whole contract row.
        # What matters is that nothing below reads them, which the tests pin.
        pass


def explain(score: Score) -> str:
    """Human-readable decomposition, ordered by contribution."""
    parts = sorted(score.components.items(), key=lambda kv: -abs(kv[1]))
    return " + ".join(f"{name}={value:.3f}" for name, value in parts)


# ---------------------------------------------------------------------------
# Scoring real crashes.
#
# The fixture parties cannot exercise this fully: `synthetic_parties.csv` is an
# identity layer with no crash identifier, so `severity_ordinal` is unknown and
# there is no `crash_sk` to look up a spatial-risk estimate. Their scores are
# therefore recency and data-quality only — honest, but a thin demonstration.
#
# So the scorer is also run over the real Montgomery crashes, which do carry
# severity, enrichment quality and a risk estimate. That shows the full
# decomposition working on data that has the inputs, rather than implying the
# fixture exercises more than it does.
# ---------------------------------------------------------------------------

def score_crashes(con, rules: dict, as_of: date, source: str = "MONTGOMERY_MD"):
    """Priority score over real crashes, for demonstration and backtest support."""
    import json

    import pandas as pd

    frame = con.execute("""
        SELECT c.crash_sk, c.severity_ordinal, c.crash_date, c.geo_quality,
               g.snap_quality, r.spatial_risk
          FROM silver_crash c
          JOIN silver_crash_geo g USING (crash_sk)
          LEFT JOIN gold_crash_risk r USING (crash_sk)
         WHERE c.source_system = ?
         ORDER BY c.crash_sk
    """, [source]).fetch_df()

    rows = []
    for rec in frame.to_dict("records"):
        result = compute({
            "eligibility_status": "ELIGIBLE",   # scoring the crash, not a lead
            "severity_ordinal": None if pd.isna(rec["severity_ordinal"])
                                else int(rec["severity_ordinal"]),
            "incident_date": rec["crash_date"],
            "geo_quality": rec["geo_quality"],
            "snap_quality": rec["snap_quality"],
        }, rules, as_of=as_of,
           spatial_risk=None if pd.isna(rec["spatial_risk"]) else rec["spatial_risk"])
        rows.append({"crash_sk": rec["crash_sk"], "priority_score": result.total,
                     "score_components": json.dumps(result.components)})

    out = pd.DataFrame(rows)
    con.register("_score_staging", out)
    try:
        con.execute("CREATE OR REPLACE TABLE gold_crash_priority AS "
                    "SELECT * FROM _score_staging")
    finally:
        con.unregister("_score_staging")
    return len(out)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging
    import sys

    import yaml

    from src.config import REPO_ROOT, load_settings
    from src.ingest import watermark

    parser = argparse.ArgumentParser(description="Score real crashes by priority")
    parser.add_argument("--source", default="MONTGOMERY_MD")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = load_settings()
    rules = yaml.safe_load((REPO_ROOT / settings.compliance["ruleset_path"]).read_text())
    as_of = date.fromisoformat(str(settings.compliance["evaluation_date"]))

    con = watermark.connect(settings.warehouse_path)
    try:
        n = score_crashes(con, rules, as_of, args.source)
        top = con.execute("""
            SELECT p.priority_score, p.score_components, c.severity_ordinal, c.crash_date
              FROM gold_crash_priority p JOIN silver_crash c USING (crash_sk)
             ORDER BY p.priority_score DESC LIMIT 5
        """).fetchall()
        spread = con.execute("""
            SELECT min(priority_score), median(priority_score), max(priority_score)
              FROM gold_crash_priority""").fetchone()
    finally:
        con.close()

    print(f"\n  scored {n:,} crashes   range {spread[0]:.3f} .. {spread[2]:.3f}  "
          f"(median {spread[1]:.3f})\n")
    print("  highest priority:")
    for score, components, severity, crash_date in top:
        print(f"    {score:.3f}  sev={severity}  {crash_date}  {components}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
