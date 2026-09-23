"""PgVectorStore against a real Postgres (requires the `vector` extension).

Uses synthetic throwaway vectors - no real regulatory text is needed to exercise the
mechanics (schema creation, idempotent upsert, cosine-ordering search).
"""

from __future__ import annotations

import psycopg
import pytest

from compliance_agent_platform.rag.pgvector_store import PgVectorStore
from compliance_agent_platform.schemas.retrieval import DocumentChunk, DocumentMetadata

pytestmark = pytest.mark.integration

# document_chunks.embedding is a fixed VECTOR(1024) column (matches
# Settings.embedding_dimension's default, since dimension isn't settings-driven at
# the DB level - see rag/schema.sql) - synthetic vectors here just need the right
# width, not real embedding structure.
_DIM = 1024


def _basis_vector(index: int, *, weight: float = 1.0) -> list[float]:
    vector = [0.0] * _DIM
    vector[index] = weight
    return vector


def _chunk(chunk_id: str) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        document_id="test-corpus",
        chunk_index=0,
        text=f"body of {chunk_id}",
        metadata=DocumentMetadata(document_id="test-corpus", title=chunk_id, program="TEST"),
    )


@pytest.fixture
def store(pg_dsn: str):
    with psycopg.connect(pg_dsn, autocommit=False) as conn:
        vector_store = PgVectorStore(conn)  # creates the schema if missing
        conn.execute("TRUNCATE document_chunks")
        conn.commit()
        yield vector_store
        conn.execute("TRUNCATE document_chunks")
        conn.commit()


def test_search_orders_by_cosine_distance(store):
    a = _basis_vector(0)
    b = _basis_vector(1)
    c = _basis_vector(0, weight=0.9)
    c[1] = 0.1  # close to A, not identical
    store.upsert([_chunk("A"), _chunk("B"), _chunk("C")], [a, b, c])

    results = store.search(a, k=3)

    assert [r.chunk_id for r in results] == ["A", "C", "B"]
    assert results[0].metadata.program == "TEST"


def test_upsert_is_idempotent_by_chunk_id(store):
    store.upsert([_chunk("A")], [_basis_vector(0)])
    store.upsert([_chunk("A")], [_basis_vector(1)])

    results = store.search(_basis_vector(1), k=5)

    assert len(results) == 1
    assert results[0].score > 0.9
