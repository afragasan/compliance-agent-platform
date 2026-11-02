"""Inference cost reporting: tokens consumed per queue/model, projected forward.

Reads straight from ``screening_audit.detail`` (``step='evaluate'``, rows that actually
called a model - the rule-overlay ``insufficient_data`` path never touches a model and
carries no token fields, so it's excluded by the ``detail ? 'input_tokens'`` filter).
No separate cost table: Week 4's token/latency/backend fields were added directly onto
the existing audit row's ``detail`` JSONB (see ``graph/nodes.py::make_evaluate``),
mirroring how the hash chain extended the same table rather than adding a new one.

Bedrock is priced per token (published per-1K rates in ``Settings``). NIM is
self-hosted with no per-token cost, so it's priced by GPU-time amortization: observed
call latency, summed and converted to GPU-hours, times ``Settings.nim_gpu_hourly_rate``
(an on-demand ``g5.4xlarge`` rate) - an honest number for a backend billed by the hour
regardless of how many tokens it serves, not a token-based guess that would
misrepresent NIM's actual cost structure. ``CAP_FAKE_MODEL``'s "fake" backend prices at
$0 - it never made a real call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from compliance_agent_platform.config import Settings


def price_call(
    settings: Settings, backend: str, input_tokens: int, output_tokens: int, latency_ms: int
) -> float:
    """USD cost of one `evaluate` LLM call, given which backend actually served it."""
    if backend == "bedrock":
        return (input_tokens / 1000) * settings.bedrock_input_price_per_1k + (
            output_tokens / 1000
        ) * settings.bedrock_output_price_per_1k
    if backend == "nim":
        gpu_hours = latency_ms / 3_600_000
        return gpu_hours * settings.nim_gpu_hourly_rate
    return 0.0  # "fake" (CAP_FAKE_MODEL) or any unrecognized backend: not a real cost.


@dataclass
class CostRow:
    queue: str
    backend: str
    model_id: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    observed_cost_usd: float = 0.0


@dataclass
class CostReport:
    since: str | None
    observed_window_days: float
    project_days: int
    observed_cost_usd: float
    projected_cost_usd: float
    rows: list[CostRow] = field(default_factory=list)


def build_cost_report(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    since: datetime | None = None,
    project_days: int = 30,
    queue: str | None = None,
    backend: str | None = None,
) -> CostReport:
    """Aggregate real `evaluate` audit rows into per-(queue, backend, model) cost,
    then scale the observed window's rate forward to `project_days` - a real rate
    extrapolated, not a fabricated monthly guess."""
    where = ["step = 'evaluate'", "event = 'node_completed'", "detail ? 'input_tokens'"]
    params: list[object] = []
    if since is not None:
        where.append("created_at >= %s")
        params.append(since)
    if queue is not None:
        where.append("detail->>'queue' = %s")
        params.append(queue)
    if backend is not None:
        where.append("detail->>'backend' = %s")
        params.append(backend)

    cur = conn.execute(
        "SELECT detail->>'queue', detail->>'backend', model_id, "
        "(detail->>'input_tokens')::int, (detail->>'output_tokens')::int, "
        "(detail->>'latency_ms')::int, created_at "
        f"FROM screening_audit WHERE {' AND '.join(where)} ORDER BY created_at",
        params,
    )
    matched = cur.fetchall()
    if not matched:
        return CostReport(
            since=since.isoformat() if since else None,
            observed_window_days=0.0,
            project_days=project_days,
            observed_cost_usd=0.0,
            projected_cost_usd=0.0,
            rows=[],
        )

    rows_by_key: dict[tuple[str, str, str], CostRow] = {}
    for q, b, model_id, input_tokens, output_tokens, latency_ms, _created_at in matched:
        key = (q or "unknown", b or "unknown", model_id or "unknown")
        row = rows_by_key.setdefault(key, CostRow(queue=key[0], backend=key[1], model_id=key[2]))
        row.calls += 1
        row.input_tokens += input_tokens or 0
        row.output_tokens += output_tokens or 0
        row.latency_ms += latency_ms or 0

    for row in rows_by_key.values():
        row.observed_cost_usd = round(
            price_call(settings, row.backend, row.input_tokens, row.output_tokens, row.latency_ms),
            6,
        )

    rows = sorted(rows_by_key.values(), key=lambda r: (r.queue, r.backend, r.model_id))
    observed_cost = round(sum(r.observed_cost_usd for r in rows), 6)

    window_start = since or matched[0][6]
    now = datetime.now(timezone.utc)
    window_days = max((now - window_start).total_seconds() / 86400, 1 / 1440)  # floor: 1 minute
    projected_cost = round(observed_cost * (project_days / window_days), 2)

    return CostReport(
        since=window_start.isoformat(),
        observed_window_days=round(window_days, 4),
        project_days=project_days,
        observed_cost_usd=observed_cost,
        projected_cost_usd=projected_cost,
        rows=rows,
    )
