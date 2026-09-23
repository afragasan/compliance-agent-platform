"""Sanity check for the integration-test harness (conftest fixtures)."""

from __future__ import annotations

import psycopg
import pytest

from compliance_agent_platform.audit.log import AuditRecord, write_audit

pytestmark = pytest.mark.integration


def test_clean_db_leaves_empty_screening_tables(clean_db: str):
    with psycopg.connect(clean_db, autocommit=True) as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "screening_audit"):
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} not empty after clean_db"


def test_audit_schema_and_immutability_trigger_present(clean_db: str):
    # Insert through write_audit (not raw SQL) so the row this test leaves behind
    # in the shared cap_test database is properly hash-chained like every other
    # row `cap` writes - an unchained row here previously tripped up
    # `cap audit export`/`verify` on a freshly test-truncated table (it isn't
    # tampering, just a row that predates the chain, but export correctly
    # refuses to treat it as a valid link either way).
    with psycopg.connect(clean_db, autocommit=False) as write_conn:
        write_audit(
            write_conn,
            AuditRecord(
                alert_id="HARNESS",
                thread_id="HARNESS",
                step="intake",
                actor="system",
                event="node_completed",
            ),
        )

    with psycopg.connect(clean_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM screening_audit WHERE alert_id = 'HARNESS'")


def test_load_alert_fixture(load_alert):
    alert = load_alert("alert_true_match")
    assert alert["alert_id"] == "ALRT-MATCH-001"
    assert alert["hits"]
