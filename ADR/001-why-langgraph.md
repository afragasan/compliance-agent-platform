# ADR 001 — Why LangGraph for the sanctions-screening agent

- **Status:** Accepted
- **Date:** 2026-09-08
- **Deciders:** Compliance Agent Platform team
- **Scope:** The `sanctions_screening` queue agent (Week 1). Sets the pattern for
  every subsequent single-queue compliance agent.

## Context

We are building an agent that works a single queue of sanctions-screening alerts. For
each alert it must gather context, decide, and emit a **typed disposition**
(`clear`, `true_match`, `escalate_to_analyst`, `insufficient_data`). Some alerts must be
handed to a human analyst and later resumed. The process runs unattended and may be
killed at any moment (deploy, crash, spot reclaim); no alert may be lost or silently
re-run from scratch. Every decision must be reconstructable for an auditor or regulator.

The target production home is **Amazon Bedrock AgentCore Runtime**, but the Week 1
deliverable runs the agent directly from Python. We therefore need a framework that (a)
models the workflow as an explicit, reviewable state machine, (b) gives us durable,
inspectable execution history, and (c) does not tie us to any particular runtime.

## Decision

**Use LangGraph** to implement the agent as a compiled `StateGraph`, with:

- nodes `intake → enrich → evaluate → [escalate | dispose]` over a typed shared state;
- a single conditional edge after `evaluate`;
- `interrupt()` / `Command(resume=...)` for the human-in-the-loop escalation;
- the **PostgreSQL checkpointer** (`langgraph-checkpoint-postgres`, `PostgresSaver`)
  persisting state to Amazon RDS for PostgreSQL;
- a stable `invoke(payload, context) -> dict` entrypoint that already matches the
  AgentCore Runtime contract.

### State machine model

The workflow is a **finite, explicitly-wired directed graph**, not an autonomous
agent loop. States and transitions:

| State (node) | Purpose | Outgoing transitions |
|---|---|---|
| `START` | — | → `intake` |
| `intake` | validate + normalize the alert into `ScreeningAlert`; write intake audit row | → `enrich` |
| `enrich` | pull watchlist entries, customer KYC, adverse media via provider adapters; compute name/DOB/nationality match features; flag `insufficient_data` | → `evaluate` |
| `evaluate` | Bedrock (Claude) structured call → `EvaluationResult { recommended, confidence, rationale, evidence }`; a rule overlay forces `insufficient_data` when enrichment is thin | → `route_after_evaluate` |
| `route_after_evaluate` (conditional edge) | policy function over `EvaluationResult` | → `escalate` \| `dispose` |
| `escalate` | `interrupt()` — a **first-class suspended state**; the graph stops and the checkpoint holds the pending interrupt until an analyst resumes with an `AnalystDecision` | → `dispose` |
| `dispose` | build the immutable `Disposition` (from the analyst decision if present, else the agent evaluation); write disposition audit row | → `END` |

Properties this buys us:

- **Bounded transition set.** The only paths are the ones drawn above. There is no
  open-ended "agent decides what to do next" step, so the behaviour space is small
  enough to review, test, and put in front of compliance as a diagram that maps 1:1
  to the written procedure.
- **Determinism where it matters.** Node functions are pure functions of
  `(state, injected dependencies)`. The LLM call runs at temperature 0 and its full
  request/response is recorded. Routing is a plain policy function
  (`graph/routing.py`), not a model decision, so *why* an alert escalated is always a
  readable rule (`recommended in {true_match, escalate_to_analyst}`, or
  `confidence < CLEAR_CONFIDENCE_THRESHOLD`).
- **One queue = one graph.** Each compliance queue gets its own graph module with its
  own state type and policy. No shared mega-agent.
- **Suspension is modelled, not improvised.** HITL is `interrupt()`; the paused alert
  is ordinary persisted state, so "waiting on an analyst" survives restarts for free.

Alternatives considered:

- **Autonomous ReAct / tool-calling agent** (LangChain `AgentExecutor`, raw tool loop):
  rejected — non-deterministic transition set, hard to audit, and the disposition
  taxonomy would be enforced only by prompt.
- **Plain LangChain chains (LCEL):** rejected — no durable state, no resumable
  interrupts, no execution history.
- **AWS Step Functions / Temporal:** durable and battle-tested, but we would hand-build
  the LLM orchestration, structured-output parsing, and HITL primitives, and local
  iteration is much slower. LangGraph gives us the state machine *and* those primitives;
  if we later need cross-service saga orchestration we can wrap the graph in a Step
  Functions task without changing it.

### Audit properties

Two layers, both in the RDS instance we control:

1. **Checkpoint lineage (LangGraph).** `PostgresSaver` writes a checkpoint after every
   super-step into `checkpoints` / `checkpoint_writes` / `checkpoint_blobs`. Each
   checkpoint is an immutable snapshot keyed by
   `(thread_id, checkpoint_ns, checkpoint_id)`. `graph.get_state_history(config)` replays
   every state transition in order, including the exact `EvaluationResult`, the routing
   outcome, the value the analyst passed to `Command(resume=...)`, and the final
   `Disposition`. This gives full time-travel / lineage for any alert
   (`thread_id == alert_id`).
2. **`screening_audit` table (application).** A dedicated **append-only** table
   (`audit/schema.sql`), one row per node completion and per disposition, carrying:
   `alert_id`, `thread_id`, `step`, `actor` (`agent` / `analyst` / `system`), `event`,
   `payload_hash` (sha256 of the node input — proves what the agent saw),
   `llm_request` / `llm_response` (evaluate only), `model_id`, `prompt_version`,
   `detail`, and `supersedes_id`. A `BEFORE UPDATE OR DELETE` trigger rejects all
   mutation; the application DB role is granted `INSERT, SELECT` only. Corrections are
   **new rows** that reference the superseded row — the record is never edited.

Why this satisfies compliance:

- **Immutability & attribution.** Every decision is attributable to `agent` (with the
  exact model id and prompt version) or a named `analyst_id`, at a timestamp, and cannot
  be altered after the fact.
- **Reproducibility.** `payload_hash` plus the recorded prompt and model id let us
  prove — or re-run — exactly what the agent evaluated.
- **Retention & access on our terms.** Because state lives in RDS (encrypted with our
  KMS key, our backup/retention policy, our PITR window), examiners get direct
  read-only SQL access. We are not dependent on an opaque managed store's export format
  or retention limits.
- **HITL is on the record.** The escalation `interrupt` and the analyst's resume value
  are both audit events *and* checkpoint entries.

### AgentCore compatibility

Amazon Bedrock AgentCore Runtime is a **framework-agnostic** container runtime for
agents; LangGraph is a first-class supported framework. We stay compatible without
adopting the runtime yet:

- **Entrypoint contract now.** `runtime/contract.py:invoke(payload: dict, context) -> dict`
  already has the shape `BedrockAgentCoreApp.entrypoint` expects: a JSON-serializable
  dict in, a JSON-serializable dict out. `context.session_id` (what AgentCore supplies
  per session) is mapped straight to the LangGraph `thread_id`. Week 2 adds nothing to
  the logic — it decorates this function with `@app.entrypoint` and adds a Dockerfile
  exposing `/invocations` + `/ping` on port 8080.
- **Stateless process, state in Postgres.** No node writes to the local filesystem or
  in-process globals between invocations; all durable state is in RDS. This matches
  AgentCore's per-invocation isolation model and makes horizontal scaling and
  mid-session recovery trivial (any worker can pick up any `thread_id`).
- **Checkpointer choice: `PostgresSaver` on RDS, not `AgentCoreMemorySaver`.** The
  `langgraph-checkpoint-aws` package offers an `AgentCoreMemorySaver` backed by Bedrock
  session/memory management. We deliberately keep the **system-of-record checkpoint
  state in RDS** because we need a single auditable store with SQL access, our own
  retention/encryption/PITR, and no runtime lock-in. `AgentCoreMemorySaver` remains
  available later for *conversational* memory if a future agent needs it; it does not
  replace the audited screening state.
- **Runtime prerequisites documented for Week 2.** VPC egress from the runtime to the
  RDS subnet, an execution role with `bedrock:InvokeModel` and
  `secretsmanager:GetSecretValue`, and DB credentials resolved from Secrets Manager
  (`config.py:Settings.resolved_dsn`, already wired behind `DB_SECRET_ARN`).
- **Execution envelope.** A single alert screening (including one LLM call) completes in
  seconds and well inside AgentCore's per-invocation ceiling; escalations are *not* a
  long-running invocation — the graph returns `status: "escalated"` immediately and the
  alert waits in Postgres until a later `resume` invocation.

## Consequences

**Positive**

- The workflow is a diagram compliance can sign off on; routing rationale is always a
  readable rule.
- Crash/restart safety and HITL suspension come from one mechanism (the checkpointer)
  rather than bespoke code.
- Full, immutable audit trail with model/prompt provenance, owned by us in RDS.
- Week 2 AgentCore packaging is a wrapper, not a rewrite.

**Negative / risks**

- LangGraph + `langgraph-checkpoint-postgres` are fast-moving (pinned:
  `langgraph>=1.2.11`, `langgraph-checkpoint-postgres>=3.1.0`); upgrades need regression
  testing against the checkpoint schema.
- Two persistence layers (checkpoint tables + `screening_audit`) must be kept
  consistent; audit writes happen inside nodes, so a node that fails after its audit
  write will re-emit that row on replay — acceptable because the table is append-only
  and rows are timestamped, but queries must account for possible duplicates per
  `(alert_id, step)`.
- Running the LLM through Bedrock adds a VPC/IAM dependency to local development
  (mitigated: the model is injectable in `build_graph(model=...)` for offline tests).

## References

- LangGraph persistence & `interrupt` — https://langchain-ai.github.io/langgraph/concepts/persistence/
- `langgraph-checkpoint-postgres` — https://pypi.org/project/langgraph-checkpoint-postgres/
- Amazon Bedrock AgentCore Runtime — https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
- AgentCore Memory + LangGraph — https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-integrate-lang.html
- `langgraph-checkpoint-aws` (`AgentCoreMemorySaver`) — https://pypi.org/project/langgraph-checkpoint-aws/
