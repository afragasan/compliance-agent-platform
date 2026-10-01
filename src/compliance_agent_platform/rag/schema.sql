-- Regulatory-corpus vector store (pgvector backend).
--
-- Unlike `screening_audit`, `document_chunks` is reference data, not per-alert
-- transactional data: it holds the ingested regulatory corpus, not a record of what
-- the agent did. It is deliberately NOT in `tests/conftest.py`'s `clean_db` truncate
-- list -- wiping it before every integration test would force a real Bedrock
-- re-embed (cost, latency, network) per test run, and repeats the class of trap
-- documented for the audit-export bucket in `docs/aws-compliance-setup.md`. Upserts
-- are idempotent (`ON CONFLICT (chunk_id) DO UPDATE`), so re-ingesting is always safe.
--
-- Requires the `vector` extension. On RDS this must be allow-listed for the engine
-- version (supported on Postgres 15.2+/16.1+) -- see docs/aws-compliance-setup.md.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id      TEXT         PRIMARY KEY,
    document_id   TEXT         NOT NULL,
    chunk_index   INTEGER      NOT NULL,
    text          TEXT         NOT NULL,
    metadata      JSONB        NOT NULL,
    embedding     VECTOR(1024) NOT NULL,  -- must match Settings.embedding_dimension
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_document_chunks_document ON document_chunks (document_id);

-- HNSW, not IVFFlat: IVFFlat's lists are trained from data already in the table, so
-- it needs a representative sample loaded before the index is useful and degrades on
-- a small, frequently re-ingested corpus. HNSW builds incrementally and gives good
-- recall with no "train on existing rows first" step -- a better fit for a corpus
-- that's re-ingested whenever a regulatory document changes (see ADR-003).
CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
