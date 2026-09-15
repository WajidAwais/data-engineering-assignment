"""The compliance gate.

Part 5 is the only hard-fail section, so these tests check the *control*, not
just the code: default disposition, that no legal constant lives in Python, and
that a new jurisdiction is a data change rather than a code change — the thing
the live defence promises to try ("Ohio just enacted a 45-day accident
solicitation window, effective in three weeks").
"""

from __future__ import annotations

import csv
from datetime import date, time

import pytest
import yaml

from src.compliance import windows
from src.compliance.engine import EligibilityEngine, load_blackout_windows
from src.compliance.reason_codes import ReasonCode
from src.geo.timezone import TimezoneResolver

RULESET = "compliance/rules.yaml"
TODAY = date(2026, 9, 1)  # frozen per fixtures/README.md


@pytest.fixture(scope="module")
def engine():
    return EligibilityEngine(RULESET)


@pytest.fixture(scope="module")
def rules():
    return yaml.safe_load(open(RULESET))


def _record(**overrides) -> dict:
    """A record that clears every gate, so each test can break exactly one."""
    base = {
        "party_id": "TEST", "jurisdiction": "MD",
        "latitude": 39.084, "longitude": -77.1528, "phone_npa": "301",
        "incident_date": date(2026, 6, 1), "report_filing_date": date(2026, 6, 2),
        "line_type_reported": "landline", "rnd_response": "NO",
        "on_national_dnc": False, "dnc_scrub_age_days": 3,
        "consent_on_file": True, "consent_revoked": False, "provenance_known": True,
    }
    base.update(overrides)
    return base


# --- the default disposition ------------------------------------------------


def test_empty_record_is_ineligible(engine):
    """Eligibility is proven, never assumed."""
    assert engine.evaluate({}, today=TODAY).status == "INELIGIBLE"


def test_every_decision_carries_a_lineage_id_and_version(engine):
    decision = engine.evaluate(_record(), today=TODAY)
    assert decision.decision_lineage_id
    assert decision.ruleset_version == engine.ruleset_version
    assert decision.evaluated_at is not None


def test_every_reason_code_carries_a_citation():
    """A reason code without a legal basis is an opinion, not a control.

    Reads the CITATIONS registry, not `__doc__`. Enum members silently discard a
    docstring written after the assignment, so __doc__ is empty for every member
    — which is how `citations_for()` came to emit "no citation recorded" on
    every decision until this test caught it.
    """
    from src.compliance.reason_codes import CITATIONS, citation_for

    # Length is not the test — "47 C.F.R. 64.1200(d)" is a complete citation.
    # Naming an authority is. A few codes are legitimately non-statutory (data
    # quality), and those must say so rather than stay silent.
    authorities = ("C.F.R.", "U.S.C.", "Stat.", "Rule", "Code", " v. ", "R. Reg.",
                   "Not a statute", "Affirmative", "Data quality")
    for code in ReasonCode:
        assert str(code) in CITATIONS, f"{code} has no citation"
        text = citation_for(code)
        assert any(a in text for a in authorities), f"{code} names no authority: {text!r}"


def test_enum_docstrings_are_not_a_citation_source():
    """Pins the trap so nobody reintroduces it: this is *why* CITATIONS exists."""
    assert not (ReasonCode.DNC_LISTED.__doc__ or "").strip()


def test_decisions_carry_real_citations(engine):
    decision = engine.evaluate(_record(on_national_dnc=True), today=TODAY)
    assert decision.legal_basis
    assert not any("no citation recorded" in c for c in decision.legal_basis)
    assert any("C.F.R." in c or "U.S.C." in c for c in decision.legal_basis)


def test_all_applicable_reasons_accumulate_not_just_the_first(engine):
    """The memo's exclusion table is a count by reason, so a record blocked for
    several independent reasons must report all of them."""
    decision = engine.evaluate(
        _record(on_national_dnc=True, consent_revoked=True,
                rnd_response="YES", dnc_scrub_age_days=99),
        today=TODAY,
    )
    codes = set(decision.reason_codes)
    assert {ReasonCode.DNC_LISTED, ReasonCode.CONSENT_REVOKED,
            ReasonCode.RND_REASSIGNED, ReasonCode.DNC_SCRUB_STALE} <= codes


# --- rules are data ---------------------------------------------------------


def test_no_legal_constant_is_hardcoded_in_the_engine():
    """Every window, threshold and citation must come from config.

    Checks literals in executable positions only — comments and docstrings may
    of course name a statute, and an earlier version of this test failed on the
    word "45" inside an explanatory comment.
    """
    import ast
    import inspect

    import src.compliance.engine as module

    tree = ast.parse(inspect.getsource(module))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }

    forbidden = {60, 31, 45, 30, 2721}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or node in docstrings:
            continue
        if isinstance(node.value, bool):
            continue
        if isinstance(node.value, int) and node.value in forbidden:
            raise AssertionError(f"legal constant {node.value} hardcoded at line {node.lineno}")
        if isinstance(node.value, str) and any(
            t in node.value for t in ("08:00", "21:00", "20:00", "C.F.R.", "U.S.C.")
        ):
            raise AssertionError(f"legal text {node.value!r} hardcoded at line {node.lineno}")


def test_a_new_jurisdiction_is_a_data_change_not_a_code_change(tmp_path, rules):
    """The live-defence scenario: Ohio enacts a 45-day accident solicitation
    window. It must be one CSV row and a version bump."""
    original = load_blackout_windows("config/blackout_windows.csv")
    csv_path = tmp_path / "blackout.csv"

    with open("config/blackout_windows.csv", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields, rows = reader.fieldnames, list(reader)
    rows.append({
        "jurisdiction": "OH", "record_type": "crash_report", "days_from": "45",
        "anchor_field": "incident_date", "reason_code": "TX_SOLICITATION_31D",
        "legal_basis": "Hypothetical Ohio statute", "notes": "live-defence scenario",
    })
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    ruleset = dict(rules)
    ruleset["blackout_windows"] = {**rules["blackout_windows"], "source": str(csv_path)}
    ruleset["version"] = "1.1.0"
    ruleset_path = tmp_path / "rules.yaml"
    ruleset_path.write_text(yaml.safe_dump(ruleset))

    engine = EligibilityEngine(str(ruleset_path))
    assert engine.ruleset_version == "1.1.0"
    assert len(engine.blackout) == len(original) + 1

    # A fresh Ohio crash is now blocked, with no Python touched.
    decision = engine.evaluate(
        _record(jurisdiction="OH", incident_date=date(2026, 8, 20),
                report_filing_date=date(2026, 8, 21)),
        today=TODAY,
    )
    assert decision.status == "BLOCKED_UNTIL"
    assert decision.blocked_until_date == date(2026, 10, 4)


# --- the fixture traps ------------------------------------------------------


def test_rnd_no_data_is_not_a_safe_harbour(engine):
    """The single most common engineering misreading of the RND: the safe
    harbour at 64.1200(m) attaches only to a 'No'."""
    decision = engine.evaluate(_record(rnd_response="NO_DATA"), today=TODAY)
    assert ReasonCode.RND_NO_DATA_NO_SAFE_HARBOR in decision.reason_codes
    assert decision.status == "INELIGIBLE"


def test_rnd_no_clears_the_gate(engine):
    decision = engine.evaluate(_record(rnd_response="NO"), today=TODAY)
    assert ReasonCode.RND_NO_DATA_NO_SAFE_HARBOR not in decision.reason_codes


def test_rnd_yes_blocks(engine):
    assert ReasonCode.RND_REASSIGNED in engine.evaluate(
        _record(rnd_response="YES"), today=TODAY).reason_codes


def test_dnc_scrub_older_than_31_days_is_stale(engine):
    """16 C.F.R. 310.4(b)(3)(iv) — a freshness SLA, not a one-time load."""
    assert ReasonCode.DNC_SCRUB_STALE not in engine.evaluate(
        _record(dnc_scrub_age_days=31), today=TODAY).reason_codes
    assert ReasonCode.DNC_SCRUB_STALE in engine.evaluate(
        _record(dnc_scrub_age_days=32), today=TODAY).reason_codes


def test_missing_scrub_is_treated_as_stale(engine):
    """Absent evidence of a scrub is not evidence of a fresh one."""
    assert ReasonCode.DNC_SCRUB_STALE in engine.evaluate(
        _record(dnc_scrub_age_days=None), today=TODAY).reason_codes


@pytest.mark.parametrize("line_type", ["voip", "unknown", None])
def test_voip_and_unknown_route_to_the_restrictive_treatment(engine, line_type):
    record = _record(line_type_reported=line_type)
    engine.evaluate(record, today=TODAY)
    assert record["_line_type_treatment"] == "restrictive"


def test_landline_routes_permissive(engine):
    record = _record(line_type_reported="landline")
    engine.evaluate(record, today=TODAY)
    assert record["_line_type_treatment"] == "permissive"


def test_revoked_consent_blocks_even_with_consent_on_file(engine):
    decision = engine.evaluate(_record(consent_on_file=True, consent_revoked=True), today=TODAY)
    assert ReasonCode.CONSENT_REVOKED in decision.reason_codes


def test_absent_consent_also_fails_the_dppa_gate(engine):
    """Solicitation has no permissible use under 2721(b) absent state-obtained
    consent, so a missing consent fails source eligibility as well as channel."""
    codes = engine.evaluate(_record(consent_on_file=False), today=TODAY).reason_codes
    assert ReasonCode.CONSENT_ABSENT in codes
    assert ReasonCode.DPPA_NO_PERMISSIBLE_USE in codes


# --- timing -----------------------------------------------------------------


def test_florida_data_gate_binds_over_the_bar_rule():
    """Fla. Stat. 316.066(2) is 60 days from filing; the bar rule is 30 from
    incident. The longer one binds, so implementing only the bar rule
    implements the wrong constraint."""
    engine = EligibilityEngine(RULESET)
    engine.channel = "written"  # both gates apply on this channel
    decision = engine.evaluate(
        _record(jurisdiction="FL", latitude=28.5421, longitude=-81.379,
                phone_npa="407",
                incident_date=date(2026, 8, 11), report_filing_date=date(2026, 8, 12)),
        today=TODAY,
    )
    assert decision.status == "BLOCKED_UNTIL"
    # 60 days from filing (2026-10-11) beats 30 from incident (2026-09-10).
    assert decision.blocked_until_date == date(2026, 10, 11)


def test_maryland_has_no_waiting_period_but_is_still_method_barred(engine):
    """A zero-day blackout row is correct and is NOT 'no restriction' — the
    constraint is on the channel, not the clock."""
    md = [r for r in engine.blackout if r.jurisdiction == "MD"]
    assert md and md[0].days_from == 0

    decision = engine.evaluate(_record(jurisdiction="MD"), today=TODAY)
    assert decision.status == "INELIGIBLE"
    assert ReasonCode.LIVE_SOLICITATION_PROHIBITED in decision.reason_codes


# --- calling windows --------------------------------------------------------


def test_florida_calling_window_is_stricter_than_federal(rules):
    earliest, latest, citations = windows.window_for(rules, "FL")
    assert (earliest, latest) == (time(8, 0), time(20, 0))
    assert len(citations) == 2, "both the federal and state citations must be recorded"


def test_federal_window_applies_where_no_override(rules):
    assert windows.window_for(rules, "MD")[:2] == (time(8, 0), time(21, 0))


def test_window_is_derived_from_coordinates_not_the_area_code(rules):
    """A pipeline computing this from SUBSTR(phone,1,3) fails Part 5c outright."""
    resolver = TimezoneResolver()
    zone, window = windows.resolve(
        rules, jurisdiction="TX", latitude=31.8479, longitude=-106.5348,
        npa="915", resolver=resolver)
    assert zone == "America/Denver"
    assert window.basis == "address_coordinates"


def test_npa_is_only_a_fallback(rules):
    resolver = TimezoneResolver()
    _, window = windows.resolve(
        rules, jurisdiction="MD", latitude=None, longitude=None,
        npa="301", resolver=resolver)
    assert window.basis == "npa_nxx_fallback"


def test_disagreement_between_sources_is_recorded(rules):
    """Number portability makes NPA unreliable evidence of location, so a
    disagreement narrows the window rather than picking a winner."""
    resolver = TimezoneResolver()
    _, window = windows.resolve(
        rules, jurisdiction="TX", latitude=31.8479, longitude=-106.5348,  # Mountain
        npa="214", resolver=resolver)                     # Dallas NPA
    assert window.sources_disagreed


def test_no_zone_means_no_window(rules):
    resolver = TimezoneResolver()
    zone, window = windows.resolve(
        rules, jurisdiction="MD", latitude=None, longitude=None,
        npa=None, resolver=resolver)
    assert zone is None and window is None


def test_unresolvable_location_blocks(engine):
    decision = engine.evaluate(
        _record(latitude=None, longitude=None, phone_npa=None), today=TODAY)
    assert ReasonCode.TIMEZONE_UNRESOLVED in decision.reason_codes
    assert decision.status == "INELIGIBLE"


# --- reproducibility --------------------------------------------------------


def test_same_input_and_ruleset_give_the_same_decision(engine):
    """Only the lineage id and timestamp may differ between runs."""
    a = engine.evaluate(_record(on_national_dnc=True), today=TODAY)
    b = engine.evaluate(_record(on_national_dnc=True), today=TODAY)
    assert a.status == b.status
    assert a.reason_codes == b.reason_codes
    assert a.legal_basis == b.legal_basis
    assert a.decision_lineage_id != b.decision_lineage_id, "lineage ids must be unique"


# --- internal DNC and the EBR exemption -------------------------------------


def test_internal_dnc_request_blocks(engine):
    """47 C.F.R. 64.1200(d). The company-specific list is independent of the
    national registry: a record clean against the registry is still blocked by
    a request made directly to the seller."""
    codes = engine.evaluate(
        _record(on_national_dnc=False, on_internal_dnc=True), today=TODAY
    ).reason_codes
    assert ReasonCode.INTERNAL_DNC in codes


def test_ebr_exempts_the_national_registry_within_the_window(engine):
    """16 C.F.R. 310.4(b)(1)(iii)(B)(ii): 18 months from a transaction. A listed
    number inside that window is exempt, and the exemption is recorded
    affirmatively rather than by the absence of a code."""
    codes = engine.evaluate(
        _record(on_national_dnc=True, ebr_basis="transaction",
                ebr_basis_date=date(2025, 6, 1)),   # 15 months before TODAY
        today=TODAY,
    ).reason_codes
    assert ReasonCode.ELIGIBLE_EBR in codes
    assert ReasonCode.DNC_LISTED not in codes


def test_ebr_expires_and_the_listing_bites_again(engine):
    """19 months after the transaction the exemption is gone. Months are
    calendar months — 18 * 30 days would still be inside the window here."""
    codes = engine.evaluate(
        _record(on_national_dnc=True, ebr_basis="transaction",
                ebr_basis_date=date(2025, 2, 1)),   # 19 months before TODAY
        today=TODAY,
    ).reason_codes
    assert ReasonCode.DNC_LISTED in codes
    assert ReasonCode.ELIGIBLE_EBR not in codes


def test_inquiry_ebr_is_three_months_not_eighteen(engine):
    """The two bases carry different windows; using the transaction window for
    an inquiry would exempt a number for fifteen months too long."""
    stale = engine.evaluate(
        _record(on_national_dnc=True, ebr_basis="inquiry",
                ebr_basis_date=date(2026, 4, 1)),   # 5 months — outside 3
        today=TODAY,
    ).reason_codes
    fresh = engine.evaluate(
        _record(on_national_dnc=True, ebr_basis="inquiry",
                ebr_basis_date=date(2026, 7, 1)),   # 2 months — inside 3
        today=TODAY,
    ).reason_codes
    assert ReasonCode.DNC_LISTED in stale
    assert ReasonCode.ELIGIBLE_EBR in fresh


def test_undated_ebr_claim_is_not_an_ebr(engine):
    """The exemption is defined by the age of the relationship, so a claim with
    no date cannot be shown to be inside the window. Fails closed."""
    codes = engine.evaluate(
        _record(on_national_dnc=True, ebr_basis="transaction", ebr_basis_date=None),
        today=TODAY,
    ).reason_codes
    assert ReasonCode.DNC_LISTED in codes
    assert ReasonCode.ELIGIBLE_EBR not in codes


def test_internal_dnc_outranks_the_ebr(engine):
    """47 C.F.R. 64.1200(d)(3). An EBR exempts the national registry only — it
    never overrides a do-not-call request made to the seller itself. This is the
    precedence that makes the two rules distinct rather than redundant."""
    decision = engine.evaluate(
        _record(on_national_dnc=True, on_internal_dnc=True,
                ebr_basis="transaction", ebr_basis_date=date(2025, 6, 1)),
        today=TODAY,
    )
    assert ReasonCode.INTERNAL_DNC in decision.reason_codes
    assert decision.status == "INELIGIBLE"


def test_ebr_does_not_cure_the_categorical_bar(engine):
    """An EBR is a DNC exemption, not a licence to solicit. On live telephone
    the categorical bar still stands, so the record stays INELIGIBLE."""
    decision = engine.evaluate(
        _record(on_national_dnc=True, ebr_basis="transaction",
                ebr_basis_date=date(2025, 6, 1)),
        today=TODAY,
    )
    assert decision.status == "INELIGIBLE"
    assert ReasonCode.LIVE_SOLICITATION_PROHIBITED in decision.reason_codes


def test_every_new_code_carries_a_citation(engine):
    """A reason code without a legal basis is an opinion, not a control."""
    for code in (ReasonCode.INTERNAL_DNC, ReasonCode.ELIGIBLE_EBR):
        assert engine.citations_for([code])[0].strip()
