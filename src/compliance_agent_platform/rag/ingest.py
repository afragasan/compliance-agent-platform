"""Load the regulatory corpus and build a vector store from it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from compliance_agent_platform.config import Settings
from compliance_agent_platform.embeddings import get_embedding_model
from compliance_agent_platform.rag.base import VectorStore
from compliance_agent_platform.rag.corpus import parse_corpus_file
from compliance_agent_platform.schemas.retrieval import DocumentChunk


def load_corpus(corpus_dir: Path) -> list[DocumentChunk]:
    """Parse every ``*.txt`` file in ``corpus_dir`` into its chunks."""
    chunks: list[DocumentChunk] = []
    for path in sorted(Path(corpus_dir).glob("*.txt")):
        chunks.extend(parse_corpus_file(path))
    return chunks


@dataclass
class IngestSummary:
    documents: int
    chunks: int
    backend: str


def build_index(
    settings: Settings, store: VectorStore, embedder: object | None = None
) -> IngestSummary:
    """Chunk + embed the configured corpus dir and upsert it into ``store``."""
    embedder = embedder or get_embedding_model()
    chunks = load_corpus(settings.regulatory_corpus_dir)
    if not chunks:
        return IngestSummary(documents=0, chunks=0, backend=settings.vector_backend)

    embeddings = embedder.embed_documents([c.text for c in chunks])
    store.upsert(chunks, embeddings)
    documents = len({c.document_id for c in chunks})
    return IngestSummary(documents=documents, chunks=len(chunks), backend=settings.vector_backend)
