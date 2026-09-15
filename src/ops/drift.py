"""Drift detection: schema and value dictionaries.

Part 6: *"When the `driver_substance_abuse` dictionary changed, the correct
behavior was an alert, not a silent pass. Build the detector that would have
caught it, and show it firing against the historical data."*

Two detectors, because the obvious one does not work
----------------------------------------------------
A **schema** detector watches column names and types. It would have passed the
`driver_substance_abuse` change silently: the column kept its name and its type,
and only the values inside it changed.

A **cardinality** detector — alert when the distinct-value count moves — fails
worse, in both directions at once. Measured over the real history:

    2023-10   9 distinct,     0 new-scheme rows
    2023-11   9 distinct,     0
    2023-12   9 distinct,     2      <- the change begins here
    2024-01   9 distinct, 1,481

Old tokens left as new ones arrived, so the count held at 9 straight through the
transition and the detector **never fired on it**. Meanwhile it fired in 92 of
140 periods on ordinary month-to-month churn — so an operator would have muted
it years before December 2023, and it would have had nothing to say anyway.

Blind to the change that mattered, noisy about everything else. Only watching the
value *set* — which tokens are present, not how many — catches it.

Both detectors ship. `cardinality_only` is kept precisely so the comparison is
testable rather than asserted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field

import duckdb

from src.config import load_settings
from src.ingest import watermark

log = logging.getLogger("ops.drift")

# Categorical columns worth watching. Chosen because a silent vocabulary change
# in any of them corrupts a downstream mapping — A2, A6 and A7 were all this.
WATCHED = {
    "bronze_montgomery_drivers": ["driver_substance_abuse", "injury_severity",
                                  "collision_type", "vehicle_body_type"],
    "bronze_montgomery_incidents": ["collision_type", "weather", "light",
                                    "surface_condition", "acrs_report_type"],
    "bronze_montgomery_non_motorists": ["injury_severity", "pedestrian_type"],
}

SEVERITY_INFO = "INFO"
SEVERITY_ALERT = "ALERT"


@dataclass
class Finding:
    severity: str
    detector: str
    table: str
    column: str | None
    period: str | None
    detail: str
    tokens: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        where = f"{self.table}.{self.column}" if self.column else self.table
        when = f" [{self.period}]" if self.period else ""
        return f"{self.severity:<5} {self.detector:<9} {where}{when}: {self.detail}"


# --- 1. schema drift --------------------------------------------------------

def schema_drift(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Columns added, removed or retyped between consecutive runs.

    Reads `source_schema_snapshot`, which has captured the source-declared
    schema on every ingest since Phase 1a — built for this, not retrofitted.
    """
    findings: list[Finding] = []
    rows = con.execute("""
        SELECT source, dataset, fields_json, types_json, captured_at
          FROM source_schema_snapshot ORDER BY source, dataset, captured_at
    """).fetchall()

    previous: dict[tuple[str, str], tuple[set, dict]] = {}
    for source, dataset, fields_json, types_json, captured_at in rows:
        fields = json.loads(fields_json)
        types = json.loads(types_json or "[]")
        typed = dict(zip(fields, types)) if len(types) == len(fields) else {}
        key = (source, dataset)

        if key in previous:
            old_fields, old_typed = previous[key]
            added = set(fields) - old_fields
            removed = old_fields - set(fields)
            retyped = {
                f for f in set(fields) & old_fields
                if old_typed.get(f) and typed.get(f) and old_typed[f] != typed[f]
            }
            for label, cols in (("added", added), ("removed", removed), ("retyped", retyped)):
                if cols:
                    findings.append(Finding(
                        SEVERITY_ALERT, "schema", f"{source}.{dataset}", None,
                        str(captured_at)[:10], f"columns {label}: {sorted(cols)}",
                        sorted(cols)))
        previous[key] = (set(fields), typed)
    return findings


# --- 2. value / dictionary drift -------------------------------------------

def value_sets_by_period(con: duckdb.DuckDBPyConnection, table: str, column: str,
                         date_column: str = "crash_date_time") -> list[tuple[str, set]]:
    """Distinct non-null values per month, oldest first."""
    rows = con.execute(f"""
        SELECT strftime(TRY_CAST("{date_column}" AS TIMESTAMP), '%Y-%m') AS period,
               "{column}" AS value
          FROM "{table}"
         WHERE "{column}" IS NOT NULL
           AND TRY_CAST("{date_column}" AS TIMESTAMP) IS NOT NULL
         GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    grouped: dict[str, set] = {}
    for period, value in rows:
        grouped.setdefault(period, set()).add(value)
    return sorted(grouped.items())


def value_drift(con: duckdb.DuckDBPyConnection, table: str, column: str,
                *, min_baseline_periods: int = 3) -> list[Finding]:
    """Alert when a period introduces tokens absent from every prior period.

    The baseline is cumulative rather than the immediately preceding period: a
    value that appears, vanishes for a month and returns is not new, and
    alerting on it would train the operator to ignore the detector.
    """
    findings: list[Finding] = []
    periods = value_sets_by_period(con, table, column)
    seen: set[str] = set()

    for index, (period, values) in enumerate(periods):
        novel = values - seen
        if novel and index >= min_baseline_periods:
            findings.append(Finding(
                SEVERITY_ALERT, "value", table, column, period,
                f"{len(novel)} token(s) never seen in {index} prior periods",
                sorted(novel)))
        seen |= values
    return findings


def cardinality_only(con: duckdb.DuckDBPyConnection, table: str,
                     column: str) -> list[Finding]:
    """The detector that does NOT work, kept so the claim is testable.

    Watches the distinct-value count. Across the driver_substance_abuse
    transition the count holds at 9, so this never fires on the change — while
    firing 92 times elsewhere on routine churn.
    """
    findings: list[Finding] = []
    periods = value_sets_by_period(con, table, column)
    for (prev_period, prev), (period, current) in zip(periods, periods[1:]):
        if len(current) != len(prev):
            findings.append(Finding(
                SEVERITY_INFO, "cardinality", table, column, period,
                f"distinct count {len(prev)} -> {len(current)}"))
    return findings


def run_all(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    findings = schema_drift(con)
    for table, columns in WATCHED.items():
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [table]).fetchone()[0]
        if not exists:
            continue
        available = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall()}
        for column in columns:
            if column in available:
                findings.extend(value_drift(con, table, column))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect schema and dictionary drift")
    parser.add_argument("--replay", action="store_true",
                        help="walk the full bronze history and report where each would fire")
    parser.add_argument("--column", help="focus on one table.column")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    con = watermark.connect(load_settings().warehouse_path)
    try:
        if args.column:
            table, column = args.column.split(".", 1)
            findings = value_drift(con, table, column)
            baseline = cardinality_only(con, table, column)
        else:
            findings = run_all(con)
            baseline = cardinality_only(
                con, "bronze_montgomery_drivers", "driver_substance_abuse")
    finally:
        con.close()

    alerts = [f for f in findings if f.severity == SEVERITY_ALERT]
    print(f"\n  {len(alerts)} alert(s)\n")
    for finding in alerts[:20]:
        print(f"    {finding}")
        for token in finding.tokens[:4]:
            print(f"        + {token!r}")

    if args.replay:
        fired = {f.period for f in baseline}
        print("\n  --- the detector that would NOT have caught it ---")
        print(f"    cardinality-only fired {len(baseline)} times across the history")
        print(f"    but at 2023-12 (when the dictionary changed)? "
              f"{'yes' if '2023-12' in fired else 'NO'}")
        print(f"    at 2024-01 (when it took over)?             "
              f"{'yes' if '2024-01' in fired else 'NO'}")
        print("    The count holds at 9 through the transition, so it is blind to")
        print("    the change — and noisy enough elsewhere to have been muted anyway.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
