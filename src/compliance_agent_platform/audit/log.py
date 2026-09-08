"""Writer for the append-only ``screening_audit`` table."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from importlib import resources
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel


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


def write_audit(conn: Connection, record: AuditRecord) -> None:
    conn.execute(
        """
        INSERT INTO screening_audit (
            alert_id, thread_id, checkpoint_id, step, actor, event,
            payload_hash, llm_request, llm_response, model_id, prompt_version, detail
        ) VALUES (
            %(alert_id)s, %(thread_id)s, %(checkpoint_id)s, %(step)s, %(actor)s, %(event)s,
            %(payload_hash)s, %(llm_request)s, %(llm_response)s, %(model_id)s,
            %(prompt_version)s, %(detail)s
        )
        """,
        {
            **record.model_dump(),
            "llm_request": _as_jsonb(record.llm_request),
            "llm_response": _as_jsonb(record.llm_response),
            "detail": _as_jsonb(record.detail),
        },
    )
    conn.commit()


def _as_jsonb(value: dict | None) -> Jsonb | None:
    return None if value is None else Jsonb(value, dumps=lambda v: json.dumps(v, default=_default))
