# compliance-agent-platform

Compliance-Aware Agent Platform for Financial Services.

## `sanctions_screening` agent (Week 1)

A single-queue LangGraph agent that screens sanctions alerts through a fixed state
machine, checkpoints to PostgreSQL (so it survives an unclean process exit), and emits
a typed disposition with a human-in-the-loop escalation path.

```
START → intake → enrich → evaluate → ├─ escalate ─→ dispose → END
                                     └───────────────↑
```

- **Disposition types:** `clear`, `true_match`, `escalate_to_analyst`, `insufficient_data`
  (`schemas/disposition.py`).
- **Checkpointing:** `PostgresSaver` on Amazon RDS for PostgreSQL; `thread_id == alert_id`.
- **HITL:** `interrupt()` in the `escalate` node; analyst resumes with `Command(resume=...)`.
- **Audit:** LangGraph checkpoint lineage + an append-only `screening_audit` table
  (`audit/schema.sql`).
- **AgentCore:** `runtime/contract.py:invoke(payload, context) -> dict` already matches the
  Bedrock AgentCore Runtime entrypoint contract. See `ADR/001-why-langgraph.md`.

### Layout

| Path | Purpose |
|---|---|
| `schemas/` | Pydantic contracts: alert, enrichment, evaluation, disposition, graph state |
| `adapters/` | Provider Protocols + mock implementations (Week 1) |
| `graph/` | Nodes, routing policy, graph builder |
| `checkpoint/` | PostgresSaver + audit connection bootstrap |
| `audit/` | Append-only audit schema + writer |
| `runtime/` | AgentCore-shaped `invoke` + `cap` CLI |

### Run locally

```bash
docker compose up -d postgres
cp .env.example .env            # adjust AWS_REGION / BEDROCK_MODEL_ID; AWS creds via env or SSO
uv sync

uv run cap run --alert examples/alert_clear.json
uv run cap run --alert examples/alert_true_match.json     # -> status: escalated, note thread_id
uv run cap resume --thread ALRT-MATCH-001 --resolution true_match \
    --analyst-id A123 --rationale "Confirmed DOB and nationality match"
```

Crash-recovery: after `run` on `alert_true_match.json` the alert is parked in Postgres;
`resume` from a brand-new process against the same `thread_id` completes it.

### Testing

Test suite is planned for the Week 1 follow-up — see `docs/testing-plan.md`.

### Not yet (Week 2)

`BedrockAgentCoreApp` wrapper + Dockerfile (`/invocations`, `/ping`), RDS/VPC/IAM
provisioning, real watchlist / KYC / adverse-media integrations.
