"""Node-wrapping middleware: audits node failures, dispatches post-node policy hooks.

Every node already writes its own fine-grained audit row on success (see
``graph/nodes.py``); duplicating that here as a coarse "node entered/exited"
event would just double audit volume for no new compliance value. What this
wrapper adds that doesn't exist today is (1) a `node_failed` audit row when a
node raises, closing a real blind spot, and (2) a place to hang post-node
policy checks like citation enforcement without touching node bodies.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from langgraph.errors import GraphBubbleUp

from compliance_agent_platform.audit.log import AuditRecord, write_audit
from compliance_agent_platform.compliance_mw.citation import check_dispose_citation
from compliance_agent_platform.schemas.state import ScreeningState

if TYPE_CHECKING:
    from compliance_agent_platform.graph.builder import NodeContext

Node = Callable[[ScreeningState], dict]
PostHook = Callable[["NodeContext", ScreeningState, dict], None]

# Per-node checks run after a node succeeds, before its output reaches graph
# state. A hook may raise (e.g. ComplianceViolationError) to block the write.
_POST_HOOKS: dict[str, PostHook] = {
    "dispose": check_dispose_citation,
}


def wrap_node(name: str, fn: Node, ctx: NodeContext) -> Node:
    """Wrap a node with failure auditing and any registered post-node hook."""

    def wrapped(state: ScreeningState) -> dict:
        try:
            result = fn(state)
        except GraphBubbleUp:
            # interrupt()/Command control flow (e.g. escalate's HITL pause) —
            # not a node failure, must propagate untouched for LangGraph to
            # handle.
            raise
        except Exception as exc:
            _audit_node_failure(ctx, state, name, exc)
            raise

        hook = _POST_HOOKS.get(name)
        if hook is not None:
            hook(ctx, state, result)
        return result

    return wrapped


def _audit_node_failure(ctx: NodeContext, state: ScreeningState, name: str, exc: Exception) -> None:
    alert = state.get("alert") or {}
    alert_id = alert.get("alert_id", "unknown")
    write_audit(
        ctx.audit_conn,
        AuditRecord(
            alert_id=alert_id,
            thread_id=alert_id,
            step=name,
            actor="system",
            event="node_failed",
            detail={"error": repr(exc), "error_type": type(exc).__name__},
        ),
    )
