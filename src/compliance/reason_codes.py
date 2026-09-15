"""Machine-readable eligibility reason codes.

Extend this. The list below is a starting point, not a complete taxonomy.
Every code you add must carry a citation — a reason code without a legal
basis is an opinion, not a control.
"""

from enum import StrEnum


class ReasonCode(StrEnum):
    # --- Source eligibility -------------------------------------------------
    DPPA_NO_PERMISSIBLE_USE = "DPPA_NO_PERMISSIBLE_USE"
    """18 U.S.C. 2721(b). Solicitation appears only at (b)(12) and requires
    state-obtained express consent. Maracich v. Spears, 570 U.S. 48 (2013),
    forecloses the (b)(4) litigation route."""

    TX_REDACTED_NO_CONTACT_PII = "TX_REDACTED_NO_CONTACT_PII"
    """Tex. Transp. Code 550.065(f). The bulk-accessible CR-3 strips name,
    address other than ZIP, and telephone number."""

    MD_MVA_TELEPHONE_SOLICITATION_BAR = "MD_MVA_TELEPHONE_SOLICITATION_BAR"
    """Md. Code Gen. Prov. 4-320."""

    PROVENANCE_UNKNOWN = "PROVENANCE_UNKNOWN"
    """A record whose acquisition basis cannot be stated cannot be contacted."""

    # --- Time-based gates ---------------------------------------------------
    FL_CRASH_REPORT_60D = "FL_CRASH_REPORT_60D"       # Fla. Stat. 316.066(2)
    FL_SOLICITATION_30D = "FL_SOLICITATION_30D"       # Fla. Bar 4-7.18(b)(1)(A)
    TX_SOLICITATION_31D = "TX_SOLICITATION_31D"       # Tex. Penal Code 38.12(d)(2)(C)
    AVIATION_45D = "AVIATION_45D"                     # 49 U.S.C. 1136(g)(2)

    # --- Channel gates ------------------------------------------------------
    LIVE_SOLICITATION_PROHIBITED = "LIVE_SOLICITATION_PROHIBITED"
    """ABA Model Rule 7.3(b) and analogues: Md. Rule 19-307.3,
    Fla. Bar 4-7.18(a), Tex. Disciplinary R. 7.03. 'Live person-to-person
    contact' includes live telephone. See also Tex. Penal Code 38.12(a)(2)."""

    DNC_LISTED = "DNC_LISTED"
    """47 C.F.R. 64.1200(c)(2); 16 C.F.R. 310.4(b)(1)(iii)(B). Number is on
    the national Do-Not-Call registry."""
    DNC_SCRUB_STALE = "DNC_SCRUB_STALE"               # 16 C.F.R. 310.4(b)(3)(iv)
    INTERNAL_DNC = "INTERNAL_DNC"                     # 47 C.F.R. 64.1200(d)
    RND_REASSIGNED = "RND_REASSIGNED"
    """47 C.F.R. 64.1200(m). The Reassigned Numbers Database returned "Yes":
    the number has been reassigned since the consent date."""
    RND_NO_DATA_NO_SAFE_HARBOR = "RND_NO_DATA_NO_SAFE_HARBOR"   # 47 C.F.R. 64.1200(m)
    LINE_TYPE_UNRESOLVED = "LINE_TYPE_UNRESOLVED"
    """47 U.S.C. 227(b)(1)(A)(iii) attaches strict liability to autodialled or
    prerecorded calls to wireless numbers; 227(c) governs landlines under a
    laxer regime. voip and unknown therefore route to the wireless (most
    restrictive) treatment, never the landline one."""
    OUTSIDE_CALLING_WINDOW = "OUTSIDE_CALLING_WINDOW"  # 16 C.F.R. 310.4(c)
    CONSENT_ABSENT = "CONSENT_ABSENT"
    """47 U.S.C. 227; 47 C.F.R. 64.1200(a). No prior express consent on file.
    Note Bradford v. Sovereign Pest Control (5th Cir. 2026) held 227(b) needs
    only prior express consent, which may be oral — but that binds only in the
    Fifth Circuit and the written rule was not vacated elsewhere."""
    CONSENT_UNVERIFIABLE = "CONSENT_UNVERIFIABLE"
    """The burden of proving consent is on the caller. Consent whose text,
    disclosure hash, URL, timestamp, IP, user agent and seller chain cannot be
    produced is functionally no consent. Retain through the 4-year TCPA
    limitations period; 5 years aligns with the TSR and DPPA record rules."""
    CONSENT_REVOKED = "CONSENT_REVOKED"                # 47 C.F.R. 64.1200(a)(10)

    # --- Data quality -------------------------------------------------------
    GEOCODE_TIER_INSUFFICIENT = "GEOCODE_TIER_INSUFFICIENT"
    """Not a statute — a precondition for one. 16 C.F.R. 310.4(c) requires the
    calling window in local time at the called party's location, so a location
    we cannot resolve to that precision cannot be given a lawful window."""
    COORDINATE_OUT_OF_ENVELOPE = "COORDINATE_OUT_OF_ENVELOPE"
    """Data quality. The coordinate falls outside the jurisdiction it claims,
    so jurisdiction-keyed gates (blackout window, state calling hours) cannot
    be applied with confidence. See DATA_QUALITY A1."""
    SNAP_DISTANCE_EXCEEDED = "SNAP_DISTANCE_EXCEEDED"
    """Data quality. The nearest road segment is far enough away that the
    inherited road attributes are fiction rather than enrichment."""
    TIMEZONE_UNRESOLVED = "TIMEZONE_UNRESOLVED"
    """16 C.F.R. 310.4(c); 47 C.F.R. 64.1200(c)(1). No IANA zone could be
    derived from the coordinates, so no lawful calling window exists."""

    TX_BARRATRY_FELONY = "TX_BARRATRY_FELONY"
    """Tex. Penal Code 38.12(a)(2). In-person or telephone solicitation for
    economic benefit is barratry, a third-degree felony. It reaches the caller
    personally, not only the firm employing them."""

    FL_CRASH_DATA_MISUSE_FELONY = "FL_CRASH_DATA_MISUSE_FELONY"
    """Fla. Stat. 316.066(3)(d). Knowing misuse of crash data obtained under
    the confidentiality exemption is a third-degree felony."""

    MD_NO_WAITING_PERIOD_METHOD_BARRED = "MD_NO_WAITING_PERIOD_METHOD_BARRED"
    """Md. Rule 19-307.3 with Md. Code Gen. Prov. 4-320. Maryland imposes no
    accident-specific waiting period; the constraint is on the contact METHOD.
    A zero-day blackout row is therefore correct and is not the same thing as
    'no restriction'."""

    NO_IDENTITY_LAYER = "NO_IDENTITY_LAYER"
    """Not a statute — a structural fact. None of the three public sources
    carries callable identity: Tex. Transp. Code 550.065(f) strips name,
    address other than ZIP and telephone from the bulk CR-3; Montgomery
    publishes an opaque person GUID; Maryland's person table is titled
    '(Anonymized)'; FARS carries no names. A crash record with no identity
    cannot be contacted at all."""

    OUTSIDE_CALLING_WINDOW_STATE_STRICTER = "OUTSIDE_CALLING_WINDOW_STATE_STRICTER"
    """Fla. Stat. 501.616(6) (and Okla., Wash.) close at 20:00 rather than the
    federal 21:00 of 16 C.F.R. 310.4(c). The stricter window binds."""

    CALLING_WINDOW_SOURCES_DISAGREE = "CALLING_WINDOW_SOURCES_DISAGREE"
    """Address-derived and NPA-derived zones disagree. Number portability and
    VoIP make NPA-NXX unreliable evidence of physical location, so the
    intersection of the two windows is taken rather than either alone."""

    # --- Affirmative -------------------------------------------------------
    ELIGIBLE_CONSENTED = "ELIGIBLE_CONSENTED"
    """Affirmative. Prior express consent is on file with verifiable
    provenance, satisfying 47 U.S.C. 227 and — where the record derives from a
    motor vehicle record — 18 U.S.C. 2721(b)(12)."""
    ELIGIBLE_EBR = "ELIGIBLE_EBR"
    """Affirmative. Established business relationship exemption, 16 C.F.R.
    310.4(b)(1)(iii)(B)(ii) and 47 C.F.R. 64.1200(f)(5): 18 months from a
    transaction, 3 months from a consumer inquiry."""

# Citations, as data.
#
# The docstrings above are for human readers: Enum members do NOT retain a
# docstring written after the assignment, so `ReasonCode.X.__doc__` is empty
# and anything reading it emits "no citation recorded" for every code. The
# output contract requires a populated `legal_basis`, so the citations have to
# live somewhere Python can actually read them.
CITATIONS: dict[str, str] = {
        "FL_CRASH_REPORT_60D":
        "Fla. Stat. 316.066(2). Crash reports revealing personal information are  "
        "exempt from public disclosure for 60 days from the filing date. This data  "
        "gate is LONGER than the 30-day bar rule, so it is the binding constraint.",
        "FL_SOLICITATION_30D":
        "R. Reg. Fla. Bar 4-7.18(b)(1)(A). Targeted written communication concerning  "
        "an accident may not be sent within 30 days of the incident.",
        "TX_SOLICITATION_31D":
        "Tex. Penal Code 38.12(d)(2)(C). Written solicitation concerning an accident  "
        "may not be sent before the 31st day after the incident.",
        "AVIATION_45D":
        "49 U.S.C. 1136(g)(2). No unsolicited communication to an aviation accident  "
        "victim or family within 45 days of the accident.",
    "DPPA_NO_PERMISSIBLE_USE":
        "18 U.S.C. 2721(b). Solicitation appears only at (b)(12) and requires "
        "state-obtained express consent. Maracich v. Spears, 570 U.S. 48 (2013), "
        "forecloses the (b)(4) litigation route.",
    "TX_REDACTED_NO_CONTACT_PII":
        "Tex. Transp. Code 550.065(f). The bulk-accessible CR-3 strips name, address "
        "other than ZIP, and telephone number.",
    "MD_MVA_TELEPHONE_SOLICITATION_BAR":
        "Md. Code Gen. Prov. 4-320.",
    "PROVENANCE_UNKNOWN":
        "18 U.S.C. 2721(c). A record whose acquisition basis cannot be stated "
        "cannot be contacted: the DPPA independently requires 5-year redisclosure "
        "records naming each recipient and the permitted purpose relied on.",
    "LIVE_SOLICITATION_PROHIBITED":
        "ABA Model Rule 7.3(b) and analogues: Md. Rule 19-307.3, Fla. Bar 4-7.18(a), "
        "Tex. Disciplinary R. 7.03. 'Live person-to-person contact' includes live "
        "telephone. See also Tex. Penal Code 38.12(a)(2).",
    "DNC_LISTED":
        "47 C.F.R. 64.1200(c)(2); 16 C.F.R. 310.4(b)(1)(iii)(B). Number is on the "
        "national Do-Not-Call registry.",
    "DNC_SCRUB_STALE":
        "16 C.F.R. 310.4(b)(3)(iv)",
    "INTERNAL_DNC":
        "47 C.F.R. 64.1200(d)",
    "RND_REASSIGNED":
        "47 C.F.R. 64.1200(m). The Reassigned Numbers Database returned 'Yes': the "
        "number has been reassigned since the consent date.",
    "RND_NO_DATA_NO_SAFE_HARBOR":
        "47 C.F.R. 64.1200(m)",
    "LINE_TYPE_UNRESOLVED":
        "47 U.S.C. 227(b)(1)(A)(iii) attaches strict liability to autodialled or "
        "prerecorded calls to wireless numbers; 227(c) governs landlines under a laxer "
        "regime. voip and unknown therefore route to the wireless (most restrictive) "
        "treatment, never the landline one.",
    "OUTSIDE_CALLING_WINDOW":
        "16 C.F.R. 310.4(c)",
    "CONSENT_ABSENT":
        "47 U.S.C. 227; 47 C.F.R. 64.1200(a). No prior express consent on file. Note "
        "Bradford v. Sovereign Pest Control (5th Cir. 2026) held 227(b) needs only "
        "prior express consent, which may be oral — but that binds only in the Fifth "
        "Circuit and the written rule was not vacated elsewhere.",
    "CONSENT_UNVERIFIABLE":
        "47 U.S.C. 227(c); 16 C.F.R. 310.5(a)(5). The burden of proving consent is on "
        "the caller. Consent whose text, disclosure hash, URL, timestamp, IP, user agent "
        "and seller chain cannot be produced is functionally no consent. Retain through "
        "the 4-year TCPA limitations period; 5 years aligns with the TSR and DPPA rules.",
    "CONSENT_REVOKED":
        "47 C.F.R. 64.1200(a)(10)",
    "GEOCODE_TIER_INSUFFICIENT":
        "Not a statute — a precondition for one. 16 C.F.R. 310.4(c) requires the "
        "calling window in local time at the called party's location, so a location we "
        "cannot resolve to that precision cannot be given a lawful window.",
    "COORDINATE_OUT_OF_ENVELOPE":
        "Data quality. The coordinate falls outside the jurisdiction it claims, so "
        "jurisdiction-keyed gates (blackout window, state calling hours) cannot be "
        "applied with confidence. See DATA_QUALITY A1.",
    "SNAP_DISTANCE_EXCEEDED":
        "Data quality. The nearest road segment is far enough away that the inherited "
        "road attributes are fiction rather than enrichment.",
    "TIMEZONE_UNRESOLVED":
        "16 C.F.R. 310.4(c); 47 C.F.R. 64.1200(c)(1). No IANA zone could be derived "
        "from the coordinates, so no lawful calling window exists.",
    "TX_BARRATRY_FELONY":
        "Tex. Penal Code 38.12(a)(2). In-person or telephone solicitation for economic "
        "benefit is barratry, a third-degree felony. It reaches the caller personally, "
        "not only the firm employing them.",
    "FL_CRASH_DATA_MISUSE_FELONY":
        "Fla. Stat. 316.066(3)(d). Knowing misuse of crash data obtained under the "
        "confidentiality exemption is a third-degree felony.",
    "MD_NO_WAITING_PERIOD_METHOD_BARRED":
        "Md. Rule 19-307.3 with Md. Code Gen. Prov. 4-320. Maryland imposes no "
        "accident-specific waiting period; the constraint is on the contact METHOD. A "
        "zero-day blackout row is therefore correct and is not the same thing as 'no "
        "restriction'.",
    "NO_IDENTITY_LAYER":
        "Not a statute — a structural fact. None of the three public sources carries "
        "callable identity: Tex. Transp. Code 550.065(f) strips name, address other "
        "than ZIP and telephone from the bulk CR-3; Montgomery publishes an opaque "
        "person GUID; Maryland's person table is titled '(Anonymized)'; FARS carries no "
        "names. A crash record with no identity cannot be contacted at all.",
    "OUTSIDE_CALLING_WINDOW_STATE_STRICTER":
        "Fla. Stat. 501.616(6) (and Okla., Wash.) close at 20:00 rather than the "
        "federal 21:00 of 16 C.F.R. 310.4(c). The stricter window binds.",
    "CALLING_WINDOW_SOURCES_DISAGREE":
        "16 C.F.R. 310.4(c); 47 C.F.R. 64.1200(c)(1) require local time at the called "
        "party's location. Address-derived and NPA-derived zones disagree; number "
        "portability and VoIP make NPA-NXX unreliable evidence of physical location, so "
        "the intersection of the two windows is taken rather than either alone.",
    "ELIGIBLE_CONSENTED":
        "Affirmative. 47 U.S.C. 227; where the record derives from a motor vehicle "
        "record, 18 U.S.C. 2721(b)(12). NOTE: this asserts only that a consent flag "
        "is set. Whether that consent is VERIFIABLE is a separate gate, governed by "
        "contact_gates.consent.require_provenance in the ruleset. With the fixture's "
        "bare boolean and no disclosure text, hash, URL, timestamp, IP, user agent or "
        "seller chain, enabling that gate takes this population to zero.",
    "ELIGIBLE_EBR":
        "Affirmative. Established business relationship exemption, 16 C.F.R. "
        "310.4(b)(1)(iii)(B)(ii) and 47 C.F.R. 64.1200(f)(5): 18 months from a "
        "transaction, 3 months from a consumer inquiry.",
}


def citation_for(code) -> str:
    """Legal basis for a reason code. Raises rather than returning a
    placeholder: a code without a citation is an opinion, not a control."""
    key = str(code)
    if key not in CITATIONS:
        raise KeyError(f"{key} has no citation in CITATIONS")
    return CITATIONS[key]
