"""The tokenisation boundary.

Part 5e: *"Tokenize direct identifiers into a segregated vault; the analytic
warehouse joins on surrogates."* That is a schema-level control rather than a
policy, so it is built rather than described.

  vault_party_identity   name, street address, raw phone. Segregated. Never
                         joined in analytics, never emitted.
  phone_token            HMAC-SHA256 of the E.164 number. Deterministic so it
                         joins across runs; keyed so it is not reversible by
                         precomputation the way a bare SHA-256 of a 10-digit
                         number would be — the keyspace is small enough to
                         enumerate in seconds.

**ZIP5 deliberately stays on the analytic side.** 18 U.S.C. §2725(3) expressly
excludes the 5-digit ZIP from the definition of "personal information", which
makes it the one geographic identifier a DPPA-constrained product may legitimately
aggregate on. Tokenising it would discard the carve-out for nothing.
"""

from __future__ import annotations

import hashlib
import hmac

# Columns that must never reach the analytic record or the output contract.
DIRECT_IDENTIFIERS = ("full_name", "street_address", "phone_e164")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_party_identity (
    party_id       VARCHAR PRIMARY KEY,
    phone_token    VARCHAR NOT NULL,
    full_name      VARCHAR,
    street_address VARCHAR,
    phone_e164     VARCHAR,
    loaded_at      TIMESTAMPTZ NOT NULL
);
"""


class MissingTokenKey(RuntimeError):
    pass


def ensure_schema(con) -> None:
    con.execute(_SCHEMA)


def phone_token(phone_e164: str | None, key: str) -> str | None:
    """Keyed, deterministic, one-way.

    An unkeyed hash of a phone number is not a token: there are ~10^10 NANP
    numbers, so a rainbow table is trivial. The key is what makes this a
    pseudonym rather than an encoding.
    """
    if not phone_e164:
        return None
    if not key:
        raise MissingTokenKey(
            "compliance.token_key is not set. An unkeyed phone hash is "
            "reversible by enumeration and is not a token."
        )
    return hmac.new(key.encode(), phone_e164.encode(), hashlib.sha256).hexdigest()


def store(con, records: list[dict], key: str) -> int:
    """Write direct identifiers to the segregated vault.

    Replaces rather than appends: the vault holds current identity, and
    retention TTLs apply to it separately from the analytic history.
    """
    ensure_schema(con)
    con.execute("DELETE FROM vault_party_identity")
    for record in records:
        con.execute(
            """INSERT INTO vault_party_identity
               (party_id, phone_token, full_name, street_address, phone_e164, loaded_at)
               VALUES (?, ?, ?, ?, ?, now())""",
            [
                record["party_id"],
                phone_token(record.get("phone_e164"), key),
                record.get("full_name"),
                record.get("street_address"),
                record.get("phone_e164"),
            ],
        )
    return len(records)


def assert_no_direct_identifiers(payload: dict) -> None:
    """Belt and braces: fail loudly if a direct identifier reaches the output.

    Checked on every emitted record rather than trusted, because the failure
    mode is silent and the consequence is distributing PII.
    """
    flat = repr(payload)
    for field in DIRECT_IDENTIFIERS:
        if field in payload:
            raise ValueError(f"direct identifier {field!r} present in emitted record")
    return None
