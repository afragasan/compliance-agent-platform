"""AgentCore-shaped entrypoint for the sanctions-screening agent.

Contract (matches ``BedrockAgentCoreApp.entrypoint``):

    invoke(payload: dict, context) -> dict

payload:
    {"alert": {<ScreeningAlert>}}                       # start a new screening
    {"thread_id": "...", "resume": {<AnalystDecision>}} # close an escalated alert

context:
    any object; ``context.session_id`` (when present) is used as the LangGraph
    thread id, which is what AgentCore Runtime supplies per session.

response:
    {"status": "disposed",  "thread_id": ..., "disposition": {<Disposition>}}
    {"status": "escalated", "thread_id": ..., "interrupt": {<payload for the analyst>}}
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command

from compliance_agent_platform.adapters.mock import default_deps
from compliance_agent_platform.checkpoint.postgres import open_checkpointer
from compliance_agent_platform.config import get_settings
from compliance_agent_platform.graph.builder import build_graph
from compliance_agent_platform.schemas.alert import ScreeningAlert
from compliance_agent_platform.schemas.evaluation import AnalystDecision


def _thread_id(payload: dict, context: Any, alert: ScreeningAlert | None) -> str:
    for candidate in (
        payload.get("thread_id"),
        getattr(context, "session_id", None),
        alert.alert_id if alert else None,
    ):
        if candidate:
            return str(candidate)
    raise ValueError("cannot determine thread_id: provide payload['thread_id'], a session id, or an alert")


def invoke(payload: dict, context: Any = None) -> dict:
    settings = get_settings()
    alert = ScreeningAlert.model_validate(payload["alert"]) if "alert" in payload else None
    resume = payload.get("resume")
    thread_id = _thread_id(payload, context, alert)
    config = {"configurable": {"thread_id": thread_id}}

    with open_checkpointer(settings) as persistence:
        graph = build_graph(
            checkpointer=persistence.checkpointer,
            audit_conn=persistence.audit_conn,
            deps=default_deps(),
            settings=settings,
        )

        if resume is not None:
            decision = AnalystDecision.model_validate(resume)
            graph.invoke(Command(resume=decision.model_dump(mode="json")), config)
        elif alert is not None:
            graph.invoke({"alert": alert.model_dump(mode="json")}, config)
        else:
            raise ValueError("payload must contain 'alert' (new run) or 'resume' (close escalation)")

        snapshot = graph.get_state(config)

    if snapshot.interrupts:
        return {
            "status": "escalated",
            "thread_id": thread_id,
            "interrupt": snapshot.interrupts[0].value,
        }

    return {
        "status": "disposed",
        "thread_id": thread_id,
        "disposition": snapshot.values["disposition"],
    }
