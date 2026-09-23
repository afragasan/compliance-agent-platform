"""``screening_audit`` is the compliance system-of-record: append-only, no edits."""

from __future__ import annotations

import psycopg
import pytest

from compliance_agent_platform.audit.log import verify_chain
from compliance_agent_platform.runtime.contract import invoke

pytestmark = pytest.mark.integration


@pytest.fixture
def disposed_run(clean_db, load_alert, monkeypatch) -> str:
    monkeypatch.setenv("CAP_FAKE_MODEL", "clear")
    invoke({"alert": load_alert("alert_clear")})
    return clean_db


def test_update_is_blocked(disposed_run):
    with (
        psycopg.connect(disposed_run, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="append-only"),
    ):
        conn.execute(
            "UPDATE screening_audit SET actor = 'tamper' WHERE alert_id = 'ALRT-CLEAR-001'"
        )


def test_delete_is_blocked(disposed_run):
    with (
        psycopg.connect(disposed_run, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="append-only"),
    ):
        conn.execute("DELETE FROM screening_audit WHERE alert_id = 'ALRT-CLEAR-001'")


def test_hash_chain_is_populated_and_verifies_on_real_rows(disposed_run):
    with psycopg.connect(disposed_run, autocommit=True) as conn:
        cols = [d.name for d in conn.execute("SELECT * FROM screening_audit LIMIT 0").description]
        raw_rows = conn.execute("SELECT * FROM screening_audit ORDER BY id").fetchall()
    rows = [dict(zip(cols, r, strict=True)) for r in raw_rows]

    assert rows  # the disposed_run's intake..dispose rows
    assert all(r["prev_hash"] is not None and r["record_hash"] is not None for r in rows)

    result = verify_chain(rows)
    assert result.ok is True


def test_insert_and_select_and_correction_row_work(disposed_run):
    with psycopg.connect(disposed_run, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT id FROM screening_audit WHERE alert_id = 'ALRT-CLEAR-001' AND step = 'dispose'"
        ).fetchall()
        assert len(rows) == 1
        original_id = rows[0][0]

        # a correction is a NEW row pointing at the superseded one, never an edit
        conn.execute(
            "INSERT INTO screening_audit (alert_id, thread_id, step, actor, event, supersedes_id) "
            "VALUES ('ALRT-CLEAR-001', 'ALRT-CLEAR-001', 'dispose', 'analyst', 'disposition', %s)",
            (original_id,),
        )
        corrections = conn.execute(
            "SELECT supersedes_id FROM screening_audit WHERE supersedes_id = %s", (original_id,)
        ).fetchall()
        assert corrections == [(original_id,)]
