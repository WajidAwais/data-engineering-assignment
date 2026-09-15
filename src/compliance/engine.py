"""The eligibility engine.

Design notes, which are also grading criteria:

1.  The default disposition is INELIGIBLE. Eligibility is proven, never assumed.
2.  Rules are DATA, loaded from config. A new jurisdiction with a new waiting
    period must be a row in a table and a version bump — not a code change.
    You will be asked to demonstrate this in the live defence.
3.  Every decision emits an immutable lineage record. If you cannot reconstruct
    why a record was released eighteen months from now, you have not built a
    compliance control, you have built a filter.
4.  Rulesets are versioned (semver) and every decision records the version that
    produced it. The law moves; see Part 5d of the assignment.

Implementation notes
--------------------
No statute, window or threshold is written in this file. Everything comes from
`compliance/rules.yaml` and `config/blackout_windows.csv`. Grep this module for
a number and you should find only list indices.

Gates accumulate ALL applicable reason codes rather than short-circuiting on the
first. The memo's exclusion table is a count by reason, so a record blocked for
four independent reasons has to report four.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml

from src.geo.timezone import TimezoneResolver

from . import lineage, windows
from .lineage import GateVerdict
from .reason_codes import ReasonCode, citation_for

Status = Literal["ELIGIBLE", "INELIGIBLE", "BLOCKED_UNTIL"]


@dataclass(frozen=True)
class EligibilityDecision:
    status: Status
    reason_codes: list[ReasonCode]
    legal_basis: list[str]
    blocked_until_date: date | None
    decision_lineage_id: str
    evaluated_at: datetime
    ruleset_version: str


@dataclass(frozen=True)
class BlackoutRule:
    jurisdiction: str
    record_type: str
    days_from: int
    anchor_field: str
    reason_code: str
    legal_basis: str


def load_blackout_windows(path: str | Path) -> list[BlackoutRule]:
    rules = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row.get("days_from"):
                continue  # placeholder row, not yet researched
            rules.append(BlackoutRule(
                jurisdiction=row["jurisdiction"].strip(),
                record_type=row["record_type"].strip(),
                days_from=int(row["days_from"]),
                anchor_field=row["anchor_field"].strip(),
                reason_code=row["reason_code"].strip(),
                legal_basis=row["legal_basis"].strip(),
            ))
    return rules


class EligibilityEngine:
    def __init__(self, ruleset_path: str, ruleset_version: str | None = None) -> None:
        self.ruleset_path = ruleset_path
        self.rules = yaml.safe_load(Path(ruleset_path).read_text())
        # The file is the source of truth for its own version; the argument is
        # kept for the documented signature and to allow an explicit override.
        self.ruleset_version = ruleset_version or self.rules["version"]
        self.channel = self.rules["channel"]
        self.blackout = load_blackout_windows(
            self.rules["blackout_windows"]["source"]
        )
        self._resolver = TimezoneResolver()

    # -- gates ---------------------------------------------------------------

    def _gate_quality(self, record: dict) -> GateVerdict:
        """A party we cannot place cannot be given a lawful calling window."""
        if not self.rules["quality_gates"].get("require_timezone"):
            return GateVerdict("quality", True)

        zone, window = windows.resolve(
            self.rules,
            jurisdiction=record.get("jurisdiction"),
            latitude=record.get("latitude"),
            longitude=record.get("longitude"),
            npa=record.get("phone_npa"),
            resolver=self._resolver,
        )
        record["_iana_timezone"] = zone
        record["_calling_window"] = window

        if zone is None:
            return GateVerdict("quality", False, [ReasonCode.TIMEZONE_UNRESOLVED],
                               "no zone from coordinates or NPA")
        codes = []
        if window and window.sources_disagreed:
            # Not blocking — the window was narrowed. Recorded so the narrowing
            # is visible rather than silent.
            codes.append(ReasonCode.CALLING_WINDOW_SOURCES_DISAGREE)
        return GateVerdict("quality", True, codes, f"zone={zone} via {window.basis}")

    def _gate_source(self, record: dict) -> GateVerdict:
        """DPPA and provenance. Applies before any channel question."""
        codes, details = [], []
        for gate in self.rules["source_gates"]:
            satisfied_by = gate["satisfied_by"]
            if not record.get(satisfied_by):
                codes.append(ReasonCode[gate["code"]])
                details.append(f"{gate['code']}: {satisfied_by} not satisfied")
        return GateVerdict("source", not codes, codes, "; ".join(details) or None)

    def _gate_channel(self, record: dict) -> GateVerdict:
        """Categorical bars that attach to the channel itself."""
        spec = self.rules["channels"][self.channel]
        jurisdiction = (record.get("jurisdiction") or "").upper()
        codes = [
            ReasonCode[bar["code"]]
            for bar in spec.get("categorical_bars", [])
            if jurisdiction in bar["applies_to"]
        ]
        return GateVerdict("channel", not codes, codes,
                           f"channel={self.channel} jurisdiction={jurisdiction}")

    def _gate_timing(self, record: dict, today: date) -> tuple[GateVerdict, date | None]:
        """Blackout windows. Longest applicable window binds."""
        spec = self.rules["channels"][self.channel]
        jurisdiction = (record.get("jurisdiction") or "").upper()

        # The data-access gate governs whether we may hold the record at all, so
        # it applies on every channel. The solicitation gate is channel-specific.
        applicable = {self.rules["blackout_windows"]["default_record_type"]}
        if spec.get("blackout_record_type"):
            applicable.add(spec["blackout_record_type"])

        earliest: date | None = None
        basis: list[str] = []
        binding: BlackoutRule | None = None
        for rule in self.blackout:
            if rule.jurisdiction != jurisdiction or rule.record_type not in applicable:
                continue
            anchor = record.get(rule.anchor_field)
            if anchor is None:
                continue
            candidate = anchor + timedelta(days=rule.days_from)
            if earliest is None or candidate > earliest:
                earliest, basis, binding = candidate, [rule.legal_basis], rule
            elif candidate == earliest:
                basis.append(rule.legal_basis)

        if earliest is None or earliest <= today:
            return GateVerdict("timing", True, [], f"earliest_contact={earliest}"), None

        # The code comes from the blackout table, not from a dict in this file.
        # Adding Ohio's 45-day window must be a CSV row, not an edit here.
        code = (ReasonCode[binding.reason_code] if binding and binding.reason_code
                else ReasonCode.PROVENANCE_UNKNOWN)
        return (
            GateVerdict("timing", False, [code], f"blocked until {earliest}: {'; '.join(basis)}"),
            earliest,
        )

    def _gate_contact(self, record: dict) -> GateVerdict:
        """DNC, RND, line type, consent."""
        cfg = self.rules["contact_gates"]
        codes, details = [], []

        dnc = cfg["dnc"]
        if dnc["national_listed_blocks"] and record.get("on_national_dnc"):
            codes.append(ReasonCode.DNC_LISTED)
        age = record.get("dnc_scrub_age_days")
        if age is None or age > dnc["max_scrub_age_days"]:
            codes.append(ReasonCode.DNC_SCRUB_STALE)
            details.append(f"scrub age {age}d > {dnc['max_scrub_age_days']}d")

        rnd = cfg["reassigned_numbers"]
        response = record.get("rnd_response")
        if response in rnd["blocking_responses"]:
            codes.append(ReasonCode.RND_REASSIGNED)
        elif response not in rnd["safe_harbour_responses"]:
            # "No Data" is NOT a green light — the single most common
            # engineering misreading of the RND.
            codes.append(ReasonCode.RND_NO_DATA_NO_SAFE_HARBOR)
            details.append(f"rnd={response!r} carries no safe harbour")

        line_type = record.get("line_type_reported")
        permissive = cfg["line_type"]["permissive"]
        # voip and unknown route to the MOST restrictive treatment, never the
        # most permissive. Recorded as a routing fact rather than a reason code:
        # most records are wireless, so emitting a code would flood the
        # exclusion table without excluding anything.
        treatment = "permissive" if line_type in permissive else "restrictive"
        record["_line_type_treatment"] = treatment
        details.append(f"line_type={line_type!r} -> {treatment}")
        if line_type in (None, "unknown"):
            codes.append(ReasonCode.LINE_TYPE_UNRESOLVED)

        consent = cfg["consent"]
        if consent["required"]:
            if record.get("consent_revoked"):
                codes.append(ReasonCode.CONSENT_REVOKED)
            elif not record.get("consent_on_file"):
                codes.append(ReasonCode.CONSENT_ABSENT)
            elif consent.get("require_provenance"):
                # A consent boolean is not consent. The burden of proof is on
                # the caller, so a claim with no disclosure text, hash, URL,
                # timestamp, IP, user agent or seller chain is unverifiable and
                # therefore functionally absent.
                missing = [f for f in consent.get("provenance_fields", [])
                           if not record.get(f"consent_{f}")]
                if missing:
                    codes.append(ReasonCode.CONSENT_UNVERIFIABLE)
                    details.append(f"consent provenance missing: {', '.join(missing)}")

        return GateVerdict("contact", not codes, codes, "; ".join(details) or None)

    # -- entry point ---------------------------------------------------------

    def evaluate(self, record: dict, *, today: date | None = None) -> EligibilityDecision:
        """Return a complete, auditable decision for one record.

        Starts at INELIGIBLE and moves toward ELIGIBLE only by affirmatively
        satisfying each gate. Accumulates ALL applicable reason codes.
        """
        decision, _ = self.evaluate_with_lineage(record, today=today)
        return decision

    def evaluate_with_lineage(self, record: dict, *, today: date | None = None):
        """Evaluate and also return the per-gate verdicts for the audit record.

        This is the single implementation; `evaluate()` delegates here so the
        two can never drift apart.
        """
        today = today or date.today()
        verdicts = [
            self._gate_quality(record),
            self._gate_source(record),
            self._gate_channel(record),
            self._gate_contact(record),
        ]
        timing_verdict, blocked_until = self._gate_timing(record, today)
        verdicts.append(timing_verdict)

        codes: list[ReasonCode] = []
        for verdict in verdicts:
            codes.extend(verdict.codes)

        hard_blocked = any(not v.passed for v in verdicts if v.gate != "timing")
        if hard_blocked:
            status: Status = "INELIGIBLE"
        elif not timing_verdict.passed:
            status = "BLOCKED_UNTIL"
        else:
            status = "ELIGIBLE"
            codes.append(ReasonCode.ELIGIBLE_CONSENTED)

        decision = EligibilityDecision(
            status=status,
            reason_codes=codes,
            legal_basis=self.citations_for(codes),
            blocked_until_date=blocked_until if status == "BLOCKED_UNTIL" else None,
            decision_lineage_id=lineage.new_lineage_id(),
            evaluated_at=lineage.utcnow(),
            ruleset_version=self.ruleset_version,
        )
        return decision, verdicts

    @staticmethod
    def citations_for(codes: list[ReasonCode]) -> list[str]:
        """A reason code without a legal basis is an opinion, not a control.

        Reads the CITATIONS registry rather than `code.__doc__`: Enum members do
        not retain a docstring written after the assignment, so __doc__ is empty
        for every member and this silently emitted "no citation recorded" on
        every decision. Found by the citation test, not by review.
        """
        seen, out = set(), []
        for code in codes:
            text = citation_for(code)
            if text not in seen:
                seen.add(text)
                out.append(text)
        return out
