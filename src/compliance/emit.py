"""Build, validate and write contract-conformant lead records.

    python -m src.compliance.emit                    # live_telephone
    python -m src.compliance.emit --channel written

Every record is validated against `contracts/lead_output.schema.json` before it
is written, and a record that fails is an error rather than a warning. The
contract is described in the scaffold as "the interface the downstream consumer
depends on"; until now nothing had ever checked against it.

Fields belonging to unbuilt phases (census geography, road snapping, scoring)
are emitted as **null**. A fabricated census tract would be worse than an absent
one — it would be indistinguishable from a real one downstream.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

from src.config import REPO_ROOT, load_settings
from src.geo import census, h3_index, roads
from src.scoring import score as scoring
from src.ingest import watermark

from . import lineage, vault, windows
from .engine import EligibilityEngine
from .evaluate import evaluation_date, load_fixture, load_identities

log = logging.getLogger("compliance.emit")

CONTRACT = REPO_ROOT / "contracts" / "lead_output.schema.json"
OUTPUT_DIR = REPO_ROOT / "output"
MAX_SAMPLE_ROWS = 100  # output/README.md

# Column order for the CSV. Nested contract groups are flattened with a dot so
# the file stays readable without losing which group a field belongs to.
CSV_COLUMNS = [
    "lead_id", "source_system", "source_record_id", "ingested_at",
    "incident_date", "report_filing_date", "jurisdiction",
    "geo.zip5", "geo.census_tract", "geo.census_bg", "geo.h3_r8",
    "geo.iana_timezone", "geo.road_class", "geo.snap_distance_m",
    "severity_ordinal",
    "contact.phone_token", "contact.line_type", "contact.line_type_asof",
    "contact.dnc_scrub_asof", "contact.rnd_response",
    "contact.calling_window_local.earliest", "contact.calling_window_local.latest",
    "contact.calling_window_local.basis",
    "eligibility_status", "blocked_until_date", "reason_codes", "legal_basis",
    "decision_lineage_id", "evaluated_at", "ruleset_version",
    "priority_score", "score_components",
]


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def build_record(record: dict, decision, *, token: str | None, ingested_at: str,
                 as_of: date, geography: tuple | None = None,
                 road: dict | None = None, rules: dict | None = None) -> dict:
    """Assemble one record in the contract's nested shape."""
    window = record.get("_calling_window")
    scrub_age = record.get("dnc_scrub_age_days")
    # Derived from the FROZEN evaluation date, not the wall clock. Using
    # `decision.evaluated_at` here silently dated every scrub to whenever the
    # job happened to run, which drifts the 31-day staleness SLA out from under
    # the decision that was made against it.
    evaluated_on = as_of

    payload = {
        "lead_id": f"lead-{record['party_id']}",
        # The fixture is a synthetic identity layer, not one of the three real
        # feeds, so OTHER is the only honest value in the enum.
        "source_system": "OTHER",
        "source_record_id": record["party_id"],
        "ingested_at": ingested_at,
        "incident_date": _iso(record.get("incident_date")),
        "report_filing_date": _iso(record.get("report_filing_date")),
        "jurisdiction": record.get("jurisdiction"),
        "geo": {
            # ZIP5 is retained deliberately: 18 U.S.C. 2725(3) excludes it from
            # "personal information", making it the legitimate aggregation key.
            "zip5": record.get("zip5"),
            "census_bg": (geography or (None, None))[0],
            "census_tract": (geography or (None, None))[1],
            "h3_r8": h3_index.cell(record.get("latitude"), record.get("longitude")),
            "iana_timezone": record.get("_iana_timezone"),
            # Only an accepted snap is enrichment. A road class inherited from
            # a segment 400 m away is fiction, so it is withheld rather than
            # emitted with a caveat nobody downstream will read.
            "road_class": (road or {}).get("road_class")
                          if (road or {}).get("snap_quality") == "OK" else None,
            "snap_distance_m": (road or {}).get("snap_distance_m"),
        },
        # Needs a crash link the fixture deliberately does not provide.
        "severity_ordinal": None,
        "contact": {
            "phone_token": token,
            "line_type": record.get("line_type_reported"),
            "line_type_asof": None,    # fixture carries no as-of date
            "dnc_scrub_asof": (
                f"{evaluated_on - timedelta(days=scrub_age)}T00:00:00+00:00"
                if scrub_age is not None else None
            ),
            "rnd_response": record.get("rnd_response"),
            "calling_window_local": (
                {
                    "earliest": window.earliest.strftime("%H:%M"),
                    "latest": window.latest.strftime("%H:%M"),
                    "basis": window.basis,
                }
                if window else None
            ),
        },
        # The fixture supplies consent as a bare boolean with no provenance, so
        # there is nothing honest to put here. See rules.yaml require_provenance.
        "consent": None,
        "eligibility_status": decision.status,
        "blocked_until_date": _iso(decision.blocked_until_date),
        "reason_codes": [str(c) for c in decision.reason_codes],
        "legal_basis": list(decision.legal_basis),
        "decision_lineage_id": decision.decision_lineage_id,
        "evaluated_at": decision.evaluated_at.isoformat(),
        "ruleset_version": decision.ruleset_version,
        "priority_score": None,
        "score_components": None,
    }
    if rules:
        # Scored from the decision plus known crash facts. Only ELIGIBLE and
        # BLOCKED_UNTIL are scorable — see src/scoring/score.py.
        result = scoring.compute({
            "eligibility_status": decision.status,
            "severity_ordinal": payload["severity_ordinal"],
            "incident_date": record.get("incident_date"),
            "geo_quality": "OK",
            "snap_quality": (road or {}).get("snap_quality"),
        }, rules, as_of=as_of, spatial_risk=(road or {}).get("spatial_risk"))
        if result is not None:
            payload["priority_score"] = result.total
            payload["score_components"] = result.components

    vault.assert_no_direct_identifiers(payload)
    return payload


def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(CONTRACT.read_text()))


def flatten(payload: dict) -> dict:
    """Contract shape -> flat CSV row."""
    geo, contact = payload.get("geo") or {}, payload.get("contact") or {}
    window = contact.get("calling_window_local") or {}
    row = {
        **{k: payload.get(k) for k in (
            "lead_id", "source_system", "source_record_id", "ingested_at",
            "incident_date", "report_filing_date", "jurisdiction",
            "severity_ordinal", "eligibility_status", "blocked_until_date",
            "decision_lineage_id", "evaluated_at", "ruleset_version", "priority_score")},
        **{f"geo.{k}": geo.get(k) for k in (
            "zip5", "census_tract", "census_bg", "h3_r8", "iana_timezone",
            "road_class", "snap_distance_m")},
        **{f"contact.{k}": contact.get(k) for k in (
            "phone_token", "line_type", "line_type_asof", "dnc_scrub_asof", "rnd_response")},
        "contact.calling_window_local.earliest": window.get("earliest"),
        "contact.calling_window_local.latest": window.get("latest"),
        "contact.calling_window_local.basis": window.get("basis"),
        "score_components": json.dumps(payload["score_components"])
                            if payload.get("score_components") else None,
        "reason_codes": "|".join(payload.get("reason_codes") or []),
        "legal_basis": " || ".join(payload.get("legal_basis") or []),
    }
    return {k: row.get(k) for k in CSV_COLUMNS}


def write_csv(path: Path, payloads: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for payload in payloads[:MAX_SAMPLE_ROWS]:
            w.writerow(flatten(payload))
    return min(len(payloads), MAX_SAMPLE_ROWS)


def run(channel: str | None = None, *, out_name: str | None = None) -> dict:
    settings = load_settings()
    today = evaluation_date(settings)
    key = settings.compliance.get("token_key", "")

    engine = EligibilityEngine(str(REPO_ROOT / settings.compliance["ruleset_path"]))
    if channel:
        engine.channel = channel

    records = load_fixture()
    identities = load_identities()
    geographies = census.lookup(records, settings=settings)
    road_attrs = roads.lookup(records, settings=settings)
    tokens = {i["party_id"]: vault.phone_token(i["phone_e164"], key) for i in identities}

    con = watermark.connect(settings.warehouse_path)
    lineage.ensure_schema(con)
    # Direct identifiers go to the vault and nowhere else.
    vault.store(con, identities, key)

    check = validator()
    ingested_at = lineage.utcnow().isoformat()
    payloads, emitted = [], []

    for record in records:
        decision, verdicts = engine.evaluate_with_lineage(record, today=today)
        lineage.write(con, decision=decision, subject_id=record["party_id"],
                      channel=engine.channel, jurisdiction=record["jurisdiction"],
                      verdicts=verdicts, record=record)
        payload = build_record(
            record, decision,
            token=tokens.get(record["party_id"]),
            ingested_at=ingested_at,
            as_of=today,
            geography=geographies.get(record["party_id"]),
            road=road_attrs.get(record["party_id"]),
            rules=engine.rules,
        )
        errors = sorted(check.iter_errors(payload), key=lambda e: e.path)
        if errors:
            raise ValueError(
                f"{record['party_id']} violates the output contract: "
                + "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
            )
        payloads.append(payload)
        # The feed carries only records the contact centre may act on. An
        # INELIGIBLE record is a decision, not a lead.
        if decision.status != "INELIGIBLE":
            emitted.append(payload)

    _persist_gold(con, payloads, engine.channel)
    con.close()

    name = out_name or ("sample_leads.csv" if engine.channel == "live_telephone"
                        else f"sample_leads_{engine.channel}.csv")
    written = write_csv(OUTPUT_DIR / name, emitted)
    counts = {"ELIGIBLE": 0, "BLOCKED_UNTIL": 0, "INELIGIBLE": 0}
    for p in payloads:
        counts[p["eligibility_status"]] += 1
    return {"channel": engine.channel, "evaluated": len(payloads),
            "emitted": len(emitted), "written": written, "file": name,
            "counts": counts, "version": engine.ruleset_version}


def _persist_gold(con, payloads: list[dict], channel: str) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold_lead_decision (
            lead_id VARCHAR, channel VARCHAR, source_record_id VARCHAR,
            jurisdiction VARCHAR, eligibility_status VARCHAR,
            blocked_until_date DATE, reason_codes VARCHAR,
            decision_lineage_id VARCHAR, ruleset_version VARCHAR,
            payload VARCHAR, emitted_at TIMESTAMPTZ
        )""")
    con.execute("DELETE FROM gold_lead_decision WHERE channel = ?", [channel])
    for p in payloads:
        con.execute(
            """INSERT INTO gold_lead_decision VALUES (?,?,?,?,?,?,?,?,?,?,now())""",
            [p["lead_id"], channel, p["source_record_id"], p["jurisdiction"],
             p["eligibility_status"], p["blocked_until_date"],
             "|".join(p["reason_codes"]), p["decision_lineage_id"],
             p["ruleset_version"], json.dumps(p)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit contract-conformant lead records")
    parser.add_argument("--channel")
    parser.add_argument("--out")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")

    result = run(args.channel, out_name=args.out)
    print(f"\n  ruleset {result['version']} · channel {result['channel']}")
    c = result["counts"]
    print(f"    evaluated          {result['evaluated']:>3}")
    print(f"      eligible now     {c['ELIGIBLE']:>3}")
    print(f"      blocked until    {c['BLOCKED_UNTIL']:>3}  (in the feed, not callable yet)")
    print(f"      ineligible       {c['INELIGIBLE']:>3}  (withheld from the feed)")
    print(f"    written to file    {result['written']:>3}  -> output/{result['file']}")
    if result["emitted"] == 0:
        print("\n    Header only, no data rows. That is the honest output for this")
        print("    channel, not a pipeline failure — see MEMO.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
