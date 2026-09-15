"""Output contract conformance.

`contracts/lead_output.schema.json` is described in the scaffold as "the
interface the downstream consumer depends on", and until this file existed
nothing had ever validated against it. These tests are the check.
"""

from __future__ import annotations

import copy
import csv
import json
from datetime import date

import pytest

from src.compliance import emit, vault
from src.compliance.engine import EligibilityEngine
from src.compliance.evaluate import load_fixture, load_identities

TODAY = date(2026, 9, 1)
KEY = "test-key"


@pytest.fixture(scope="module")
def payloads():
    """Every fixture record, evaluated on the written channel and built."""
    engine = EligibilityEngine("compliance/rules.yaml")
    engine.channel = "written"
    identities = {i["party_id"]: i for i in load_identities()}
    out = []
    for record in load_fixture():
        decision = engine.evaluate(record, today=TODAY)
        token = vault.phone_token(identities[record["party_id"]]["phone_e164"], KEY)
        out.append(emit.build_record(
            record, decision, token=token,
            ingested_at="2026-09-01T00:00:00+00:00", as_of=TODAY))
    return out


# --- the contract -----------------------------------------------------------


def test_every_record_validates_against_the_contract(payloads):
    check = emit.validator()
    for payload in payloads:
        errors = sorted(check.iter_errors(payload), key=lambda e: list(e.path))
        assert not errors, (
            f"{payload['source_record_id']}: "
            + "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
        )


def test_required_fields_are_present_and_non_null(payloads):
    required = json.loads(emit.CONTRACT.read_text())["required"]
    for payload in payloads:
        for field in required:
            assert payload.get(field) is not None, f"{field} is null"


def test_reason_codes_are_never_empty(payloads):
    """minItems: 1 — a decision with no stated reason is not a decision."""
    assert all(payload["reason_codes"] for payload in payloads)


def test_every_reason_code_has_a_matching_citation(payloads):
    for payload in payloads:
        assert payload["legal_basis"], payload["source_record_id"]
        assert not any("no citation recorded" in c for c in payload["legal_basis"])


def test_source_system_is_within_the_enum(payloads):
    """The fixture is a synthetic identity layer, not one of the three real
    feeds, so OTHER is the only honest value."""
    assert {p["source_system"] for p in payloads} == {"OTHER"}


# --- the tokenisation boundary ----------------------------------------------


def test_no_direct_identifier_reaches_the_emitted_record(payloads):
    """Checked against the RAW fixture, not the analytic record — the analytic
    record already excludes them, and testing it against itself proves nothing."""
    blob = json.dumps(payloads)
    for identity in load_identities():
        for field in ("full_name", "street_address", "phone_e164"):
            assert identity[field] not in blob, (
                f"direct identifier {identity[field]!r} leaked into output")


def test_the_analytic_record_never_holds_a_full_phone_number():
    """Structural, not enforced by remembering to exclude it: the fixture reader
    keeps the NPA only, so the calling-window fallback works and the rest of the
    number never enters the analytic path."""
    for record in load_fixture():
        assert "phone_e164" not in record
        assert record["phone_npa"] and len(record["phone_npa"]) == 3


def test_the_vault_actually_holds_the_identities():
    """The boundary is only real if the vault side is populated — an empty vault
    plus a clean analytic record is not separation, it is data loss."""
    identities = load_identities()
    assert len(identities) == 40
    assert all(i["full_name"] and i["street_address"] and i["phone_e164"]
               for i in identities)


def test_phone_token_is_deterministic_and_keyed():
    a = vault.phone_token("+13015550101", KEY)
    assert a == vault.phone_token("+13015550101", KEY)
    assert a != vault.phone_token("+13015550101", "other-key")
    assert a != vault.phone_token("+13015550102", KEY)


def test_unkeyed_tokenisation_is_refused():
    """An unkeyed hash of a 10-digit number is reversible by enumeration; that
    is an encoding, not a token."""
    with pytest.raises(vault.MissingTokenKey):
        vault.phone_token("+13015550101", "")


def test_zip5_is_deliberately_retained(payloads):
    """18 U.S.C. 2725(3) excludes the 5-digit ZIP from 'personal information',
    which makes it the legitimate geographic aggregation key. Tokenising it
    would discard the carve-out for nothing."""
    assert all(p["geo"]["zip5"] for p in payloads)
    assert all(len(p["geo"]["zip5"]) == 5 for p in payloads)


def test_assert_no_direct_identifiers_catches_a_leak():
    with pytest.raises(ValueError, match="direct identifier"):
        vault.assert_no_direct_identifiers({"lead_id": "x", "phone_e164": "+13015550101"})


# --- honesty about unbuilt phases -------------------------------------------


@pytest.mark.parametrize("group,field", [
    ("geo", "census_tract"), ("geo", "census_bg"),      # Phase 3b
    ("geo", "road_class"), ("geo", "snap_distance_m"),  # Phase 3c
])
def test_unbuilt_enrichments_emit_null_not_a_placeholder(payloads, group, field):
    """A fabricated census tract is worse than an absent one — downstream it is
    indistinguishable from a real one."""
    assert all(p[group][field] is None for p in payloads)


def test_score_is_null_until_phase_4(payloads):
    assert all(p["priority_score"] is None for p in payloads)


def test_severity_is_null_because_the_fixture_has_no_crash_link(payloads):
    assert all(p["severity_ordinal"] is None for p in payloads)


# --- derived values use the frozen evaluation date --------------------------


def test_dnc_scrub_asof_is_anchored_to_the_frozen_date(payloads):
    """Deriving it from the wall clock silently re-dates every scrub to whenever
    the job ran, drifting the 31-day SLA out from under the decision."""
    by_id = {p["source_record_id"]: p for p in payloads}
    # P001 carries dnc_scrub_age_days = 3 against a frozen 2026-09-01.
    assert by_id["P001"]["contact"]["dnc_scrub_asof"].startswith("2026-08-29")


def test_calling_window_reflects_the_stricter_state_rule(payloads):
    """Florida closes at 20:00 per Fla. Stat. 501.616(6); Maryland takes the
    federal 21:00."""
    by_id = {p["source_record_id"]: p for p in payloads}
    assert by_id["P009"]["contact"]["calling_window_local"]["latest"] == "20:00"
    assert by_id["P001"]["contact"]["calling_window_local"]["latest"] == "21:00"


def test_calling_window_comes_from_coordinates(payloads):
    by_id = {p["source_record_id"]: p for p in payloads}
    assert by_id["P007"]["geo"]["iana_timezone"] == "America/Denver"  # El Paso
    assert by_id["P008"]["geo"]["iana_timezone"] == "America/Chicago"  # Pensacola


# --- consent provenance -----------------------------------------------------


def test_requiring_consent_provenance_zeroes_the_written_channel():
    """The fixture's consent is a bare boolean with no disclosure text, hash,
    URL, timestamp, IP, user agent or seller chain. The burden of proof is on
    the caller, so with provenance required there is nothing left."""
    engine = EligibilityEngine("compliance/rules.yaml")
    engine.channel = "written"
    engine.rules = copy.deepcopy(engine.rules)
    engine.rules["contact_gates"]["consent"]["require_provenance"] = True

    statuses = [engine.evaluate(r, today=TODAY).status for r in load_fixture()]
    assert statuses.count("ELIGIBLE") == 0


def test_provenance_off_by_default_is_explicit():
    """Off because the fixture is a harness — not because it was overlooked."""
    engine = EligibilityEngine("compliance/rules.yaml")
    assert engine.rules["contact_gates"]["consent"]["require_provenance"] is False
    assert engine.rules["contact_gates"]["consent"]["provenance_fields"]


# --- the files --------------------------------------------------------------


def test_live_telephone_sample_has_a_header_and_no_rows():
    """The honest output for this channel. The brief: 'if your pipeline's honest
    output for a jurisdiction is an empty file, submit the empty file with its
    header and explain it in the memo.'"""
    path = emit.OUTPUT_DIR / "sample_leads.csv"
    if not path.exists():
        pytest.skip("run: python -m src.compliance.emit")
    rows = list(csv.DictReader(path.open()))
    assert rows == []
    assert path.read_text().splitlines()[0].startswith("lead_id,")


def test_sample_files_respect_the_row_cap():
    """output/README.md: maximum 100 rows."""
    for path in emit.OUTPUT_DIR.glob("sample_leads*.csv"):
        assert len(list(csv.DictReader(path.open()))) <= emit.MAX_SAMPLE_ROWS


def test_written_sample_contains_no_ineligible_records():
    """An INELIGIBLE record is a decision, not a lead. BLOCKED_UNTIL stays in
    the feed because the contract carries blocked_until_date for exactly that."""
    path = emit.OUTPUT_DIR / "sample_leads_written.csv"
    if not path.exists():
        pytest.skip("run: python -m src.compliance.emit --channel written")
    statuses = {r["eligibility_status"] for r in csv.DictReader(path.open())}
    assert "INELIGIBLE" not in statuses
