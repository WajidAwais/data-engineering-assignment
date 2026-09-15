"""Unit tests for the value normalisers and the severity crosswalk.

Pure functions, no database. These are the defect fixes from DATA_QUALITY A2,
A6, A7 stated as behaviour.
"""

from __future__ import annotations

import pytest

from src.transform import normalize, severity


# --- the dual dictionary (A2, A6, A7) ---------------------------------------


@pytest.mark.parametrize("upper,title", [
    ("NO APPARENT INJURY", "No Apparent Injury"),
    ("POSSIBLE INJURY", "Possible Injury"),
    ("SUSPECTED MINOR INJURY", "Suspected Minor Injury"),
    ("SUSPECTED SERIOUS INJURY", "Suspected Serious Injury"),
    ("FATAL INJURY", "Fatal Injury"),
])
def test_both_case_schemes_map_to_the_same_ordinal(upper, title):
    assert normalize.injury_severity(upper) == normalize.injury_severity(title)
    assert normalize.injury_severity(upper) is not None


def test_injury_severity_is_ordered():
    order = [
        normalize.injury_severity(v) for v in (
            "NO APPARENT INJURY", "Possible Injury", "SUSPECTED MINOR INJURY",
            "Suspected Serious Injury", "FATAL INJURY",
        )
    ]
    assert order == sorted(order) == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("token", [
    None, "", "  ", "N/A", "UNKNOWN", "Unknown, Unknown", "None", "OTHER",
])
def test_every_spelling_of_absent_collapses_to_null(token):
    assert normalize.is_null_token(token)
    assert normalize.injury_severity(token) is None


# --- substance abuse: two encodings, one comma ------------------------------


def test_new_scheme_splits_the_pair():
    assert normalize.substance_abuse(
        "Not Suspect of Alcohol Use, Not Suspect of Drug Use") == (False, False)
    assert normalize.substance_abuse(
        "Suspect of Alcohol Use, Not Suspect of Drug Use") == (True, False)
    assert normalize.substance_abuse(
        "Not Suspect of Alcohol Use, Suspect of Drug Use") == (False, True)
    assert normalize.substance_abuse(
        "Suspect of Alcohol Use, Suspect of Drug Use") == (True, True)


def test_new_scheme_unknown_half_stays_unknown():
    assert normalize.substance_abuse("Suspect of Alcohol Use, Unknown") == (True, None)
    assert normalize.substance_abuse("Unknown, Suspect of Drug Use") == (None, True)


def test_old_scheme_does_not_manufacture_a_negative():
    """'ALCOHOL PRESENT' says nothing about drugs. Inferring False there would
    invent information the old dictionary never carried."""
    assert normalize.substance_abuse("ALCOHOL PRESENT") == (True, None)
    assert normalize.substance_abuse("ILLEGAL DRUG PRESENT") == (None, True)
    assert normalize.substance_abuse("NONE DETECTED") == (False, False)
    assert normalize.substance_abuse("COMBINED SUBSTANCE PRESENT") == (True, True)


def test_the_embedded_comma_does_not_break_the_split():
    """A naive split(',') over the whole column is ragged: old-scheme values
    yield one element, new-scheme two."""
    old = normalize.substance_abuse("NONE DETECTED")
    new = normalize.substance_abuse("Not Suspect of Alcohol Use, Not Suspect of Drug Use")
    assert len(old) == len(new) == 2


def test_dispatch_is_on_shape_not_date():
    """The two encodings are both live on 2023-12-28 and 2024-01-03, so nothing
    here may depend on when the crash happened."""
    import inspect
    src = inspect.getsource(normalize.substance_abuse)
    assert "date" not in src.lower().replace("dictionary", "")


# --- severity crosswalk -----------------------------------------------------


def test_txdot_codes_are_not_the_identity_mapping():
    """Derived from the injury-count columns: code 5 is *no injury*, not the
    most severe, and 1/3 are inverted relative to the ordinal."""
    assert severity.from_crash_sev_id("5") == normalize.NO_INJURY
    assert severity.from_crash_sev_id("4") == normalize.FATAL
    assert severity.from_crash_sev_id("1") == normalize.SUSPECTED_SERIOUS
    assert severity.from_crash_sev_id("3") == normalize.POSSIBLE
    assert severity.from_crash_sev_id("0") is None


def test_txdot_code_tolerates_double_formatting():
    """crash_sev_id is esriFieldTypeDouble, so it can arrive as '4' or '4.0'."""
    assert severity.from_crash_sev_id("4.0") == severity.from_crash_sev_id("4")


def test_report_type_fallback_is_lossy_by_construction():
    """Three levels cannot represent five. 'Injury Crash' covers C, B and A."""
    assert severity.from_report_type("Property Damage Crash") == normalize.NO_INJURY
    assert severity.from_report_type("Fatal Crash") == normalize.FATAL
    assert severity.from_report_type("Injury Crash") is not None
    assert len(set(severity.REPORT_TYPE.values())) < len(
        {0, 1, 2, 3, 4}), "fallback must not claim full KABCO resolution"


def test_crash_severity_is_the_worst_party_injury():
    assert severity.from_party_values(
        ["NO APPARENT INJURY", "FATAL INJURY", "Possible Injury"]) == normalize.FATAL
    assert severity.from_party_values(["NO APPARENT INJURY"]) == normalize.NO_INJURY


def test_crash_severity_is_null_when_no_party_is_classifiable():
    assert severity.from_party_values([]) is None
    assert severity.from_party_values(["None", "UNKNOWN", None]) is None
