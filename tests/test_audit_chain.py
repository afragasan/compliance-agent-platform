"""Hash-chain math: `write_audit` chaining and `verify_chain` (no DB, no network)."""

from __future__ import annotations

from datetime import datetime, timezone

from fakes import FakeAuditConn

from compliance_agent_platform.audit.log import (
    _CHAIN_GENESIS,
    AuditRecord,
    _chain_hash,
    verify_chain,
    write_audit,
)


def _record(**overrides) -> AuditRecord:
    base = dict(
        alert_id="A1", thread_id="A1", step="intake", actor="system", event="node_completed"
    )
    base.update(overrides)
    return AuditRecord(**base)


# --- _chain_hash -------------------------------------------------------------


def test_chain_hash_is_deterministic():
    record = _record()
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _chain_hash(record, created_at, "prev") == _chain_hash(record, created_at, "prev")


def test_chain_hash_changes_with_prev_hash():
    record = _record()
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _chain_hash(record, created_at, "prev-a") != _chain_hash(record, created_at, "prev-b")


def test_chain_hash_changes_with_business_field():
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    h1 = _chain_hash(_record(event="node_completed"), created_at, "prev")
    h2 = _chain_hash(_record(event="disposition"), created_at, "prev")
    assert h1 != h2


# --- write_audit ---------------------------------------------------------------


def test_write_audit_first_row_chains_from_genesis():
    conn = FakeAuditConn()
    write_audit(conn, _record())

    assert conn.rows[0]["prev_hash"] == _CHAIN_GENESIS
    assert conn.rows[0]["record_hash"]
    assert conn.committed == 1


def test_write_audit_links_second_row_to_first():
    conn = FakeAuditConn()
    write_audit(conn, _record(step="intake"))
    write_audit(conn, _record(step="enrich"))

    assert conn.rows[1]["prev_hash"] == conn.rows[0]["record_hash"]
    assert conn.rows[1]["record_hash"] != conn.rows[0]["record_hash"]


def test_write_audit_takes_advisory_lock_before_reading_the_tail():
    conn = FakeAuditConn()
    write_audit(conn, _record())

    sql_calls = [sql for sql, _ in conn.calls]
    assert "pg_advisory_xact_lock" in sql_calls[0]
    assert sql_calls[1].startswith("SELECT record_hash FROM screening_audit")


# --- verify_chain --------------------------------------------------------------


def test_verify_chain_happy_path_multiple_rows():
    conn = FakeAuditConn()
    for step in ("intake", "enrich", "evaluate", "dispose"):
        write_audit(conn, _record(step=step, event="node_completed"))

    result = verify_chain(conn.rows)
    assert result.ok is True
    assert result.broken_at_id is None


def test_verify_chain_single_genesis_row():
    conn = FakeAuditConn()
    write_audit(conn, _record())

    assert verify_chain(conn.rows).ok is True


def test_verify_chain_tolerates_replay_duplicate_rows():
    # escalate's pre-interrupt audit call re-executes on resume/replay: two rows
    # with an identical step/actor/event are legitimate, not tampering.
    conn = FakeAuditConn()
    write_audit(conn, _record(step="escalate", event="interrupt_raised"))
    write_audit(conn, _record(step="escalate", event="interrupt_raised"))

    result = verify_chain(conn.rows)
    assert result.ok is True


def test_verify_chain_detects_tampered_business_field():
    conn = FakeAuditConn()
    write_audit(conn, _record(step="intake"))
    write_audit(conn, _record(step="enrich"))

    conn.rows[0]["event"] = "tampered"

    result = verify_chain(conn.rows)
    assert result.ok is False
    assert result.broken_at_id == 1


def test_verify_chain_detects_deleted_row_gap():
    conn = FakeAuditConn()
    write_audit(conn, _record(step="intake"))
    write_audit(conn, _record(step="enrich"))
    write_audit(conn, _record(step="evaluate"))

    del conn.rows[1]  # simulate a deleted middle row

    result = verify_chain(conn.rows)
    assert result.ok is False
    assert result.broken_at_id == 3
