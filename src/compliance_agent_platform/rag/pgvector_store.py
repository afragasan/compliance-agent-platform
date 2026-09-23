"""Deployed vector store: pgvector on RDS PostgreSQL."""

from __future__ import annotations

from importlib import resources

from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.types.json import Jsonb

from compliance_agent_platform.schemas.retrieval import (
    DocumentChunk,
    DocumentMetadata,
    RetrievedChunk,
)


def ensure_rag_schema(conn: Connection) -> None:
    """Idempotently create the ``document_chunks`` table / extension / index."""
    sql = resources.files("compliance_agent_platform.rag").joinpath("schema.sql").read_text()
    conn.execute(sql)
    conn.commit()


class PgVectorStore:
    def __init__(self, conn: Connection) -> None:
        ensure_rag_schema(conn)
        register_vector(conn)
        self._conn = conn

    def upsert(self, chunks: list[DocumentChunk], embeddings: list[list[float]]) -> None:
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            self._conn.execute(
                """
                INSERT INTO document_chunks (
                    chunk_id, document_id, chunk_index, text, metadata, embedding
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    chunk_index = EXCLUDED.chunk_index,
                    text = EXCLUDED.text,
                    metadata = EXCLUDED.metadata,
                    embedding = EXCLUDED.embedding
                """,
                (
                    chunk.chunk_id,
                    chunk.document_id,
                    chunk.chunk_index,
                    chunk.text,
                    Jsonb(chunk.metadata.model_dump(mode="json")),
                    Vector(embedding),
                ),
            )
        self._conn.commit()

    def search(self, embedding: list[float], k: int = 5) -> list[RetrievedChunk]:
        rows = self._conn.execute(
            """
            SELECT chunk_id, document_id, chunk_index, text, metadata,
                   1 - (embedding <=> %s) AS score
            FROM document_chunks
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (Vector(embedding), Vector(embedding), k),
        ).fetchall()
        return [
            RetrievedChunk(
                chunk_id=row[0],
                document_id=row[1],
                chunk_index=row[2],
                text=row[3],
                metadata=DocumentMetadata.model_validate(row[4]),
                score=float(row[5]),
            )
            for row in rows
        ]
