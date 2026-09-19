"""End-to-end persistence + crash-recovery against the dev RDS ``cap_test`` database.

Each ``invoke()`` call opens and closes its own ``open_checkpointer`` context, so calling
it twice for the same ``thread_id`` *is* the crash/restart scenario: the second call
rebuilds the checkpointer and graph from the DSN and resumes purely from Postgres state.
The LLM is the ``CAP_FAKE_MODEL`` stub, so no Bedrock call is made.
"""

from __future__ import annotations

import psycopg
import pytest

from compliance_agent_platform.adapters.mock import default_deps
from compliance_agent_platform.checkpoint.postgres import open_checkpointer
from compliance_agent_platform.config import Settings
from compliance_agent_platform.graph.builder import build_graph
from compliance_agent_platform.runtime.contract import invoke

pytestmark = pytest.mark.integration


def _audit_trail(dsn: str, alert_id: str) -> list[tuple[str, str, str]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT step, actor, event FROM screening_audit WHERE alert_id = %s ORDER BY id",
            (alert_id,),
        ).fetchall()


def test_clear_alert_disposes_and_persists(clean_db, load_alert, monkeypatch):
    monkeypatch.setenv("CAP_FAKE_MODEL", "clear")
    result = invoke({"alert": load_alert("alert_clear")})

    assert result["status"] == "disposed"
    assert result["thread_id"] == "ALRT-CLEAR-001"
    assert result["disposition"]["disposition"] == "clear"
    assert result["disposition"]["decided_by"] == "agent"

    trail = _audit_trail(clean_db, "ALRT-CLEAR-001")
    assert [(s, e) for s, _, e in trail] == [
        ("intake", "node_completed"),
        ("enrich", "node_completed"),
        ("evaluate", "node_completed"),
        ("dispose", "disposition"),
    ]


def test_true_match_escalates_then_resumes_after_restart(clean_db, load_alert, monkeypatch):
    monkeypatch.setenv("CAP_FAKE_MODEL", "true_match")
    alert = load_alert("alert_true_match")
    tid = alert["alert_id"]

    # (b) first invocation parks at the HITL interrupt
    escalated = invoke({"alert": alert})
    assert escalated["status"] == "escalated"
    assert escalated["thread_id"] == tid
    interrupt_payload = escalated["interrupt"]
    assert interrupt_payload["alert_id"] == tid
    assert interrupt_payload["agent_recommendation"] == "true_match"
    assert "agent_rationale" in interrupt_payload

    # (c) a brand-new invocation (fresh checkpointer + graph) resumes from Postgres
    disposed = invoke(
        {
            "thread_id": tid,
            "resume": {
                "resolution": "true_match",
                "analyst_id": "A123",
                "rationale": "confirmed DOB and nationality match",
            },
        }
    )
    assert disposed["status"] == "disposed"
    d = disposed["disposition"]
    assert d["disposition"] == "true_match"
    assert d["decided_by"] == "analyst"
    assert d["analyst_id"] == "A123"
    assert d["confidence"] is None
    assert d["matched_entities"]  # fell back to the evaluation's matched entity

    # (d) audit trail — evaluate runs once; escalate may re-emit interrupt_raised on replay
    trail = [(s, a, e) for s, a, e in _audit_trail(clean_db, tid)]
    pairs = [(s, e) for s, _, e in trail]
    assert pairs[:3] == [
        ("intake", "node_completed"),
        ("enrich", "node_completed"),
        ("evaluate", "node_completed"),
    ]
    assert ("escalate", "interrupt_raised") in pairs
    assert pairs.count(("escalate", "resumed")) == 1
    assert pairs.count(("evaluate", "node_completed")) == 1
    assert pairs[-1] == ("dispose", "disposition")
    assert trail[-1][1] == "analyst"  # dispose actor
    assert pairs.index(("escalate", "resumed")) < pairs.index(("dispose", "disposition"))

    # (e) full checkpoint lineage is replayable from a fresh graph
    cfg = {"configurable": {"thread_id": tid}}
    with open_checkpointer(Settings(database_url=clean_db)) as p:
        graph = build_graph(
            checkpointer=p.checkpointer, audit_conn=p.audit_conn, deps=default_deps()
        )
        history = list(graph.get_state_history(cfg))
    assert len(history) >= 6
    assert history[0].next == ()  # most-recent checkpoint is terminal
    assert history[0].values["disposition"]["decided_by"] == "analyst"
