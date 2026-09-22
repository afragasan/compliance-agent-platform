"""Writer for the append-only, hash-chained ``screening_audit`` table."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from importlib import resources
from typing import Any, NamedTuple

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel

# Fixed key scoping the audit hash-chain's advisory lock ("CAP1" as an int). Any
# process appending to `screening_audit` takes this lock first, serializing
# chain-tail reads against concurrent writers; it is released automatically at
# commit (`pg_advisory_xact_lock` is transaction-scoped).
_CHAIN_LOCK_KEY = 0x43415031

# Fixed prev_hash for the first row ever chained.
_CHAIN_GENESIS = "0" * 64


def _default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def hash_payload(payload: Any) -> str:
    """Stable sha256 of a node input, used to prove what the agent saw."""
    blob = json.dumps(payload, sort_keys=True, default=_default).encode()
    return hashlib.sha256(blob).hexdigest()


class AuditRecord(BaseModel):
    alert_id: str
    thread_id: str
    step: str
    actor: str
    event: str
    checkpoint_id: str | None = None
    payload_hash: str | None = None
    llm_request: dict | None = None
    llm_response: dict | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    detail: dict | None = None


def ensure_audit_schema(conn: Connection) -> None:
    """Idempotently create the audit table / trigger. Run once at startup."""
    sql = resources.files("compliance_agent_platform.audit").joinpath("schema.sql").read_text()
    conn.execute(sql)
    conn.commit()


def _chain_hash(record: AuditRecord, created_at: datetime, prev_hash: str) -> str:
    """sha256 committing this row's business fields + created_at + prev_hash."""
    payload = {
        **record.model_dump(mode="json"),
        "created_at": created_at.isoformat(),
        "prev_hash": prev_hash,
    }
    blob = json.dumps(payload, sort_keys=True, default=_default).encode()
    return hashlib.sha256(blob).hexdigest()


def write_audit(conn: Connection, record: AuditRecord) -> None:
    """Append one row to the hash-chained, append-only audit log.

    Chain-appends are serialized with a transaction-scoped advisory lock so two
    concurrent writers can't both read the same chain tail; each row's
    ``record_hash`` commits to its own business fields plus the previous row's
    hash, so deleting or reordering any row breaks every hash after it. This is
    additive to (not a replacement for) the table's ``BEFORE UPDATE OR DELETE``
    trigger, which remains the primary in-place-tamper guard.
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK_KEY,))
    tail = conn.execute(
        "SELECT record_hash FROM screening_audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = tail[0] if tail and tail[0] else _CHAIN_GENESIS
    created_at = datetime.now(timezone.utc)
    record_hash = _chain_hash(record, created_at, prev_hash)

    conn.execute(
        """
        INSERT INTO screening_audit (
            alert_id, thread_id, checkpoint_id, step, actor, event,
            payload_hash, llm_request, llm_response, model_id, prompt_version, detail,
            created_at, prev_hash, record_hash
        ) VALUES (
            %(alert_id)s, %(thread_id)s, %(checkpoint_id)s, %(step)s, %(actor)s, %(event)s,
            %(payload_hash)s, %(llm_request)s, %(llm_response)s, %(model_id)s,
            %(prompt_version)s, %(detail)s,
            %(created_at)s, %(prev_hash)s, %(record_hash)s
        )
        """,
        {
            **record.model_dump(),
            "llm_request": _as_jsonb(record.llm_request),
            "llm_response": _as_jsonb(record.llm_response),
            "detail": _as_jsonb(record.detail),
            "created_at": created_at,
            "prev_hash": prev_hash,
            "record_hash": record_hash,
        },
    )
    conn.commit()


def _as_jsonb(value: dict | None) -> Jsonb | None:
    return None if value is None else Jsonb(value, dumps=lambda v: json.dumps(v, default=_default))


class ChainVerificationResult(NamedTuple):
    ok: bool
    broken_at_id: Any | None = None
    reason: str | None = None


def verify_chain(rows: list[dict], start_hash: str = _CHAIN_GENESIS) -> ChainVerificationResult:
    """Recompute the hash chain over ``rows`` and report the first break, if any.

    ``rows`` must be plain dicts ordered by ``id`` ascending (e.g. from
    ``SELECT * FROM screening_audit ORDER BY id``, or an exported JSONL file).
    Rows are order-based, not step-based: two rows with an identical
    step/event (e.g. ``escalate``'s known replay-duplicate ``interrupt_raised``
    rows) verify fine as long as each links to the actual previous row.

    ``start_hash`` defaults to the genesis constant, for verifying a chain from
    its true beginning. Pass the previous batch's chain-head hash to verify a
    later *sub-range* of the chain (e.g. one S3 export batch resuming from an
    earlier one) without needing the full history.
    """
    prev_hash = start_hash
    for row in rows:
        record = AuditRecord.model_validate(row)
        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)

        stored_prev = row.get("prev_hash") or _CHAIN_GENESIS
        if stored_prev != prev_hash:
            return ChainVerificationResult(
                False, row.get("id"), "prev_hash does not match the previous row's record_hash"
            )

        expected_hash = _chain_hash(record, created_at, prev_hash)
        if expected_hash != row.get("record_hash"):
            return ChainVerificationResult(
                False, row.get("id"), "record_hash does not match the recomputed hash"
            )
        prev_hash = expected_hash

    return ChainVerificationResult(True)
