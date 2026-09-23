"""`compliance_mw`: node-wrapping failure auditing + citation enforcement (no DB/network)."""

from __future__ import annotations

from datetime import date

import pytest
from fakes import FakeAuditConn, FakeEmbedder, FakeVectorStore
from langgraph.checkpoint.memory import InMemorySaver

from compliance_agent_platform.adapters.mock import default_deps
from compliance_agent_platform.compliance_mw import ComplianceViolationError, wrap_node
from compliance_agent_platform.compliance_mw.citation import check_dispose_citation
from compliance_agent_platform.config import Settings
from compliance_agent_platform.graph.builder import NodeContext, build_graph
from compliance_agent_platform.schemas.alert import ScreeningAlert, WatchlistHit
from compliance_agent_platform.schemas.disposition import DecidedBy, Disposition, DispositionType
from compliance_agent_platform.schemas.evaluation import EvaluationResult

_SETTINGS = Settings(
    clear_confidence_threshold=0.85,
    bedrock_model_id="test-model-id",
    prompt_version="test-v1",
    queue="sanctions_screening",
)


def _ctx(audit_conn=None) -> NodeContext:
    return NodeContext(
        deps=default_deps(),
        audit_conn=audit_conn or FakeAuditConn(),
        settings=_SETTINGS,
        model=None,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )


def _disposition(**overrides) -> Disposition:
    base = dict(
        alert_id="A1",
        disposition=DispositionType.CLEAR,
        rationale="not a match",
        confidence=0.9,
        decided_by=DecidedBy.AGENT,
        model_id="test-model-id",
    )
    base.update(overrides)
    return Disposition(**base)


# --- wrap_node: success / failure ---------------------------------------------


def test_wrap_node_passes_through_on_success():
    ctx = _ctx()
    wrapped = wrap_node("intake", lambda state: {"alert": state["alert"]}, ctx)

    result = wrapped({"alert": {"alert_id": "A1"}})

    assert result == {"alert": {"alert_id": "A1"}}
    assert ctx.audit_conn.rows == []  # no failure row on success, no post-hook for "intake"


def test_wrap_node_audits_and_reraises_generic_exception():
    ctx = _ctx()

    def boom(state):
        raise ValueError("bad enrichment payload")

    wrapped = wrap_node("enrich", boom, ctx)

    with pytest.raises(ValueError, match="bad enrichment payload"):
        wrapped({"alert": {"alert_id": "A1"}})

    assert len(ctx.audit_conn.rows) == 1
    row = ctx.audit_conn.rows[0]
    assert row["step"] == "enrich"
    assert row["event"] == "node_failed"
    assert row["alert_id"] == "A1"
    assert "bad enrichment payload" in row["detail"].obj["error"]


def test_wrap_node_handles_missing_alert_in_state_defensively():
    ctx = _ctx()

    def boom(state):
        raise RuntimeError("parse failure")

    wrapped = wrap_node("intake", boom, ctx)

    with pytest.raises(RuntimeError):
        wrapped({})

    assert ctx.audit_conn.rows[0]["alert_id"] == "unknown"


def test_wrap_node_reraises_graph_bubble_up_without_audit_row():
    # interrupt()/Command control flow raises a GraphBubbleUp (GraphInterrupt);
    # wrap_node must let it through untouched, not misrecord an HITL pause as
    # a node failure. Raised directly here (rather than via interrupt(), which
    # requires a live LangGraph runnable context) to unit-test this branch in
    # isolation; the full-graph variant below covers the real code path.
    from langgraph.errors import GraphInterrupt

    ctx = _ctx()

    def raises_bubble_up(state):
        raise GraphInterrupt()

    wrapped = wrap_node("escalate", raises_bubble_up, ctx)

    with pytest.raises(GraphInterrupt):
        wrapped({"alert": {"alert_id": "A1"}})
    assert ctx.audit_conn.rows == []


def test_wrap_node_true_escalate_via_full_graph_emits_no_failure_row():
    # End-to-end version of the above through the real escalate node + a
    # compiled graph, confirming the interrupt path is genuinely exception-free
    # from the wrapper's point of view.
    conn = FakeAuditConn()
    evaluation = EvaluationResult(
        recommended=DispositionType.TRUE_MATCH,
        confidence=0.9,
        rationale="match",
        matched_entities=[],
    )

    class _FakeModel:
        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            return evaluation

    alert = ScreeningAlert(
        alert_id="A1",
        subject_id="CUST-1001",
        screened_name="Viktor Petrov",
        date_of_birth=date(1969, 4, 12),
        nationality="RU",
        hits=[
            WatchlistHit(
                list_name="OFAC SDN", entry_id="SDN-12345", matched_name="Viktor Petrov", score=0.95
            )
        ],
    ).model_dump(mode="json")

    graph = build_graph(
        checkpointer=InMemorySaver(),
        audit_conn=conn,
        deps=default_deps(),
        settings=_SETTINGS,
        model=_FakeModel(),
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    cfg = {"configurable": {"thread_id": "A1"}}
    graph.invoke({"alert": alert}, cfg)

    assert graph.get_state(cfg).interrupts
    assert all(row["event"] != "node_failed" for row in conn.rows)


# --- citation enforcement ------------------------------------------------------


def test_check_dispose_citation_passes_when_evidence_present():
    d = _disposition(evidence=[{"source": "OFAC SDN", "detail": "name match"}])
    ctx = _ctx()

    check_dispose_citation(ctx, {}, {"disposition": d.model_dump(mode="json")})

    assert ctx.audit_conn.rows == []


@pytest.mark.parametrize("disposition_type", [DispositionType.CLEAR, DispositionType.TRUE_MATCH])
def test_check_dispose_citation_blocks_when_evidence_missing(disposition_type):
    overrides = {"evidence": []}
    if disposition_type is DispositionType.TRUE_MATCH:
        overrides["matched_entities"] = [
            {"list_name": "OFAC SDN", "entry_id": "SDN-1", "primary_name": "X"}
        ]
    d = _disposition(disposition=disposition_type, **overrides)
    ctx = _ctx()

    with pytest.raises(ComplianceViolationError) as excinfo:
        check_dispose_citation(ctx, {}, {"disposition": d.model_dump(mode="json")})

    assert excinfo.value.disposition_type == disposition_type.value
    assert len(ctx.audit_conn.rows) == 1
    assert ctx.audit_conn.rows[0]["event"] == "compliance_blocked"


def test_check_dispose_citation_exempts_insufficient_data():
    d = _disposition(disposition=DispositionType.INSUFFICIENT_DATA, evidence=[], confidence=None)
    ctx = _ctx()

    check_dispose_citation(ctx, {}, {"disposition": d.model_dump(mode="json")})

    assert ctx.audit_conn.rows == []


def test_dispose_hook_wired_into_wrap_node_for_dispose_only():
    ctx = _ctx()
    d = _disposition(evidence=[])

    blocked = wrap_node("dispose", lambda state: {"disposition": d.model_dump(mode="json")}, ctx)
    with pytest.raises(ComplianceViolationError):
        blocked({})

    # same bad disposition through a different step name: no hook registered, passes.
    ctx2 = _ctx()
    passthrough = wrap_node(
        "evaluate", lambda state: {"disposition": d.model_dump(mode="json")}, ctx2
    )
    assert passthrough({}) == {"disposition": d.model_dump(mode="json")}
