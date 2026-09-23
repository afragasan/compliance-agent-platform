"""``rag.ingest``: corpus loading + build_index, fully offline."""

from __future__ import annotations

from fakes import FakeVectorStore

from compliance_agent_platform.config import Settings
from compliance_agent_platform.embeddings import _FakeEmbeddings
from compliance_agent_platform.rag.ingest import build_index, load_corpus


def _write_corpus(tmp_path, name: str, n_chunks: int) -> None:
    blocks = []
    for i in range(n_chunks):
        blocks.append(
            f"[CHUNK_ID]: {name.upper()}-{i:03d}\n"
            f"[TOPIC]: Topic {i}\n"
            f"[TEXT]: Body text {i}.\n"
            f"[CITATION]: Citation {i}\n"
        )
    (tmp_path / f"{name}-corpus.txt").write_text("\n".join(blocks), encoding="utf-8")


def test_load_corpus_reads_every_txt_file(tmp_path):
    _write_corpus(tmp_path, "ofac", 2)
    _write_corpus(tmp_path, "nacha", 3)

    chunks = load_corpus(tmp_path)

    assert len(chunks) == 5
    assert {c.document_id for c in chunks} == {"ofac-corpus", "nacha-corpus"}


def test_build_index_embeds_and_upserts_all_chunks(tmp_path):
    _write_corpus(tmp_path, "ofac", 4)
    settings = Settings(regulatory_corpus_dir=str(tmp_path))
    store = FakeVectorStore()

    summary = build_index(settings, store, embedder=_FakeEmbeddings(8))

    assert summary.documents == 1
    assert summary.chunks == 4
    assert len(store.upserted) == 1
    upserted_chunks, upserted_embeddings = store.upserted[0]
    assert len(upserted_chunks) == 4
    assert len(upserted_embeddings) == 4
    assert len(upserted_embeddings[0]) == 8


def test_build_index_on_empty_corpus_dir_is_a_noop(tmp_path):
    settings = Settings(regulatory_corpus_dir=str(tmp_path))
    store = FakeVectorStore()

    summary = build_index(settings, store, embedder=_FakeEmbeddings(8))

    assert summary == type(summary)(documents=0, chunks=0, backend=settings.vector_backend)
    assert store.upserted == []
