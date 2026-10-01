"""Assemble the compiled ``sanctions_screening`` graph."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from compliance_agent_platform.adapters.base import ScreeningDeps
from compliance_agent_platform.compliance_mw import wrap_node
from compliance_agent_platform.config import Settings, get_settings
from compliance_agent_platform.embeddings import get_embedding_model
from compliance_agent_platform.graph.nodes import (
    make_dispose,
    make_enrich,
    make_escalate,
    make_evaluate,
    make_intake,
    make_retrieve,
)
from compliance_agent_platform.graph.routing import make_route_after_evaluate
from compliance_agent_platform.llm import get_chat_model
from compliance_agent_platform.rag.base import VectorStore
from compliance_agent_platform.rag.factory import get_vector_store
from compliance_agent_platform.schemas.state import ScreeningState


@dataclass
class NodeContext:
    deps: ScreeningDeps
    audit_conn: psycopg.Connection
    settings: Settings
    model: object  # LangChain chat model (BaseChatModel)
    embedder: object  # LangChain embeddings model
    vector_store: VectorStore


def build_graph(
    *,
    checkpointer: BaseCheckpointSaver,
    audit_conn: psycopg.Connection,
    deps: ScreeningDeps,
    settings: Settings | None = None,
    model: object | None = None,
    embedder: object | None = None,
    vector_store: VectorStore | None = None,
):
    """Return the compiled graph. ``model``/``embedder``/``vector_store`` are
    injectable for tests."""
    settings = settings or get_settings()
    ctx = NodeContext(
        deps=deps,
        audit_conn=audit_conn,
        settings=settings,
        model=model or get_chat_model(),
        embedder=embedder or get_embedding_model(),
        vector_store=vector_store or get_vector_store(settings, conn=audit_conn),
    )

    builder = StateGraph(ScreeningState)
    builder.add_node("intake", wrap_node("intake", make_intake(ctx), ctx))
    builder.add_node("enrich", wrap_node("enrich", make_enrich(ctx), ctx))
    builder.add_node("retrieve", wrap_node("retrieve", make_retrieve(ctx), ctx))
    builder.add_node("evaluate", wrap_node("evaluate", make_evaluate(ctx), ctx))
    builder.add_node("escalate", wrap_node("escalate", make_escalate(ctx), ctx))
    builder.add_node("dispose", wrap_node("dispose", make_dispose(ctx), ctx))

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "enrich")
    builder.add_edge("enrich", "retrieve")
    builder.add_edge("retrieve", "evaluate")
    builder.add_conditional_edges(
        "evaluate",
        make_route_after_evaluate(settings),
        {"escalate": "escalate", "dispose": "dispose"},
    )
    builder.add_edge("escalate", "dispose")
    builder.add_edge("dispose", END)

    return builder.compile(checkpointer=checkpointer)
