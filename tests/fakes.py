"""Shared test doubles used across multiple test modules."""

from __future__ import annotations

from typing import Any


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeAuditConn:
    """In-memory stand-in for the audit ``psycopg`` connection.

    Understands just enough of ``write_audit``'s SQL shapes to exercise the
    hash-chain logic without a real database: advisory-lock calls are
    no-ops, ``SELECT record_hash ... ORDER BY id DESC LIMIT 1`` returns the
    last inserted row's hash, and INSERTs are recorded (and assigned an
    incrementing id) so tests can assert on chain fields.
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
        if normalized.startswith("INSERT INTO screening_audit"):
            row = dict(params)
            row["id"] = len(self.rows) + 1
            self.rows.append(row)
            return _FakeCursor([])
        raise AssertionError(f"FakeAuditConn does not understand: {sql!r}")

    def commit(self) -> None:
        self.committed += 1
