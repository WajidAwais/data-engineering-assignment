"""Evaluate the fixture party records against the compliance ruleset.

    python -m src.compliance.evaluate
    python -m src.compliance.evaluate --channel written

`fixtures/synthetic_parties.csv` is the lead population for this phase. It does
NOT join to `silver_crash` — it has no crash identifier, because none of the
three real sources carries callable identity (Tex. Transp. Code 550.065(f)
strips it, Montgomery publishes an opaque GUID, Maryland's person table is
titled "(Anonymized)", FARS has no names). That absence is the statutory design,
not a gap in the fixture, and the memo has to say so.

Evaluation date is frozen from config rather than taken from the clock: the
fixture's dates are anchored to 2026-09-01, and letting "today" drift would
silently change which records are blocked.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from src.config import CONFIG_DIR, REPO_ROOT, load_settings
from src.ingest import watermark

from . import lineage
from .engine import EligibilityEngine

log = logging.getLogger("compliance.evaluate")

FIXTURE = REPO_ROOT / "fixtures" / "synthetic_parties.csv"

_BOOL = {"true": True, "false": False, "": None}


def _parse_row(row: dict) -> dict:
    """Fixture CSV -> typed record. Field names map to the ruleset's vocabulary."""
    def as_date(value):
        return date.fromisoformat(value) if value else None

    return {
        "party_id": row["party_id"],
        "jurisdiction": row["state"],
        "zip5": row["zip5"],
        # NPA only. The full number is a direct identifier and lives in the
        # vault; the calling-window fallback needs the area code and nothing
        # more, so carrying the rest through the analytic path would be gratuitous.
        "phone_npa": row["phone_e164"][2:5] if row.get("phone_e164") else None,
        "latitude": float(row["party_latitude"]),
        "longitude": float(row["party_longitude"]),
        "incident_date": as_date(row["incident_date"]),
        "report_filing_date": as_date(row["report_filing_date"]),
        "line_type_reported": row["line_type_reported"] or None,
        "rnd_response": row["rnd_response"] or None,
        "on_national_dnc": _BOOL.get(row["on_national_dnc"].lower()),
        "dnc_scrub_age_days": int(row["dnc_scrub_age_days"]) if row["dnc_scrub_age_days"] else None,
        "consent_on_file": _BOOL.get(row["consent_on_file"].lower()),
        "consent_revoked": _BOOL.get(row["consent_revoked"].lower()),
        # The supplied fixture carries no internal-DNC or EBR columns, so both
        # read as absent here and the gates are inert against it. They are
        # exercised directly in tests/test_compliance.py. Absent is the safe
        # reading for each: no suppression entry means not suppressed, and an
        # undated EBR claim is not an EBR.
        "on_internal_dnc": _BOOL.get((row.get("on_internal_dnc") or "").lower()),
        "ebr_basis": row.get("ebr_basis") or None,
        "ebr_basis_date": as_date(row.get("ebr_basis_date")),
        # Provenance is affirmatively known for the fixture: it is synthetic and
        # its origin is documented. A real record would have to prove this.
        "provenance_known": True,
        "fixture_note": row.get("fixture_note") or None,
    }


def load_fixture(path: Path = FIXTURE) -> list[dict]:
    """Analytic records. Direct identifiers are NOT included — see load_identities."""
    with path.open(newline="", encoding="utf-8") as fh:
        return [_parse_row(r) for r in csv.DictReader(fh)]


def load_identities(path: Path = FIXTURE) -> list[dict]:
    """Raw identity rows, for the vault only.

    Kept as a separate reader so the tokenisation boundary is visible in the
    call graph rather than enforced by remembering to exclude fields.
    """
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            {"party_id": r["party_id"], "full_name": r["full_name"],
             "street_address": r["street_address"], "phone_e164": r["phone_e164"]}
            for r in csv.DictReader(fh)
        ]


def evaluation_date(settings) -> date:
    raw = settings.compliance.get("evaluation_date")
    if not raw:
        raise ValueError(
            "compliance.evaluation_date is not set. The fixture is anchored to "
            "2026-09-01; letting it drift silently changes which records block."
        )
    return date.fromisoformat(str(raw))


def run(channel: str | None = None, *, persist: bool = True) -> dict:
    settings = load_settings()
    today = evaluation_date(settings)
    engine = EligibilityEngine(str(REPO_ROOT / settings.compliance["ruleset_path"]))
    if channel:
        engine.channel = channel

    records = load_fixture()
    log.info("evaluating %d fixture parties on channel=%s as of %s",
             len(records), engine.channel, today)

    con = watermark.connect(settings.warehouse_path) if persist else None
    if con:
        lineage.ensure_schema(con)

    statuses, reasons, decisions = Counter(), Counter(), []
    for record in records:
        decision, verdicts = engine.evaluate_with_lineage(record, today=today)
        statuses[decision.status] += 1
        for code in decision.reason_codes:
            reasons[str(code)] += 1
        decisions.append((record, decision))
        if con:
            lineage.write(con, decision=decision, subject_id=record["party_id"],
                          channel=engine.channel, jurisdiction=record["jurisdiction"],
                          verdicts=verdicts, record=record)
    if con:
        con.close()

    return {"today": today, "channel": engine.channel,
            "version": engine.ruleset_version, "statuses": statuses,
            "reasons": reasons, "decisions": decisions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate fixture parties for contact eligibility")
    parser.add_argument("--channel", help="override the ruleset channel (e.g. written)")
    parser.add_argument("--no-persist", action="store_true", help="skip writing lineage")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")

    result = run(args.channel, persist=not args.no_persist)

    print(f"\n  ruleset {result['version']} · channel {result['channel']} · as of {result['today']}\n")
    for status in ("ELIGIBLE", "BLOCKED_UNTIL", "INELIGIBLE"):
        print(f"    {status:<16} {result['statuses'].get(status, 0):>3}")

    print("\n  exclusion table (Part 7 asks for this one, not the inclusion table):\n")
    for code, n in result["reasons"].most_common():
        print(f"    {n:>3}  {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
