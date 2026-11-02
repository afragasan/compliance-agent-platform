"""Backend-aware structured output + the serving model_id reaching the Disposition."""

from __future__ import annotations

from datetime import date

import pytest
from fakes import FakeAuditConn, FakeEmbedder, FakeVectorStore
from langgraph.checkpoint.memory import InMemorySaver

from compliance_agent_platform.adapters.mock import default_deps
from compliance_agent_platform.config import Settings
from compliance_agent_platform.graph import builder, nodes
from compliance_agent_platform.guardrails.errors import GuardrailViolationError
from compliance_agent_platform.llm import ResolvedBackend, json_mode_instruction, structured_output
from compliance_agent_platform.schemas.alert import ScreeningAlert, WatchlistHit
from compliance_agent_platform.schemas.disposition import DispositionType
from compliance_agent_platform.schemas.evaluation import EvaluationResult


class _Recorder:
    def __init__(self, result=None, fail=False):
        self.kwargs = None
        self.messages = None
        self._result, self._fail = result, fail

    def with_structured_output(self, schema, **kwargs):
        self.kwargs = kwargs
        return self

    def invoke(self, messages, **kwargs):
        self.messages = messages
        if self._fail:
            raise RuntimeError("400 response_format json_schema unsupported")
        return {"raw": None, "parsed": self._result, "parsing_error": None}


def test_nim_uses_json_mode_and_bedrock_uses_tool_use():
    nim, bedrock = _Recorder(), _Recorder()
    structured_output(nim, "nim", EvaluationResult)
    structured_output(bedrock, "bedrock", EvaluationResult)
    assert nim.kwargs == {"method": "json_mode", "include_raw": True}
    assert bedrock.kwargs == {"include_raw": True}  # default method (function calling)


def test_json_mode_instruction_only_for_nim_and_carries_schema():
    assert json_mode_instruction("bedrock", EvaluationResult) == ""
    text = json_mode_instruction("nim", EvaluationResult)
    assert "JSON" in text and "recommended" in text and "confidence" in text


def test_fallback_disposition_carries_serving_model_id(monkeypatch):
    monkeypatch.setenv("CAP_FAKE_GUARDRAILS", "1")
    good = EvaluationResult(recommended=DispositionType.CLEAR, confidence=0.95, rationale="ok")
    primary = _Recorder(fail=True)
    fallback = _Recorder(result=good)
    monkeypatch.setattr(
        builder,
        "resolve_chat_model",
        lambda queue: (
            ResolvedBackend(primary, "nim", "meta/llama-3.1-8b-instruct"),
            ResolvedBackend(fallback, "bedrock", "claude-fallback"),
        ),
    )
    graph = builder.build_graph(
        checkpointer=InMemorySaver(),
        audit_conn=FakeAuditConn(),
        deps=default_deps(),
        settings=Settings(queue="sanctions_screening", prompt_version="t"),
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    alert = ScreeningAlert(
        alert_id="fb-1",
        subject_id="CUST-1001",
        screened_name="Viktor Petrov",
        date_of_birth=date(1969, 4, 12),
        nationality="RU",
        hits=[
            WatchlistHit(list_name="OFAC SDN", entry_id="SDN-12345", matched_name="V P", score=0.9)
        ],
    ).model_dump(mode="json")
    cfg = {"configurable": {"thread_id": "fb-1"}}
    graph.invoke({"alert": alert}, cfg)

    state = graph.get_state(cfg).values
    assert state["evaluation_model"] == {"backend": "bedrock", "model_id": "claude-fallback"}
    assert state["disposition"]["model_id"] == "claude-fallback"
    # NIM got the schema-bearing system prompt; Bedrock did not.
    assert "JSON Schema" in primary.messages[0][1]
    assert "JSON Schema" not in fallback.messages[0][1]


def _fallback_graph(monkeypatch, primary, fallback):
    monkeypatch.setattr(
        builder,
        "resolve_chat_model",
        lambda queue: (
            ResolvedBackend(primary, "nim", "meta/llama-3.1-8b-instruct"),
            ResolvedBackend(fallback, "bedrock", "claude-fallback"),
        ),
    )
    return builder.build_graph(
        checkpointer=InMemorySaver(),
        audit_conn=FakeAuditConn(),
        deps=default_deps(),
        settings=Settings(queue="aml_analysis", prompt_version="t"),
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )


def _alert_dict(alert_id: str) -> dict:
    return ScreeningAlert(
        alert_id=alert_id,
        subject_id="CUST-1001",
        screened_name="Viktor Petrov",
        date_of_birth=date(1969, 4, 12),
        nationality="RU",
        hits=[
            WatchlistHit(list_name="OFAC SDN", entry_id="SDN-12345", matched_name="V P", score=0.9)
        ],
    ).model_dump(mode="json")


def test_primary_outage_in_input_guardrail_falls_back(monkeypatch):
    good = EvaluationResult(recommended=DispositionType.CLEAR, confidence=0.95, rationale="ok")
    primary, fallback = _Recorder(result=good), _Recorder(result=good)
    checked = []

    def check_input(model, text):
        checked.append(model)
        if model is primary:
            raise ConnectionError("NIM unreachable")

    monkeypatch.setattr(nodes.guardrails_service, "check_input", check_input)
    monkeypatch.setattr(nodes.guardrails_service, "check_output", lambda m, t: None)
    graph = _fallback_graph(monkeypatch, primary, fallback)
    cfg = {"configurable": {"thread_id": "gd-1"}}
    graph.invoke({"alert": _alert_dict("gd-1")}, cfg)

    values = graph.get_state(cfg).values
    assert checked == [primary, fallback]  # re-checked on the fallback model
    assert primary.messages is None  # never reached the primary invoke
    assert values["evaluation_model"] == {"backend": "bedrock", "model_id": "claude-fallback"}
    assert values["disposition"]["model_id"] == "claude-fallback"


def test_guardrail_violation_does_not_fall_back(monkeypatch):
    good = EvaluationResult(recommended=DispositionType.CLEAR, confidence=0.95, rationale="ok")
    primary, fallback = _Recorder(result=good), _Recorder(result=good)

    def check_input(model, text):
        raise GuardrailViolationError(rail="input", reason="pii")

    monkeypatch.setattr(nodes.guardrails_service, "check_input", check_input)
    graph = _fallback_graph(monkeypatch, primary, fallback)
    with pytest.raises(GuardrailViolationError):
        graph.invoke({"alert": _alert_dict("gd-2")}, {"configurable": {"thread_id": "gd-2"}})
    assert fallback.messages is None
