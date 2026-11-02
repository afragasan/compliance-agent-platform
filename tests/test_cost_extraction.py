"""Cost tracking: pure pricing math (no DB), the evaluate node's audit-detail token
extraction (no DB), and the real SQL aggregation/grouping in `build_cost_report`
(`@pytest.mark.integration`, against the real audit schema in the integration test database)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fakes import FakeAuditConn, FakeEmbedder, FakeVectorStore

from compliance_agent_platform.adapters.mock import default_deps
from compliance_agent_platform.audit.log import AuditRecord, write_audit
from compliance_agent_platform.config import Settings
from compliance_agent_platform.cost import build_cost_report, price_call
from compliance_agent_platform.graph.builder import NodeContext
from compliance_agent_platform.graph.nodes import make_evaluate
from compliance_agent_platform.schemas.alert import ScreeningAlert, WatchlistHit
from compliance_agent_platform.schemas.disposition import DispositionType
from compliance_agent_platform.schemas.enrichment import EnrichmentBundle
from compliance_agent_platform.schemas.evaluation import EvaluationResult
from compliance_agent_platform.schemas.retrieval import RetrievalBundle

_SETTINGS = Settings(
    bedrock_model_id="test-model-id",
    prompt_version="test-v1",
    queue="sanctions_screening",
    bedrock_input_price_per_1k=0.003,
    bedrock_output_price_per_1k=0.015,
    nim_gpu_hourly_rate=1.624,
)


# --- pure pricing math --------------------------------------------------------


def test_price_call_bedrock_uses_published_token_rates():
    # 2000 input tokens @ $0.003/1K + 1000 output tokens @ $0.015/1K
    cost = price_call(_SETTINGS, "bedrock", input_tokens=2000, output_tokens=1000, latency_ms=999)
    assert cost == pytest.approx(2 * 0.003 + 1 * 0.015)


def test_price_call_nim_amortizes_gpu_hours_not_tokens():
    # 1 hour of latency, any token count -> exactly one GPU-hour's cost.
    cost = price_call(_SETTINGS, "nim", input_tokens=999_999, output_tokens=0, latency_ms=3_600_000)
    assert cost == pytest.approx(1.624)


def test_price_call_fake_backend_is_zero():
    cost = price_call(_SETTINGS, "fake", input_tokens=10_000, output_tokens=10_000, latency_ms=1)
    assert cost == 0.0


# --- evaluate's audit-detail extraction (no DB) ------------------------------


class _FakeRawMessage:
    def __init__(self, usage_metadata: dict) -> None:
        self.content = "..."
        self.usage_metadata = usage_metadata


class _FakeModel:
    def __init__(self, result: EvaluationResult, usage: dict) -> None:
        self._result = result
        self._usage = usage

    def with_structured_output(self, schema, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        return {
            "raw": _FakeRawMessage(self._usage),
            "parsed": self._result,
            "parsing_error": None,
        }


def _ctx(model) -> NodeContext:
    return NodeContext(
        deps=default_deps(),
        audit_conn=FakeAuditConn(),
        settings=_SETTINGS,
        model=model,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
        model_backend="nim",
        model_id="meta/llama-3.1-8b-instruct",
    )


def _alert() -> dict:
    return ScreeningAlert(
        alert_id="A1",
        subject_id="CUST-1001",
        screened_name="Viktor Petrov",
        date_of_birth=date(1969, 4, 12),
        nationality="RU",
        hits=[
            WatchlistHit(
                list_name="OFAC SDN", entry_id="SDN-12345", matched_name="Viktor Petrov", score=0.95
            )
        ],
    ).model_dump(mode="json")


def test_evaluate_audit_detail_carries_real_usage_and_backend():
    canned = EvaluationResult(recommended=DispositionType.CLEAR, confidence=0.9, rationale="clear")
    usage = {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168}
    ctx = _ctx(_FakeModel(canned, usage))
    state = {
        "alert": _alert(),
        "enrichment": EnrichmentBundle(insufficient_data=False).model_dump(mode="json"),
        "retrieval": RetrievalBundle(query="q").model_dump(mode="json"),
    }

    make_evaluate(ctx)(state)

    detail = ctx.audit_conn.rows[-1]["detail"].obj
    assert detail["backend"] == "nim"
    assert detail["input_tokens"] == 123
    assert detail["output_tokens"] == 45
    assert detail["total_tokens"] == 168
    assert detail["fallback_used"] is False
    assert ctx.audit_conn.rows[-1]["model_id"] == "meta/llama-3.1-8b-instruct"


# --- real SQL aggregation -----------------------------------------------------


@pytest.mark.integration
def test_build_cost_report_groups_and_prices_real_audit_rows(clean_db):
    import psycopg

    conn = psycopg.connect(clean_db, autocommit=True)
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        rows = [
            # Two NIM calls on sanctions_screening, 1800ms each -> 1 GPU-hour total.
            ("sanctions_screening", "nim", "meta/llama-3.1-8b-instruct", 100, 50, 1_800_000),
            ("sanctions_screening", "nim", "meta/llama-3.1-8b-instruct", 200, 100, 1_800_000),
            # One Bedrock call on aml_analysis.
            ("aml_analysis", "bedrock", "us.anthropic.claude-sonnet-4-6", 2000, 1000, 500),
        ]
        for queue, backend, model_id, in_tok, out_tok, latency in rows:
            write_audit(
                conn,
                AuditRecord(
                    alert_id="COST-TEST",
                    thread_id="COST-TEST",
                    step="evaluate",
                    actor="agent",
                    event="node_completed",
                    model_id=model_id,
                    detail={
                        "queue": queue,
                        "backend": backend,
                        "fallback_used": False,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "total_tokens": in_tok + out_tok,
                        "latency_ms": latency,
                    },
                ),
            )

        report = build_cost_report(conn, _SETTINGS, since=since, project_days=30)

        by_queue = {(r.queue, r.backend): r for r in report.rows}
        nim_row = by_queue[("sanctions_screening", "nim")]
        assert nim_row.calls == 2
        assert nim_row.input_tokens == 300
        assert nim_row.latency_ms == 3_600_000
        assert nim_row.observed_cost_usd == pytest.approx(_SETTINGS.nim_gpu_hourly_rate)

        bedrock_row = by_queue[("aml_analysis", "bedrock")]
        assert bedrock_row.calls == 1
        expected_bedrock_cost = (2000 / 1000) * _SETTINGS.bedrock_input_price_per_1k + (
            1000 / 1000
        ) * _SETTINGS.bedrock_output_price_per_1k
        assert bedrock_row.observed_cost_usd == pytest.approx(expected_bedrock_cost)

        total = nim_row.observed_cost_usd + bedrock_row.observed_cost_usd
        assert report.observed_cost_usd == pytest.approx(total)
        assert report.projected_cost_usd >= report.observed_cost_usd  # projecting 30d from <1h

        # --queue/--backend filters narrow the same data correctly.
        queue_only = build_cost_report(
            conn, _SETTINGS, since=since, queue="aml_analysis", project_days=30
        )
        assert [r.queue for r in queue_only.rows] == ["aml_analysis"]

        backend_only = build_cost_report(
            conn, _SETTINGS, since=since, backend="nim", project_days=30
        )
        assert all(r.backend == "nim" for r in backend_only.rows)
    finally:
        conn.close()
