"""The AgentCore-shaped entrypoint contract, exercised against ``cap_test``."""

from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest

from compliance_agent_platform.runtime.contract import invoke

pytestmark = pytest.mark.integration


def test_new_alert_returns_disposed_shape(clean_db, load_alert, monkeypatch):
    monkeypatch.setenv("CAP_FAKE_MODEL", "clear")
    result = invoke({"alert": load_alert("alert_clear")})

    assert set(result) == {"status", "thread_id", "disposition"}
    assert result["status"] == "disposed"
    assert result["thread_id"] == "ALRT-CLEAR-001"
    assert result["disposition"]["queue"] == "sanctions_screening"


def test_escalation_returns_interrupt_shape_then_resume_closes(clean_db, load_alert, monkeypatch):
    monkeypatch.setenv("CAP_FAKE_MODEL", "true_match")
    tid = load_alert("alert_true_match")["alert_id"]

    escalated = invoke({"alert": load_alert("alert_true_match")})
    assert set(escalated) == {"status", "thread_id", "interrupt"}
    assert escalated["status"] == "escalated"

    closed = invoke(
        {
            "thread_id": tid,
            "resume": {"resolution": "clear", "analyst_id": "A9", "rationale": "false positive"},
        }
    )
    assert closed["status"] == "disposed"
    assert closed["disposition"]["disposition"] == "clear"
    assert closed["disposition"]["decided_by"] == "analyst"


def test_disposition_with_no_evidence_returns_blocked_shape(clean_db, load_alert, monkeypatch):
    monkeypatch.setenv("CAP_FAKE_MODEL", "clear")
    alert = load_alert("alert_clear")
    alert["hits"] = []  # no watchlist hits -> no candidates -> no fallback evidence either

    result = invoke({"alert": alert})

    assert set(result) == {"status", "thread_id", "violation"}
    assert result["status"] == "blocked"
    assert result["violation"]["disposition_type"] == "clear"

    with psycopg.connect(clean_db, autocommit=True) as conn:
        events = {
            r[0]
            for r in conn.execute(
                "SELECT event FROM screening_audit WHERE alert_id = %s", (alert["alert_id"],)
            ).fetchall()
        }
    assert "disposition" in events  # dispose() itself still ran and audited
    assert "compliance_blocked" in events  # ...then the middleware rejected it


def test_session_id_overrides_alert_id_as_thread(clean_db, load_alert, monkeypatch):
    monkeypatch.setenv("CAP_FAKE_MODEL", "clear")
    context = SimpleNamespace(session_id="SESSION-XYZ")

    result = invoke({"alert": load_alert("alert_clear")}, context)

    assert result["thread_id"] == "SESSION-XYZ"
    # checkpoint + audit are keyed by the session id, not the alert id
    with psycopg.connect(clean_db, autocommit=True) as conn:
        threads = {
            r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        }
    assert "SESSION-XYZ" in threads
    assert "ALRT-CLEAR-001" not in threads
