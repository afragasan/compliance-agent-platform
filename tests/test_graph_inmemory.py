"""End-to-end graph behaviour on an in-memory checkpointer (no DB, no network).

Warnings are errors here so a checkpoint-serialization regression (e.g. state channels
holding un-registered Pydantic types) fails the suite loudly.
"""

from __future__ import annotations

from datetime import date

import pytest
from fakes import FakeAuditConn
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from compliance_agent_platform.adapters.mock import default_deps
from compliance_agent_platform.config import Settings
from compliance_agent_platform.graph.builder import build_graph
from compliance_agent_platform.schemas.alert import ScreeningAlert, WatchlistHit
from compliance_agent_platform.schemas.disposition import DispositionType, MatchedEntity
from compliance_agent_platform.schemas.evaluation import EvaluationResult

pytestmark = pytest.mark.filterwarnings("error::UserWarning")

_SETTINGS = Settings(
    clear_confidence_threshold=0.85,
    bedrock_model_id="test-model-id",
    prompt_version="test-v1",
    queue="sanctions_screening",
)


class _FakeModel:
    def __init__(self, result: EvaluationResult) -> None:
        self._result = result

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        return self._result


def _graph(result: EvaluationResult, saver: InMemorySaver | None = None):
    return build_graph(
        checkpointer=saver or InMemorySaver(),
        audit_conn=FakeAuditConn(),
        deps=default_deps(),
        settings=_SETTINGS,
        model=_FakeModel(result),
    )


def _alert(alert_id: str, *, dob: date | None = date(1969, 4, 12)) -> dict:
    return ScreeningAlert(
        alert_id=alert_id,
        subject_id="CUST-1001",
        screened_name="Viktor Petrov",
        date_of_birth=dob,
        nationality="RU",
        hits=[
            WatchlistHit(
                list_name="OFAC SDN", entry_id="SDN-12345", matched_name="Viktor Petrov", score=0.95
            )
        ],
    ).model_dump(mode="json")


def test_clear_alert_disposes_as_agent():
    graph = _graph(
        EvaluationResult(recommended=DispositionType.CLEAR, confidence=0.95, rationale="no match")
    )
    cfg = {"configurable": {"thread_id": "clear-1"}}
    graph.invoke({"alert": _alert("clear-1")}, cfg)

    snap = graph.get_state(cfg)
    assert snap.next == ()
    assert snap.values["disposition"]["disposition"] == "clear"
    assert snap.values["disposition"]["decided_by"] == "agent"


def test_true_match_parks_at_escalate_interrupt():
    graph = _graph(
        EvaluationResult(
            recommended=DispositionType.TRUE_MATCH,
            confidence=0.93,
            rationale="name+dob+nationality match",
            matched_entities=[
                MatchedEntity(
                    list_name="OFAC SDN", entry_id="SDN-12345", primary_name="Viktor Petrov"
                )
            ],
        )
    )
    cfg = {"configurable": {"thread_id": "match-1"}}
    graph.invoke({"alert": _alert("match-1")}, cfg)

    snap = graph.get_state(cfg)
    assert snap.next == ("escalate",)
    assert snap.interrupts
    payload = snap.interrupts[0].value
    assert payload["alert_id"] == "match-1"
    assert payload["agent_recommendation"] == "true_match"


def test_resume_after_rebuild_closes_as_analyst():
    result = EvaluationResult(
        recommended=DispositionType.TRUE_MATCH,
        confidence=0.93,
        rationale="match",
        matched_entities=[
            MatchedEntity(list_name="OFAC SDN", entry_id="SDN-12345", primary_name="Viktor Petrov")
        ],
    )
    saver = InMemorySaver()
    cfg = {"configurable": {"thread_id": "match-2"}}

    _graph(result, saver).invoke({"alert": _alert("match-2")}, cfg)

    # Simulate a fresh process: brand-new graph object over the same checkpointer store.
    rebuilt = _graph(result, saver)
    rebuilt.invoke(
        Command(
            resume={
                "resolution": "true_match",
                "analyst_id": "AN-1",
                "rationale": "confirmed identity",
            }
        ),
        cfg,
    )

    snap = rebuilt.get_state(cfg)
    assert snap.next == ()
    d = snap.values["disposition"]
    assert d["disposition"] == "true_match"
    assert d["decided_by"] == "analyst"
    assert d["analyst_id"] == "AN-1"
    assert len(d["matched_entities"]) == 1


def test_insufficient_data_disposes_via_rule_overlay():
    # Model result is irrelevant: missing DOB makes enrich flag insufficient_data,
    # and evaluate's rule overlay short-circuits before the model is consulted.
    graph = _graph(
        EvaluationResult(recommended=DispositionType.CLEAR, confidence=0.99, rationale="unused")
    )
    cfg = {"configurable": {"thread_id": "insuf-1"}}
    graph.invoke({"alert": _alert("insuf-1", dob=None)}, cfg)

    snap = graph.get_state(cfg)
    assert snap.next == ()
    assert snap.values["disposition"]["disposition"] == "insufficient_data"
    assert snap.values["disposition"]["decided_by"] == "agent"


def test_get_state_history_records_every_transition():
    graph = _graph(
        EvaluationResult(recommended=DispositionType.CLEAR, confidence=0.95, rationale="no match")
    )
    cfg = {"configurable": {"thread_id": "hist-1"}}
    graph.invoke({"alert": _alert("hist-1")}, cfg)

    steps = [c.metadata.get("step") for c in graph.get_state_history(cfg)]
    # one checkpoint per super-step: -1 (input) .. through dispose
    assert len(steps) >= 5
