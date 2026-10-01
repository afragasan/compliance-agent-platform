"""Retrieval evaluation: precision@k / recall@k over labeled query->chunk pairs.

Labels are at chunk granularity, not document/file granularity: the regulatory
corpus is only a handful of source files (one per program), so document-level
precision/recall would be a near-trivial signal. Each corpus chunk is already an
independently retrievable unit (one numbered clause), so chunk-level labels give a
real signal across the full set of chunks.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from compliance_agent_platform.rag.base import VectorStore


class RetrievalLabel(BaseModel):
    query: str
    relevant_chunk_ids: list[str] = Field(min_length=1)


class RetrievalEvalRow(BaseModel):
    query: str
    precision: float
    recall: float
    retrieved_chunk_ids: list[str]
    relevant_chunk_ids: list[str]


class RetrievalEvalReport(BaseModel):
    k: int
    n_queries: int
    mean_precision_at_k: float
    mean_recall_at_k: float
    rows: list[RetrievalEvalRow] = Field(default_factory=list)


def load_labels(path: str) -> list[RetrievalLabel]:
    import json

    data = json.loads(Path(path).read_text())
    return [RetrievalLabel.model_validate(item) for item in data]


def evaluate_retrieval(
    store: VectorStore, embedder: object, labels: list[RetrievalLabel], k: int = 5
) -> RetrievalEvalReport:
    rows: list[RetrievalEvalRow] = []
    for label in labels:
        embedding = embedder.embed_query(label.query)
        retrieved = store.search(embedding, k=k)
        retrieved_ids = {chunk.chunk_id for chunk in retrieved}
        relevant_ids = set(label.relevant_chunk_ids)
        hits = retrieved_ids & relevant_ids

        precision = len(hits) / len(retrieved_ids) if retrieved_ids else 0.0
        recall = len(hits) / len(relevant_ids) if relevant_ids else 0.0
        rows.append(
            RetrievalEvalRow(
                query=label.query,
                precision=precision,
                recall=recall,
                retrieved_chunk_ids=sorted(retrieved_ids),
                relevant_chunk_ids=sorted(relevant_ids),
            )
        )

    n = len(rows)
    mean_precision = sum(r.precision for r in rows) / n if n else 0.0
    mean_recall = sum(r.recall for r in rows) / n if n else 0.0
    return RetrievalEvalReport(
        k=k,
        n_queries=n,
        mean_precision_at_k=mean_precision,
        mean_recall_at_k=mean_recall,
        rows=rows,
    )
