"""Value normalisation. Pure functions, no I/O.

These fix the dual-dictionary defects found in bronze (DATA_QUALITY A2, A6, A7).
The Montgomery feed migrated several categorical columns from an UPPERCASE
single-value scheme to a Title Case one, and the two coexist in the data.

Every function here dispatches on the **shape of the value**, never on a date.
That is the whole point: the encodings overlap on 2023-12-28 and 2024-01-03, so
no cutover date can separate them.
"""

from __future__ import annotations

# Every spelling of "absent" seen across the three sources. Montgomery alone
# uses four: SQL NULL, 'N/A', 'UNKNOWN', and 'Unknown, Unknown' — plus 'None'
# as a literal string in injury_severity.
NULL_TOKENS = frozenset({
    "", "n/a", "na", "unknown", "unknown, unknown", "none", "not applicable", "other",
})

# KABCO, ascending. Matches the 0-5 range in contracts/lead_output.schema.json.
NO_INJURY = 0        # O
POSSIBLE = 1         # C
SUSPECTED_MINOR = 2  # B
SUSPECTED_SERIOUS = 3  # A
FATAL = 4            # K

_INJURY_SEVERITY = {
    "no apparent injury": NO_INJURY,
    "possible injury": POSSIBLE,
    "suspected minor injury": SUSPECTED_MINOR,
    "suspected serious injury": SUSPECTED_SERIOUS,
    "fatal injury": FATAL,
}


def _clean(value: str | None) -> str | None:
    """Trim and case-fold. Returns None for any spelling of absent."""
    if value is None:
        return None
    folded = value.strip().lower()
    return None if folded in NULL_TOKENS else folded


def is_null_token(value: str | None) -> bool:
    return _clean(value) is None


def injury_severity(value: str | None) -> int | None:
    """KABCO string -> ordinal. Case-folding collapses the dual dictionary:
    'FATAL INJURY' and 'Fatal Injury' are the same level."""
    cleaned = _clean(value)
    return _INJURY_SEVERITY.get(cleaned) if cleaned else None


def collision_type(value: str | None) -> str | None:
    """Same uppercase/Title Case split as injury_severity. Normalised to lower
    case rather than mapped to a fixed vocabulary — the value set is open and
    we have no authority to collapse categories we have not profiled."""
    return _clean(value)


def substance_abuse(value: str | None) -> tuple[bool | None, bool | None]:
    """driver_substance_abuse -> (alcohol_suspected, drugs_suspected).

    Two encodings, distinguished by the pair delimiter:

      new  'Suspect of Alcohol Use, Not Suspect of Drug Use'  -> (True, False)
      old  'ALCOHOL PRESENT'                                  -> (True, None)

    The old scheme carries strictly less information: 'ALCOHOL PRESENT' says
    nothing about drugs, so the drug side stays None rather than being inferred
    as False. Manufacturing a negative there would be the lossy direction.

    Note the embedded comma in the new scheme — a naive split on ',' over the
    whole column produces a ragged result, because old-scheme values yield one
    element and new-scheme values two.
    """
    cleaned = _clean(value)
    if cleaned is None:
        return (None, None)

    if "," in cleaned:
        alcohol_part, _, drug_part = cleaned.partition(",")
        return (_suspect(alcohol_part), _suspect(drug_part))

    return _OLD_SCHEME.get(cleaned, (None, None))


def _suspect(part: str) -> bool | None:
    """One half of a new-scheme pair."""
    part = part.strip()
    if part.startswith("not suspect"):
        return False
    if part.startswith("suspect"):
        return True
    return None  # 'unknown'


# Old scheme. Absent keys (N/A, UNKNOWN, OTHER) fall through to (None, None)
# via NULL_TOKENS.
_OLD_SCHEME: dict[str, tuple[bool | None, bool | None]] = {
    "none detected": (False, False),
    "alcohol present": (True, None),
    "alcohol contributed": (True, None),
    "illegal drug present": (None, True),
    "illegal drug contributed": (None, True),
    "medication present": (None, True),
    "medication contributed": (None, True),
    "combined substance present": (True, True),
    "combination contributed": (True, True),
}
