# Week 4 validation runbook

Validates Week 4's NVIDIA NIM inference, multi-model routing, guardrails and cost
tracking against a **real NIM** and real Bedrock. Decisions for this run:

- **NIM target:** `meta/llama-3.1-8b-instruct` on the `nim-dev` EKS cluster, reached by
  `kubectl port-forward` at `http://127.0.0.1:8000/v1` (the `NIM_BASE_URL` default).
  Dev-only access path; see Known gaps.
- **Routing:** `sanctions_screening` → NIM with Bedrock fallback; `aml_analysis` →
  Bedrock (Claude Sonnet 4.6, `us.anthropic.claude-sonnet-4-6`). See
  [ADR-004](../ADR/004-nim-routing-and-guardrails.md).
- **Build stages (committed before this run):** A routing core and `llm.py`
  generalization, B `evaluate` integration (fallback, token capture), C guardrails
  package, D cost tracking and `cap cost report` (`b4f1e56`..`253320e`).
- **Defects found by this run and fixed:** `c76e949` (NIM payload, structured output,
  fallback, model_id, audit queue) and `e2920ae` (deterministic PII floor, scoped input
  rail, opt-in Bedrock input rail).
- Branch: `week4-NVIDIA-Inference-and-Multi-Model-routing`, off `main`.

**Evidence caveat.** The `cap_test` database is shared by `cap run` and the integration
suite, and the suite's fixtures wipe `screening_audit`. A full `uv run pytest` run during
this validation deleted the audit rows from Stages C-E. The figures for those stages
below are the console/audit values recorded when each run happened; only Stage F's
sample was regenerated and read back from the table. See Known gaps.

---

## Stage A — Offline checks

```bash
CAP_FAKE_MODEL=1 CAP_FAKE_GUARDRAILS=1 uv run pytest -q
```

122 passed at the start of the run, including the previously stale `lastfailed` entry
(`test_compliance_mw.py::test_wrap_node_does_not_audit_failure_on_interrupt`). After the
fixes below: `uv run pytest -m "not integration"` → **145 passed, 11 skipped
(`test_guardrails_live.py`, opt-in), 16 deselected**; `ruff check` and
`ruff format --check` clean.

---

## Stage B — Reach NIM

```bash
curl -s http://127.0.0.1:8000/v1/models
curl -s http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"meta/llama-3.1-8b-instruct","messages":[{"role":"user","content":"Reply with the single word: ready"}],"max_tokens":8,"temperature":0}'
```

HTTP 200 on both. Model served: `meta/llama-3.1-8b-instruct`, **`max_model_len: 4096`**.
Chat reply `"Ready"` in 0.149 s (17 prompt, 1 completion token). The first attempt, made
before the port-forward existed on this host, failed with connection refused.

---

## Stage C — Live `sanctions_screening` run

```bash
NIM_BASE_URL=http://127.0.0.1:8000/v1 uv run cap run --alert examples/alert_clear.json
```

The first runs exposed three defects (all fixed in `c76e949`, with tests in
`tests/test_structured_output.py`):

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | HTTP 400 `extra_forbidden max_completion_tokens` from the guardrail | nemoguardrails binds `max_tokens`; `langchain-openai` renames it; NIM rejects the new name | `_NimChatOpenAI` sends `max_tokens` |
| 2 | HTTP 400 on `response_format: json_schema`; every `evaluate` silently fell back to Bedrock | NIM accepts only `text`/`json_object` | `structured_output()`: NIM uses `json_mode` + schema in the system prompt; Bedrock keeps tool-use (`ChatBedrockConverse` has no `json_mode`) |
| 3 | Disposition said `meta/llama-3.1-8b-instruct` while Bedrock had answered | `dispose` used the routed primary's id | `evaluate` writes `evaluation_model` to state; `dispose` stamps it |

After the fixes, a NIM-served run: audit row `backend: nim`, `fallback_used: false`,
1,464 input / 636 output tokens, **95.6 s**. The 8B model returned
`escalate_to_analyst` at confidence 0.5 for `alert_clear.json`, which Bedrock Claude
clears at 0.97, and filled citations malformed (`chunk_id: "[document_id#chunk_id]"`,
`document_id: "ofac-sanctions-corpus#OFAC-003"`). See Known gaps.

---

## Stage D — Fallback and queue routing

```bash
# NIM unreachable (dead port) -> must fall back to Bedrock
NIM_BASE_URL=http://127.0.0.1:9/v1 uv run cap run --alert <alert>
# aml_analysis must stay on Bedrock even with NIM healthy
QUEUE=aml_analysis NIM_BASE_URL=http://127.0.0.1:8000/v1 uv run cap run --alert <alert>
```

First attempt **failed**: the input guardrail ran on the NIM model *before* the fallback
`try`, so a NIM outage crashed the run. Fixed in `c76e949`: the rail runs inside the
`try`; a connection/server error from the rail or the call triggers the fallback and the
rail is re-run on the fallback model; a `GuardrailViolationError` never falls back. Also
fixed: audit `detail.queue` now records the routed queue, not the alert's field.

After the fix: exit 0, `disposed clear`, audit `backend: bedrock`, `fallback_used: true`,
Disposition `model_id: us.anthropic.claude-sonnet-4-6`. `aml_analysis` run: Bedrock,
`fallback_used: false`.

---

## Stage E — Guardrails on live NIM

The first matrix (9 cases, rails judged by the 8B model on the full evaluate prompt)
scored **7/9**: the 8B model **allowed** a forced-disposition instruction and a raw SSN
buried in the ~2k-token prompt, 3/3 runs, but **blocked** the same content as short
messages, 3/3. Claude on Bedrock blocked every variant. Output-rail cases (legal advice,
followed injection, card number, grounded rationale) were all correct.

Fixed in `e2920ae` (see ADR-004): a deterministic SSN/card check over the whole prompt,
and the LLM input rail restricted to untrusted free text. Validated before coding: 7/7
adversarial cases blocked on the 8B model once the input is short.

```bash
NIM_BASE_URL=http://127.0.0.1:8000/v1 CAP_LIVE_GUARDRAILS=1 \
  uv run pytest tests/test_guardrails_live.py -q      # 11 passed on NIM
```

End to end on NIM: SSN in `transaction_ref` → blocked with **no model call**; injection
in `screened_name` → blocked by the NIM rail, no fallback; benign alert → disposed via
NIM. `GUARDRAIL_INPUT_BACKEND=bedrock` ran the input rail on Bedrock (4.0 s vs 1.2 s on
NIM) while `evaluate` stayed on NIM.

---

## Stage F — Cost report on real rows

Sample regenerated after the audit table was wiped, run from a script (not the test
suite), read back with:

```bash
uv run cap cost report --since 2026-11-01T23:40:00+00:00
```

Per-call rows from `screening_audit` (`step='evaluate'`):

| Alert | Queue | Backend | In tok | Out tok | Latency | Outcome |
|---|---|---|---|---|---|---|
| nim-clear-1 | sanctions_screening | nim | 1,462 | 370 | 58.9 s | escalate 0.5 |
| nim-clear-2 | sanctions_screening | nim | 1,462 | 362 | 44.9 s | escalate 0.5 |
| nim-match-1 | sanctions_screening | nim | 1,477 | 474 | 60.5 s | true_match 1.0 |
| nim-match-2 | sanctions_screening | nim | 1,477 | 474 | 60.2 s | true_match 1.0 |
| aml-clear-1 | aml_analysis | bedrock | 2,039 | 814 | 8.0 s | clear 0.97 |
| fb-clear-1 | sanctions_screening | bedrock (fallback) | 2,038 | 925 | 8.3 s | clear 0.97 |

Report aggregate (project window 30 days):

| Queue | Backend | Calls | In tok | Out tok | Latency | Observed USD |
|---|---|---|---|---|---|---|
| sanctions_screening | nim | 4 | 5,878 | 1,680 | 224.6 s | 0.1013 |
| sanctions_screening | bedrock | 1 | 2,038 | 925 | 8.3 s | 0.0200 |
| aml_analysis | bedrock | 1 | 2,039 | 814 | 8.0 s | 0.0183 |

**Reading the numbers** (rates: Bedrock $0.003/$0.015 per 1K in/out tokens; NIM
$1.624/GPU-hour, `g5.4xlarge`, amortized over call latency):

- Per call: NIM **$0.0253** (56 s, ~7.5 output tokens/s) vs Bedrock **~$0.0192** (8 s).
  Run one-at-a-time, NIM is ~1.3x more expensive per call *and* ~7x slower.
- NIM only wins with concurrency/batching: it needs roughly **1.3 concurrent calls per
  GPU** to match Bedrock's per-call price, and the pod costs $1.624/h
  (~$1,186 per 730-hour month) whether or not it is busy. Against Bedrock's ~$0.0192, the
  fixed-cost break-even is **~62k calls/month (~85/hour)** if the pod runs 24/7.
  Concurrent-throughput was **not measured** in this run.
- The latency-based NIM price counts only time spent serving. Idle GPU time is not in
  it, so it is a **lower bound** on real NIM cost at low utilization.
- The report's projection (`projected_cost_usd` ≈ $812) extrapolates a ~7-minute burst to
  30 days and should not be used for planning.
- Calls whose output the guardrail blocks consume tokens but write no `evaluate` row, so
  they are missing from the report (see Known gaps).

---

## Final gate

```bash
uv run ruff check src tests && uv run ruff format --check src tests   # clean
uv run pytest -m "not integration" -q   # 145 passed, 11 skipped, 16 deselected
```

The integration suite (`uv run pytest -m integration`) was last run mid-validation,
passing (161 total at that point); it was **not** re-run after Stage F because it wipes
the shared audit table.

---

## Known gaps carried forward

**Quality and performance**
- **NIM latency is high.** ~45-95 s and ~7.5 output tokens/s for the 8B model on one
  `g5.4xlarge` is far from "low-latency"; investigate GPU utilization, batching and
  whether the pod was cold before drawing conclusions about the NIM tier.
- **8B decision quality is weak.** It escalates a clear alert (0.5) that Claude clears
  (0.97), misread nationality, and returns malformed citations (placeholder `chunk_id`,
  combined `document_id#chunk_id` in `document_id`). `compliance_mw` does not reject
  these on the escalate path, and does not cross-check citations against the retrieved
  chunks.
- **4,096-token context.** Fine at ~1.5k input tokens; longer retrievals or guardrail
  prompts could overflow it.

**Guardrails**
- **Passport and bank-account numbers** are not covered by the deterministic check; they
  rely on the LLM rail, which the 8B model can miss. Planned enhancement recorded in
  ADR-004 (label-anchored patterns, IBAN mod-97, with false-positive testing).
- **Output rail is non-deterministic on a legitimate `true_match` rationale.** For
  `ALRT-MATCH-001` on `aml_analysis` (Claude rationale ending "The institution must
  immediately block ... and file a Blocking Report ... within 10 business days") the rail
  blocked the original run and two reruns, then allowed a third; the same text was
  allowed 3/3 in isolation on both Claude and the 8B model, and the final sentence alone
  was blocked 3/3. The rail prompt treats regulatory next steps as "action outside
  deciding this disposition", yet a grounded rationale naturally cites them. A block
  fails the run (no fallback) after the model call. Needs a prompt change and a
  retry/triage policy; the exact blocked rationale was not captured.
- **Blocked outputs are missing from cost.** The `evaluate` audit row is written only
  after the output rail passes, so a blocked call's tokens/latency are never recorded
  (the aml block above cost ~$0.02 per attempt, 3 attempts, none in the report).
- **Output rail** still receives the whole rationale; scoping it is listed in ADR-004.

**Tooling and docs**
- **The integration suite wipes the shared `cap_test` audit table**, which `cap run`
  also writes to. It destroyed Stage C-E audit evidence during this run. Give the suite
  its own database/schema or stop truncating `screening_audit` when the DSN is shared.
- `docs/aws-compliance-setup.md` still has no EKS/NIM section (`config.py` points to it);
  `.env.example` lacks `NIM_BASE_URL`, `NIM_MODEL_ID`, `NIM_API_KEY`,
  `LLM_ROUTING_POLICY`, `BEDROCK_FALLBACK_ENABLED`, `NIM_GPU_HOURLY_RATE`,
  `GUARDRAIL_INPUT_BACKEND`; the README's "Not yet" list is not updated for Week 4.
- The `port-forward` access path is dev-only; the production path (internal LB/ingress,
  auth, TLS) is undefined.
- `verify_chain` takes rows, not a connection, and `cap audit verify` works on exported
  S3 batches; neither was run against the Week 4 rows.
- On the `insufficient_data` rule-overlay path the `evaluate` audit row and Disposition
  still carry the routed `ctx.model_id` although no model was called.
- Carried forward from earlier weeks: `cap_app` least-privilege DB role, RDS/S3/CloudTrail
  as IaC, `AuditRecord` not exposing `supersedes_id`.
