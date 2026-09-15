"""Immutable decision lineage.

Part 5: *"if you cannot reconstruct why a record was released eighteen months
from now, you have not built a compliance control, you have built a filter."*

So every decision writes one append-only row capturing the inputs it saw, the
ruleset version that judged them, and each gate's verdict. DPPA §2721(c)
independently requires 5-year redisclosure records naming each recipient and
permitted purpose, which this table is the substrate for.

Append-only is enforced by convention plus a check constraint on the writer —
nothing here ever issues an UPDATE or DELETE.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compliance_decision_lineage (
    decision_lineage_id VARCHAR PRIMARY KEY,
    subject_id          VARCHAR NOT NULL,
    ruleset_version     VARCHAR NOT NULL,
    channel             VARCHAR NOT NULL,
    jurisdiction        VARCHAR,
    status              VARCHAR NOT NULL,
    blocked_until_date  DATE,
    reason_codes        VARCHAR NOT NULL,
    legal_basis         VARCHAR NOT NULL,
    gate_verdicts       VARCHAR NOT NULL,
    input_digest        VARCHAR NOT NULL,
    evaluated_at        TIMESTAMPTZ NOT NULL
);
"""


@dataclass
class GateVerdict:
    gate: str
    passed: bool
    codes: list[str] = field(default_factory=list)
    detail: str | None = None


def new_lineage_id() -> str:
    return uuid.uuid4().hex


def input_digest(record: dict) -> str:
    """Hash of the inputs the decision saw.

    Direct identifiers are excluded: the digest must be reproducible from the
    analytic record without the vault. It proves *which values* were judged,
    not who they belonged to.
    """
    redacted = {
        k: v for k, v in sorted(record.items())
        if k not in {"full_name", "street_address", "phone_e164"}
    }
    canonical = json.dumps(redacted, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def ensure_schema(con) -> None:
    con.execute(_SCHEMA)


def write(con, *, decision, subject_id: str, channel: str, jurisdiction: str | None,
          verdicts: list[GateVerdict], record: dict) -> None:
    con.execute(
        """
        INSERT INTO compliance_decision_lineage
            (decision_lineage_id, subject_id, ruleset_version, channel, jurisdiction,
             status, blocked_until_date, reason_codes, legal_basis, gate_verdicts,
             input_digest, evaluated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            decision.decision_lineage_id, subject_id, decision.ruleset_version,
            channel, jurisdiction, decision.status, decision.blocked_until_date,
            json.dumps([str(c) for c in decision.reason_codes]),
            json.dumps(list(decision.legal_basis)),
            json.dumps([asdict(v) for v in verdicts]),
            input_digest(record),
            decision.evaluated_at,
        ],
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
