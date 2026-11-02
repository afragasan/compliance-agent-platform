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
from compliance_agent_platform.llm import resolve_chat_model
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
    # Which backend/model actually serves `model` - resolved once at build time from
    # the routing policy (llm.resolve_chat_model), so audit rows can name the truth
    # instead of assuming Bedrock. "test"/"test-model-id" when `model=` is injected
    # directly (tests bypass routing entirely).
    model_backend: str = "test"
    model_id: str = "test-model-id"
    model_fallback: object | None = None
    fallback_backend: str | None = None
    fallback_model_id: str | None = None


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

    if model is not None:
        resolved_model, backend, model_id = model, "test", "test-model-id"
        fallback_model = fallback_backend = fallback_model_id = None
    else:
        primary, fallback = resolve_chat_model(settings.queue)
        resolved_model, backend, model_id = primary.model, primary.backend, primary.model_id
        fallback_model = fallback.model if fallback else None
        fallback_backend = fallback.backend if fallback else None
        fallback_model_id = fallback.model_id if fallback else None

    ctx = NodeContext(
        deps=deps,
        audit_conn=audit_conn,
        settings=settings,
        model=resolved_model,
        embedder=embedder or get_embedding_model(),
        vector_store=vector_store or get_vector_store(settings, conn=audit_conn),
        model_backend=backend,
        model_id=model_id,
        model_fallback=fallback_model,
        fallback_backend=fallback_backend,
        fallback_model_id=fallback_model_id,
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
