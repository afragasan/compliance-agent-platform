"""Vector-store Protocol, mirroring ``adapters/base.py``'s swappable-dependency pattern."""

from __future__ import annotations

from typing import Protocol

from compliance_agent_platform.schemas.retrieval import DocumentChunk, RetrievedChunk


class VectorStore(Protocol):
    def search(self, embedding: list[float], k: int = 5) -> list[RetrievedChunk]: ...

    def upsert(self, chunks: list[DocumentChunk], embeddings: list[list[float]]) -> None: ...
