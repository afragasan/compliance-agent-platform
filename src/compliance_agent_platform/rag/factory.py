"""Select the configured vector-store backend."""

from __future__ import annotations

from psycopg import Connection

from compliance_agent_platform.config import Settings
from compliance_agent_platform.rag.base import VectorStore
from compliance_agent_platform.rag.faiss_store import FaissVectorStore
from compliance_agent_platform.rag.pgvector_store import PgVectorStore


def get_vector_store(settings: Settings, *, conn: Connection | None = None) -> VectorStore:
    if settings.vector_backend == "faiss":
        return FaissVectorStore.load(settings.faiss_index_path, settings.embedding_dimension)
    if settings.vector_backend == "pgvector":
        if conn is None:
            raise ValueError("pgvector backend requires a psycopg connection")
        return PgVectorStore(conn)
    raise ValueError(f"unknown vector_backend: {settings.vector_backend!r}")
