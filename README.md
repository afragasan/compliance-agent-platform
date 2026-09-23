# compliance-agent-platform

Compliance-Aware Agent Platform for Financial Services.

## `sanctions_screening` agent (Week 1)

A single-queue LangGraph agent that screens sanctions alerts through a fixed state
machine, checkpoints to PostgreSQL (so it survives an unclean process exit), and emits
a typed disposition with a human-in-the-loop escalation path.

```
START → intake → enrich → retrieve → evaluate → ├─ escalate ─→ dispose → END
                                                └───────────────↑
```

- **Disposition types:** `clear`, `true_match`, `escalate_to_analyst`, `insufficient_data`
  (`schemas/disposition.py`).
- **Checkpointing:** `PostgresSaver` on Amazon RDS for PostgreSQL; `thread_id == alert_id`.
- **HITL:** `interrupt()` in the `escalate` node; analyst resumes with `Command(resume=...)`.
- **Audit:** LangGraph checkpoint lineage + an append-only, hash-chained `screening_audit`
  table (`audit/schema.sql`), enforced by `compliance_mw` middleware that wraps every node
  and blocks citation-less `clear`/`true_match` dispositions. See `ADR/002-audit-chain-design.md`.
- **Retrieval (RAG):** `retrieve` grounds `evaluate` in `docs/regulatory-corpus/` via a
  FAISS (local) or pgvector (deployed) vector store — see `ADR/003-rag-vs-fine-tuning.md`.
- **AgentCore:** `runtime/contract.py:invoke(payload, context) -> dict` already matches the
  Bedrock AgentCore Runtime entrypoint contract. See `ADR/001-why-langgraph.md`.

### Layout

| Path | Purpose |
|---|---|
| `schemas/` | Pydantic contracts: alert, enrichment, retrieval, evaluation, disposition, graph state |
| `adapters/` | Provider Protocols + mock implementations (Week 1) |
| `graph/` | Nodes, routing policy, graph builder |
| `checkpoint/` | PostgresSaver + audit connection bootstrap |
| `audit/` | Hash-chained append-only audit schema/writer + S3 Object Lock export |
| `compliance_mw/` | Node-wrapping middleware: failure auditing + citation-mandatory enforcement |
| `rag/` | Regulatory-corpus parsing, chunking, FAISS/pgvector stores, retrieval eval |
| `embeddings.py` | Bedrock Titan Embeddings factory (mirrors `llm.py`) |
| `runtime/` | AgentCore-shaped `invoke` + `cap` CLI |

### Run locally

```bash
docker compose up -d postgres
cp .env.example .env            # adjust AWS_REGION / BEDROCK_MODEL_ID; AWS creds via env or SSO
uv sync

uv run cap rag ingest --backend faiss      # embed docs/regulatory-corpus/ into a local index
uv run cap run --alert examples/alert_clear.json
uv run cap run --alert examples/alert_true_match.json     # -> status: escalated, note thread_id
uv run cap resume --thread ALRT-MATCH-001 --resolution true_match \
    --analyst-id A123 --rationale "Confirmed DOB and nationality match"
```

Crash-recovery: after `run` on `alert_true_match.json` the alert is parked in Postgres;
`resume` from a brand-new process against the same `thread_id` completes it.

### Testing

`uv run pytest -q` — unit tests run with no DB/network; `@pytest.mark.integration` tests
run against a real Postgres (`DATABASE_URL`) and are skipped automatically without one.
See `docs/testing-plan.md`, `docs/validation-runbook.md`, and the Week 2/3 runbooks for
what's been exercised so far.

### Not yet

`BedrockAgentCoreApp` wrapper + Dockerfile (`/invocations`, `/ping`), RDS/VPC/IAM
provisioning as IaC, real watchlist / KYC / adverse-media integrations (still mocked),
retrieved-chunk citations aren't cross-checked against what was actually retrieved for
an alert (see `docs/week3-validation-runbook.md`'s carried-forward gaps).
