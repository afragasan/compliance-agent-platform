"""Assemble the compiled ``sanctions_screening`` graph."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from compliance_agent_platform.adapters.base import ScreeningDeps
from compliance_agent_platform.config import Settings, get_settings
from compliance_agent_platform.graph.nodes import (
    make_dispose,
    make_enrich,
    make_escalate,
    make_evaluate,
    make_intake,
)
from compliance_agent_platform.graph.routing import make_route_after_evaluate
from compliance_agent_platform.llm import get_chat_model
from compliance_agent_platform.schemas.state import ScreeningState


@dataclass
class NodeContext:
    deps: ScreeningDeps
    audit_conn: psycopg.Connection
    settings: Settings
    model: object  # LangChain chat model (BaseChatModel)


def build_graph(
    *,
    checkpointer: BaseCheckpointSaver,
    audit_conn: psycopg.Connection,
    deps: ScreeningDeps,
    settings: Settings | None = None,
    model: object | None = None,
):
    """Return the compiled graph. ``model`` is injectable for tests."""
    settings = settings or get_settings()
    ctx = NodeContext(
        deps=deps,
        audit_conn=audit_conn,
        settings=settings,
        model=model or get_chat_model(),
    )

    builder = StateGraph(ScreeningState)
    builder.add_node("intake", make_intake(ctx))
    builder.add_node("enrich", make_enrich(ctx))
    builder.add_node("evaluate", make_evaluate(ctx))
    builder.add_node("escalate", make_escalate(ctx))
    builder.add_node("dispose", make_dispose(ctx))

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "enrich")
    builder.add_edge("enrich", "evaluate")
    builder.add_conditional_edges(
        "evaluate",
        make_route_after_evaluate(settings),
        {"escalate": "escalate", "dispose": "dispose"},
    )
    builder.add_edge("escalate", "dispose")
    builder.add_edge("dispose", END)

    return builder.compile(checkpointer=checkpointer)
