# Week 1 testing plan (follow-up day)

Code landed without tests by agreement ("code today, plan for testing tomorrow"). This
is the checklist for the follow-up.

## Harness

- `docker compose up -d postgres` for local integration tests.
- `testcontainers[postgres]` for CI (already in the `dev` group).
- Bedrock is not hit in tests: pass a fake chat model via `build_graph(model=...)`.

## Unit tests (no DB, no network)

| File | Covers |
|---|---|
| `tests/test_disposition_schema.py` | `Disposition` validator: each `DispositionType`; `analyst_id` required for `ANALYST`; `model_id` + `confidence` required for `AGENT` clear/true_match; `true_match` requires `matched_entities`. |
| `tests/test_routing.py` | `route_after_evaluate` truth table: `true_match`/`escalate_to_analyst` → escalate; `clear`/`insufficient_data` with confidence ≥ / < threshold → dispose / escalate. |
| `tests/test_nodes.py` | `enrich` match-feature computation and `insufficient_data` flagging with mock adapters; `evaluate` rule overlay; `dispose` builds agent vs analyst disposition. |

## Integration tests (Postgres via testcontainers)

| File | Covers |
|---|---|
| `tests/test_checkpoint_resume.py` | Full graph with a fake model: (1) clear alert → `status: disposed`; (2) true_match → `status: escalated`, interrupt persisted; (3) **resume after simulated restart** — discard the graph object, rebuild `build_graph` from a fresh `open_checkpointer` on the same DSN, `Command(resume=...)`, assert final disposition `decided_by == analyst`; (4) `screening_audit` has the expected rows; (5) `graph.get_state_history` enumerates every checkpoint. |
| `tests/test_contract.py` | `invoke({"alert": ...})` and `invoke({"thread_id": ..., "resume": ...})` return the documented shapes; `context.session_id` overrides thread id. |
| `tests/test_audit_immutable.py` | `UPDATE`/`DELETE` on `screening_audit` raise; `INSERT`/`SELECT` succeed. |

## Manual end-to-end (real Bedrock)

1. `uv run cap run --alert examples/alert_clear.json` → `clear`, `decided_by: agent`.
2. `uv run cap run --alert examples/alert_true_match.json` → `escalated`.
3. `uv run cap resume --thread ALRT-MATCH-001 --resolution true_match --analyst-id A123 --rationale "..."` → `true_match`, `decided_by: analyst`.
4. `uv run cap run --alert examples/alert_insufficient.json` → `insufficient_data`.
5. Kill the process between steps 2 and 3; step 3 still completes (state from Postgres).
6. `SELECT step, actor, event, model_id FROM screening_audit WHERE alert_id = 'ALRT-MATCH-001' ORDER BY created_at;`

## Open questions to confirm

- Confidence threshold value (`CLEAR_CONFIDENCE_THRESHOLD`, currently 0.85).
- Whether `insufficient_data` should ever auto-dispose or always escalate (currently: agent
  disposes it when confident, i.e. the rule overlay path disposes directly).
- Bedrock model id for the environment.
