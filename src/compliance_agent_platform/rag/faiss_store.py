"""Local, no-cloud-cost vector store for iteration: FAISS over normalized vectors.

Persistence is a single JSON file (chunk payload + its vector), not FAISS's own
binary index format — at this corpus's scale (a few hundred regulatory-guidance
chunks) that costs nothing in performance and keeps the on-disk format readable and
diff-able. The FAISS index itself is rebuilt in memory, lazily, from that JSON.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import faiss
import numpy as np

from compliance_agent_platform.schemas.retrieval import DocumentChunk, RetrievedChunk


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


class FaissVectorStore:
    """Exact (flat) cosine search — fine at this scale; revisit only if the corpus
    grows by orders of magnitude."""

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension
        self._chunks: dict[str, DocumentChunk] = {}
        self._vectors: dict[str, list[float]] = {}
        self._index: faiss.Index | None = None
        self._ids: list[str] = []

    def upsert(self, chunks: list[DocumentChunk], embeddings: list[list[float]]) -> None:
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            self._chunks[chunk.chunk_id] = chunk
            self._vectors[chunk.chunk_id] = _normalize(embedding)
        self._index = None  # invalidate; rebuilt lazily on next search

    def _ensure_index(self) -> None:
        if self._index is not None:
            return
        self._ids = list(self._chunks.keys())
        index = faiss.IndexFlatIP(self._dimension)
        if self._ids:
            matrix = np.array([self._vectors[i] for i in self._ids], dtype="float32")
            index.add(matrix)
        self._index = index

    def search(self, embedding: list[float], k: int = 5) -> list[RetrievedChunk]:
        self._ensure_index()
        if not self._ids:
            return []
        query = np.array([_normalize(embedding)], dtype="float32")
        k = min(k, len(self._ids))
        scores, positions = self._index.search(query, k)
        results: list[RetrievedChunk] = []
        for score, position in zip(scores[0], positions[0], strict=True):
            if position == -1:
                continue
            chunk = self._chunks[self._ids[position]]
            results.append(RetrievedChunk(**chunk.model_dump(), score=float(score)))
        return results

    def save(self, path: str) -> None:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            chunk_id: {
                "chunk": self._chunks[chunk_id].model_dump(mode="json"),
                "vector": self._vectors[chunk_id],
            }
            for chunk_id in self._chunks
        }
        out.write_text(json.dumps(payload))

    @classmethod
    def load(cls, path: str, dimension: int) -> FaissVectorStore:
        store = cls(dimension)
        file = Path(path)
        if not file.exists():
            return store
        payload = json.loads(file.read_text())
        for chunk_id, entry in payload.items():
            store._chunks[chunk_id] = DocumentChunk.model_validate(entry["chunk"])
            store._vectors[chunk_id] = entry["vector"]
        return store
