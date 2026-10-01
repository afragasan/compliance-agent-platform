"""Regulatory-corpus chunks retrieved to ground ``evaluate``'s decision."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentMetadata(BaseModel):
    """Provenance carried by every retrieved chunk."""

    document_id: str = Field(description="e.g. 'ofac-sanctions-corpus' (one per source file).")
    title: str = Field(description="The chunk's own topic, e.g. 'False Hit Resolution'.")
    program: str = Field(description="e.g. 'OFAC', 'NACHA', 'MTRA'.")
    section: str | None = Field(default=None, description="Regulatory reference, e.g. citation.")
    citation: str | None = Field(default=None, description="Human-readable citation string.")
    compliance_level: str | None = None


class DocumentChunk(BaseModel):
    """One retrievable unit of the regulatory corpus."""

    chunk_id: str
    document_id: str
    chunk_index: int
    text: str = Field(min_length=1)
    metadata: DocumentMetadata


class RetrievedChunk(DocumentChunk):
    """A :class:`DocumentChunk` ranked by a vector search."""

    score: float


class RetrievalBundle(BaseModel):
    """What the ``retrieve`` node writes to ``ScreeningState["retrieval"]``."""

    query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)
