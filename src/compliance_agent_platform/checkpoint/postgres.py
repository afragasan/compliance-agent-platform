"""Open a PostgreSQL-backed checkpointer plus a dedicated audit connection.

LangGraph persists a checkpoint after every super-step, so an unclean process exit
loses nothing: re-invoking the graph with the same ``thread_id`` resumes from the last
committed checkpoint, and a pending HITL ``interrupt`` stays parked in Postgres until an
analyst resumes it.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver

from compliance_agent_platform.audit.log import ensure_audit_schema
from compliance_agent_platform.config import Settings, get_settings


@dataclass
class Persistence:
    checkpointer: PostgresSaver
    audit_conn: psycopg.Connection


@contextmanager
def open_checkpointer(settings: Settings | None = None) -> Iterator[Persistence]:
    """Yield a :class:`Persistence` bundle, creating all schema on first use."""
    settings = settings or get_settings()
    dsn = settings.resolved_dsn()

    with ExitStack() as stack:
        checkpointer = stack.enter_context(PostgresSaver.from_conn_string(dsn))
        checkpointer.setup()

        audit_conn = stack.enter_context(psycopg.connect(dsn, autocommit=False))
        ensure_audit_schema(audit_conn)

        yield Persistence(checkpointer=checkpointer, audit_conn=audit_conn)
