"""Contract enforcement at every layer boundary.

Part 6: contracts "with enforced types, nullability, ranges, and referential
expectations". Hand-rolled — the brief explicitly permits it, and Great
Expectations is a large dependency for checks this shape.

**A violation fails.** A contract that warns is documentation with extra steps.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from src.config import REPO_ROOT, load_settings
from src.ingest import watermark

log = logging.getLogger("contracts.validate")

CONTRACT_DIR = REPO_ROOT / "contracts"


@dataclass
class Violation:
    contract: str
    check: str
    detail: str
    count: int | None = None

    def __str__(self) -> str:
        n = f" ({self.count:,})" if self.count is not None else ""
        return f"{self.contract} :: {self.check}{n} — {self.detail}"


def load_contracts(directory: Path = CONTRACT_DIR) -> list[dict]:
    return [yaml.safe_load(p.read_text()) for p in sorted(directory.glob("*.yaml"))]


def _table_exists(con, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [table]).fetchone()[0])


def check_columns(con, contract: dict) -> list[Violation]:
    table = contract["table"]
    violations: list[Violation] = []
    present = {r[0]: r[1] for r in con.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?",
        [table]).fetchall()}

    for column, spec in (contract.get("columns") or {}).items():
        if column not in present:
            violations.append(Violation(table, f"{column}.exists", "column is absent"))
            continue

        if not spec.get("nullable", True):
            nulls = con.execute(
                f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NULL').fetchone()[0]
            if nulls:
                violations.append(Violation(table, f"{column}.not_null",
                                            "nulls in a non-nullable column", nulls))

        if spec.get("unique"):
            dupes = con.execute(
                f'SELECT count(*) - count(DISTINCT "{column}") FROM "{table}"').fetchone()[0]
            if dupes:
                violations.append(Violation(table, f"{column}.unique",
                                            "duplicate values", dupes))

        if "allowed" in spec:
            allowed = ", ".join(f"'{v}'" for v in spec["allowed"])
            bad = con.execute(
                f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NOT NULL '
                f'AND "{column}" NOT IN ({allowed})').fetchone()[0]
            if bad:
                violations.append(Violation(table, f"{column}.allowed",
                                            f"values outside {spec['allowed']}", bad))

        for bound, op in (("min", "<"), ("max", ">")):
            if bound in spec:
                bad = con.execute(
                    f'SELECT count(*) FROM "{table}" '
                    f'WHERE TRY_CAST("{column}" AS DOUBLE) {op} {spec[bound]}').fetchone()[0]
                if bad:
                    violations.append(Violation(table, f"{column}.{bound}",
                                                f"values {op} {spec[bound]}", bad))

        if "matches" in spec:
            bad = con.execute(
                f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NOT NULL '
                f"AND NOT regexp_matches(\"{column}\", '{spec['matches']}')").fetchone()[0]
            if bad:
                violations.append(Violation(table, f"{column}.matches",
                                            f"values not matching {spec['matches']}", bad))
    return violations


def check_grain(con, contract: dict) -> list[Violation]:
    grain = (contract.get("grain") or {}).get("unique_by")
    if not grain:
        return []
    table = contract["table"]
    keys = ", ".join(f'"{c}"' for c in grain)
    dupes = con.execute(
        f'SELECT count(*) FROM (SELECT {keys} FROM "{table}" '
        f"GROUP BY {keys} HAVING count(*) > 1)").fetchone()[0]
    return [Violation(table, "grain", f"{grain} is not unique", dupes)] if dupes else []


def check_expectations(con, contract: dict) -> list[Violation]:
    """Referential and business expectations, expressed as SQL that must return
    the expected value. Direction matters — see bronze_montgomery.yaml."""
    violations: list[Violation] = []
    for expectation in contract.get("expectations") or []:
        actual = con.execute(expectation["sql"]).fetchone()[0]
        if actual != expectation["expect"]:
            violations.append(Violation(
                contract["table"], expectation["name"],
                expectation.get("description", "").strip().split("\n")[0], actual))
    return violations


def validate(con, contracts: list[dict] | None = None) -> list[Violation]:
    contracts = contracts if contracts is not None else load_contracts()
    violations: list[Violation] = []
    for contract in contracts:
        if not _table_exists(con, contract["table"]):
            log.warning("%s: table absent, skipping", contract["table"])
            continue
        violations.extend(check_columns(con, contract))
        violations.extend(check_grain(con, contract))
        violations.extend(check_expectations(con, contract))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate layer-boundary data contracts")
    parser.add_argument("--contract", help="validate one contract file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    contracts = ([yaml.safe_load((CONTRACT_DIR / args.contract).read_text())]
                 if args.contract else load_contracts())
    con = watermark.connect(load_settings().warehouse_path)
    try:
        violations = validate(con, contracts)
    finally:
        con.close()

    checked = [c["table"] for c in contracts]
    print(f"\n  contracts: {', '.join(checked)}")
    if not violations:
        print("  all checks passed\n")
        return 0
    print(f"\n  {len(violations)} VIOLATION(S)\n")
    for violation in violations:
        print(f"    {violation}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
