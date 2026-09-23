"""Retrieval schemas round-trip and stay backward compatible with existing callers."""

from __future__ import annotations

from compliance_agent_platform.schemas.disposition import EvidenceItem
from compliance_agent_platform.schemas.retrieval import (
    DocumentChunk,
    DocumentMetadata,
    RetrievalBundle,
    RetrievedChunk,
)


def _metadata(**overrides) -> DocumentMetadata:
    base = dict(document_id="test-corpus", title="Test Topic", program="TEST")
    base.update(overrides)
    return DocumentMetadata(**base)


def test_document_chunk_round_trip():
    chunk = DocumentChunk(
        chunk_id="TEST-001",
        document_id="test-corpus",
        chunk_index=0,
        text="body",
        metadata=_metadata(),
    )
    restored = DocumentChunk.model_validate(chunk.model_dump(mode="json"))
    assert restored == chunk


def test_retrieved_chunk_adds_score_over_document_chunk():
    chunk = RetrievedChunk(
        chunk_id="TEST-001",
        document_id="test-corpus",
        chunk_index=0,
        text="body",
        metadata=_metadata(),
        score=0.87,
    )
    assert chunk.score == 0.87


def test_retrieval_bundle_defaults_to_empty_chunks():
    bundle = RetrievalBundle(query="q")
    assert bundle.chunks == []


def test_evidence_item_still_constructs_without_new_fields():
    # Pre-Week-3 call sites (e.g. graph/nodes.py::_fallback_evidence) never set
    # document_id/chunk_id - must keep working unchanged.
    item = EvidenceItem(source="OFAC SDN", detail="name_similarity=1.00")
    assert item.document_id is None
    assert item.chunk_id is None


def test_evidence_item_accepts_chunk_provenance():
    item = EvidenceItem(
        source="OFAC guidance",
        detail="...",
        document_id="ofac-sanctions-corpus",
        chunk_id="OFAC-014",
    )
    assert item.chunk_id == "OFAC-014"
