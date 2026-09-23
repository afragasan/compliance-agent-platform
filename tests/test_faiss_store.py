"""FaissVectorStore: upsert/search ordering, idempotent re-upsert, save/load round-trip."""

from __future__ import annotations

from compliance_agent_platform.rag.faiss_store import FaissVectorStore
from compliance_agent_platform.schemas.retrieval import DocumentChunk, DocumentMetadata

_DIM = 4


def _chunk(chunk_id: str) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        document_id="test-corpus",
        chunk_index=0,
        text=f"body of {chunk_id}",
        metadata=DocumentMetadata(document_id="test-corpus", title=chunk_id, program="TEST"),
    )


def test_search_returns_nearest_neighbor_first():
    store = FaissVectorStore(_DIM)
    store.upsert(
        [_chunk("A"), _chunk("B"), _chunk("C")],
        [[1, 0, 0, 0], [0, 1, 0, 0], [0.9, 0.1, 0, 0]],
    )

    results = store.search([1, 0, 0, 0], k=3)

    assert [r.chunk_id for r in results] == ["A", "C", "B"]
    assert results[0].score > results[1].score > results[2].score


def test_search_caps_k_to_available_chunks():
    store = FaissVectorStore(_DIM)
    store.upsert([_chunk("A")], [[1, 0, 0, 0]])

    results = store.search([1, 0, 0, 0], k=5)

    assert len(results) == 1


def test_search_on_empty_store_returns_no_results():
    store = FaissVectorStore(_DIM)
    assert store.search([1, 0, 0, 0], k=5) == []


def test_upsert_is_idempotent_by_chunk_id():
    store = FaissVectorStore(_DIM)
    store.upsert([_chunk("A")], [[1, 0, 0, 0]])
    store.upsert([_chunk("A")], [[0, 1, 0, 0]])  # re-ingest with a different vector

    results = store.search([0, 1, 0, 0], k=5)

    assert len(results) == 1
    assert results[0].score > 0.9  # matches the latest vector, not the first


def test_save_and_load_round_trips(tmp_path):
    store = FaissVectorStore(_DIM)
    store.upsert([_chunk("A"), _chunk("B")], [[1, 0, 0, 0], [0, 1, 0, 0]])
    path = tmp_path / "index.json"
    store.save(str(path))

    loaded = FaissVectorStore.load(str(path), _DIM)
    results = loaded.search([1, 0, 0, 0], k=2)

    assert [r.chunk_id for r in results] == ["A", "B"]


def test_load_missing_file_returns_empty_store(tmp_path):
    loaded = FaissVectorStore.load(str(tmp_path / "does-not-exist.json"), _DIM)
    assert loaded.search([1, 0, 0, 0], k=5) == []
