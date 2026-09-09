"""Sanity check for the integration-test harness (conftest fixtures)."""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.integration


def test_clean_db_leaves_empty_screening_tables(clean_db: str):
    with psycopg.connect(clean_db, autocommit=True) as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "screening_audit"):
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} not empty after clean_db"


def test_audit_schema_and_immutability_trigger_present(clean_db: str):
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO screening_audit (alert_id, thread_id, step, actor, event) "
            "VALUES ('HARNESS', 'HARNESS', 'intake', 'system', 'node_completed')"
        )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM screening_audit WHERE alert_id = 'HARNESS'")


def test_load_alert_fixture(load_alert):
    alert = load_alert("alert_true_match")
    assert alert["alert_id"] == "ALRT-MATCH-001"
    assert alert["hits"]
