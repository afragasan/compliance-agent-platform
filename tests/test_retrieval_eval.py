"""``evaluate_retrieval``'s precision@k/recall@k math on a fully controlled fixture.

This validates the metric plumbing, not real retrieval quality - the store below
returns a fixed, known ranking regardless of the query, so expected precision/recall
values are computed by hand.
"""

from __future__ import annotations

from compliance_agent_platform.rag.eval import RetrievalLabel, evaluate_retrieval
from compliance_agent_platform.schemas.retrieval import DocumentMetadata, RetrievedChunk


class _FixedStore:
    """Always returns the same 5 ranked chunks: 3 from doc A, 2 from doc B."""

    def __init__(self) -> None:
        self._chunks = [
            RetrievedChunk(
                chunk_id=cid,
                document_id="A" if cid.startswith("A") else "B",
                chunk_index=0,
                text="x",
                metadata=DocumentMetadata(document_id="d", title="t", program="TEST"),
                score=1.0 - i * 0.1,
            )
            for i, cid in enumerate(["A-1", "A-2", "A-3", "B-1", "B-2"])
        ]

    def search(self, embedding, k=5):
        return self._chunks[:k]


class _FixedEmbedder:
    def embed_query(self, text: str) -> list[float]:
        return [0.0]


def test_precision_and_recall_at_k_computed_correctly():
    # relevant = {A-1, B-1, A-4 (never retrieved)}; retrieved top-5 = A-1,A-2,A-3,B-1,B-2
    # hits = {A-1, B-1} -> precision@5 = 2/5 = 0.4, recall@5 = 2/3
    labels = [RetrievalLabel(query="q1", relevant_chunk_ids=["A-1", "B-1", "A-4"])]

    report = evaluate_retrieval(_FixedStore(), _FixedEmbedder(), labels, k=5)

    assert report.n_queries == 1
    assert report.k == 5
    row = report.rows[0]
    assert row.precision == 0.4
    assert row.recall == 2 / 3
    assert report.mean_precision_at_k == 0.4
    assert report.mean_recall_at_k == 2 / 3


def test_perfect_recall_and_partial_precision_when_all_relevant_are_retrieved():
    # relevant = {A-1, A-2} - both are in the top-5 -> recall@5 = 1.0, precision@5 = 2/5
    labels = [RetrievalLabel(query="q1", relevant_chunk_ids=["A-1", "A-2"])]

    report = evaluate_retrieval(_FixedStore(), _FixedEmbedder(), labels, k=5)

    assert report.rows[0].recall == 1.0
    assert report.rows[0].precision == 0.4


def test_mean_is_averaged_across_multiple_queries():
    labels = [
        RetrievalLabel(query="q1", relevant_chunk_ids=["A-1"]),  # precision=0.2, recall=1.0
        RetrievalLabel(query="q2", relevant_chunk_ids=["ZZZ"]),  # precision=0.0, recall=0.0
    ]

    report = evaluate_retrieval(_FixedStore(), _FixedEmbedder(), labels, k=5)

    assert report.mean_precision_at_k == 0.1
    assert report.mean_recall_at_k == 0.5


def test_k_caps_how_many_retrieved_chunks_count():
    # top-2 only = A-1, A-2; relevant={A-1,B-1} -> hits={A-1} -> precision@2=0.5, recall@2=0.5
    labels = [RetrievalLabel(query="q1", relevant_chunk_ids=["A-1", "B-1"])]

    report = evaluate_retrieval(_FixedStore(), _FixedEmbedder(), labels, k=2)

    assert report.rows[0].precision == 0.5
    assert report.rows[0].recall == 0.5
