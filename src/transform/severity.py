"""Severity harmonisation across three incompatible scales.

Target is the KABCO ordinal in normalize.py (0 no-injury .. 4 fatal), which is
what contracts/lead_output.schema.json expects. NULL means unknown — no magic
sentinel.

Where each source sits, and where the mapping loses information:

  Montgomery  party-level `injury_severity` is already KABCO, so crash severity
              is max(party severity). For the 785 crashes with no party row it
              falls back to `acrs_report_type`, which has only three levels —
              'Injury Crash' cannot distinguish C/B/A. That is the lossy path
              and it is recorded per-row in `severity_basis`.

  TxDOT       `crash_sev_id` is a coded integer and the code list is NOT the
              identity mapping. Derived from the injury-count columns rather
              than assumed (see CRASH_SEV_ID below).

  FARS        a fatality census — every row has FATALS >= 1, so every row is 4.
              No mapping needed and none is lossy.
"""

from __future__ import annotations

from .normalize import (
    FATAL,
    NO_INJURY,
    POSSIBLE,
    SUSPECTED_MINOR,
    SUSPECTED_SERIOUS,
    injury_severity,
)

# Basis codes, carried on every silver row so the lossy edges stay visible.
BASIS_PARTY_MAX = "moco_party_max"        # exact
BASIS_REPORT_TYPE = "moco_report_type"    # lossy: 3 levels -> 5
BASIS_TXDOT_CODE = "txdot_crash_sev_id"   # exact
BASIS_FARS_CENSUS = "fars_fatality_census"  # exact by construction

# Montgomery crash-level fallback. 'Injury Crash' collapses C/B/A, so it maps to
# the middle of that range and the basis column flags it as an approximation.
REPORT_TYPE = {
    "property damage crash": NO_INJURY,
    "injury crash": SUSPECTED_MINOR,
    "fatal crash": FATAL,
}

# TxDOT. Derived from the data, not from a CRIS lookup we do not have: grouping
# crash_sev_id against the injury-count columns (2026-09-15, 14,535 rows) gives
# a clear dominant injury class per code.
#
#   id  n      dominant counts              -> meaning
#   0   33     unknown only                 -> unknown
#   1   1,809  serious 1,835                -> suspected serious (A)
#   2   6,379  minor 6,472                  -> suspected minor (B)
#   3   3,724  possible 3,782               -> possible (C)
#   4   451    death 457                    -> fatal (K)
#   5   2,139  no-injury only               -> no apparent injury (O)
#
# Note it is emphatically NOT the identity: a naive `severity = crash_sev_id`
# reads 5 as most-severe when it is in fact no-injury, and inverts 1 and 3.
CRASH_SEV_ID = {
    "0": None,
    "1": SUSPECTED_SERIOUS,
    "2": SUSPECTED_MINOR,
    "3": POSSIBLE,
    "4": FATAL,
    "5": NO_INJURY,
}


def from_report_type(value: str | None) -> int | None:
    return REPORT_TYPE.get(value.strip().lower()) if value else None


def from_crash_sev_id(value: str | None) -> int | None:
    if value is None:
        return None
    # Arrives as a Double-typed string: '4' or '4.0'.
    key = value.strip().split(".")[0]
    return CRASH_SEV_ID.get(key)


def from_party_values(values: list[str | None]) -> int | None:
    """Crash severity is the worst injury among its parties."""
    ordinals = [o for o in (injury_severity(v) for v in values) if o is not None]
    return max(ordinals) if ordinals else None
