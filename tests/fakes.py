"""Shared test doubles used across multiple test modules."""

from __future__ import annotations

from collections import namedtuple
from typing import Any

_ColumnDescription = namedtuple("_ColumnDescription", ["name"])

# Mirrors screening_audit's real column order (schema.sql) plus the synthetic
# `id` FakeAuditConn assigns on insert - lets `SELECT * ...`-style queries
# (used by the S3 exporter) round-trip through `.description`/row tuples the
# same way a real psycopg cursor would.
_COLUMNS = [
    "id",
    "alert_id",
    "thread_id",
    "checkpoint_id",
    "step",
    "actor",
    "event",
    "payload_hash",
    "llm_request",
    "llm_response",
    "model_id",
    "prompt_version",
    "detail",
    "supersedes_id",
    "created_at",
    "prev_hash",
    "record_hash",
]


class _FakeCursor:
    def __init__(self, rows: list[tuple], *, description: list | None = None) -> None:
        self._rows = rows
        self.description = description or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeAuditConn:
    """In-memory stand-in for the audit ``psycopg`` connection.

    Understands just enough of ``write_audit``'s and the S3 exporter's SQL
    shapes to exercise the hash-chain and export logic without a real
    database: advisory-lock calls are no-ops, ``SELECT record_hash ... ORDER
    BY id DESC LIMIT 1`` returns the last inserted row's hash, ``SELECT * ...
    LIMIT 0`` / ``... WHERE id > %s ORDER BY id`` answer with column
    descriptions and row tuples in ``_COLUMNS`` order, and INSERTs are
    recorded (and assigned an incrementing id) so tests can assert on them.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.committed = 0

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self.calls.append((sql, params))
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return _FakeCursor([])
        if normalized.startswith("SELECT record_hash FROM screening_audit"):
            if not self.rows:
                return _FakeCursor([])
            return _FakeCursor([(self.rows[-1]["record_hash"],)])
        if normalized.startswith("SELECT * FROM screening_audit LIMIT 0"):
            return _FakeCursor([], description=[_ColumnDescription(c) for c in _COLUMNS])
        if normalized.startswith("SELECT * FROM screening_audit WHERE id >"):
            since_id = params[0] if params else 0
            selected = [r for r in self.rows if r["id"] > since_id]
            tuples = [tuple(r.get(c) for c in _COLUMNS) for r in selected]
            return _FakeCursor(tuples)
        if normalized.startswith("INSERT INTO screening_audit"):
            row = dict(params)
            row["id"] = len(self.rows) + 1
            self.rows.append(row)
            return _FakeCursor([])
        raise AssertionError(f"FakeAuditConn does not understand: {sql!r}")

    def commit(self) -> None:
        self.committed += 1


class FakeVectorStore:
    """In-memory :class:`VectorStore` returning pre-canned, clearly-synthetic chunks
    regardless of the query embedding - enough to exercise the ``retrieve`` node's
    wiring without any embedding/similarity math."""

    def __init__(self, chunks: list | None = None) -> None:
        from compliance_agent_platform.schemas.retrieval import RetrievedChunk

        self._chunks = chunks or [
            RetrievedChunk(
                chunk_id="TEST-001",
                document_id="test-corpus",
                chunk_index=0,
                text="[TEST CHUNK] placeholder regulatory guidance text.",
                metadata={
                    "document_id": "test-corpus",
                    "title": "Test Topic",
                    "program": "TEST",
                    "section": "TEST-REF-1",
                    "citation": "Test Citation 1",
                    "compliance_level": "Mandatory",
                },
                score=0.99,
            )
        ]
        self.upserted: list[tuple] = []

    def search(self, embedding, k: int = 5):
        return self._chunks[:k]

    def upsert(self, chunks, embeddings) -> None:
        self.upserted.append((chunks, embeddings))


class FakeEmbedder:
    """Trivial embedder - the vector's content never matters to ``FakeVectorStore``."""

    def embed_query(self, text: str) -> list[float]:
        return [0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]
