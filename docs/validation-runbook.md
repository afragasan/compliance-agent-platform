# Week 1 validation runbook

Executes the plan in `testing-plan.md`. Decisions for this run:

- **Postgres:** a **dev RDS for PostgreSQL** instance. Refresh `aws sso login`, then export a
  reachable `DATABASE_URL` (use a dedicated `cap_test` database on that instance).
- **Manual E2E LLM:** **both** — a deterministic fake-model switch (`CAP_FAKE_MODEL`) for
  offline runs now, then repeat against real Bedrock once SSO is back.

Stages 1–2 gate the milestone; stage 3 is the real-Bedrock confirmation.

---

## Stage 0 — Environment prep

| # | Step | Command / detail | Done when |
|---|---|---|---|
| 0.1 | Install dev deps | `uv sync --dev` | `uv run pytest --version` works |
| 0.2 | Refresh AWS creds | `aws sso login` | `aws sts get-caller-identity` returns the dev account |
| 0.3 | Create the test DB on dev RDS | `psql "$RDS_ADMIN_URL" -c 'CREATE DATABASE cap_test;'` | DB exists |
| 0.4 | Export test env | `export DATABASE_URL='postgresql://USER:PASS@<rds-endpoint>:5432/cap_test'` `export AWS_REGION=us-east-1` | `psql "$DATABASE_URL" -c 'select 1'` succeeds from this host |
| 0.5 | Confirm RDS reachability | if the endpoint is VPC-private and unreachable here, run stages 2–3 from a bastion / CloudShell in-VPC, or temporarily allow this host's IP in the RDS security group | connection succeeds |

If 0.5 cannot be satisfied, fall back to `postgresql-wheel` (throwaway local server) for
stage 2 and defer RDS verification to the Week 2 infra sprint.

---

## Stage 1 — No-DB validation (do first, no AWS needed)

### 1.1 Static checks
```bash
uv run python -m compileall -q src
uv run ruff check src            # add ruff to the dev group if not present
```

### 1.2 Add the fake-model switch — DONE

- `llm.py`: when the shell env var `CAP_FAKE_MODEL` is set, `get_chat_model()` returns a
  stub whose `.with_structured_output(EvaluationResult)` yields a canned `EvaluationResult`.
  No network. Unknown value → `ValueError` listing the valid names. Default (unset) path is
  unchanged and still returns `ChatBedrockConverse` (no call until `.invoke`).
- Valid values: `clear` (0.95), `clear_low` (0.40 → routes to escalate), `true_match`
  (0.93, carries one `matched_entities`), `escalate` / `escalate_to_analyst` (0.60),
  `insufficient_data` (0.90).
- This is what makes `cap run` / `cap resume` and `test_contract.py` runnable offline.
- Verified: fake profiles, bad-value error, in-memory graph run with no `model=` injection
  (`CAP_FAKE_MODEL=clear` → `clear`; `clear_low` → parked at `escalate`), default path
  returns the real Bedrock client. `ruff check` / `ruff format --check` clean.

### 1.3 Unit tests (`tests/`, no DB, no network) — DONE (34 tests)

| File | Assertions |
|---|---|
| `test_disposition_schema.py` (14) | valid `Disposition` for each agent/analyst `DispositionType`; analyst carries no `confidence`; `analyst_id` required for `decided_by=analyst`; `model_id` required for agent; agent `clear`/`true_match` require `confidence`; `true_match` requires ≥1 `matched_entities`; confidence range + non-empty rationale; `mode="json"` round-trip equality. |
| `test_routing.py` (8) | `make_route_after_evaluate` table: `true_match`@{0.99,0.10}→escalate, `escalate_to_analyst`→escalate, `clear`@{0.90, threshold}→dispose, `clear`@0.50→escalate, `insufficient_data`@0.99→dispose / @0.50→escalate. |
| `test_nodes.py` (7) | `enrich`: name/DOB/nationality features + `insufficient_data` flag (DOB missing, hit unresolved) via mock adapters; `evaluate`: rule overlay returns `insufficient_data` **without** invoking the model, else invokes it; `dispose`: agent-path (`model_id`/`prompt_version` set, `analyst_id` None) vs analyst-path (resolution mapped, `confidence` None, falls back to `evaluation.matched_entities`). Fake `audit_conn`. |
| `test_graph_inmemory.py` (5) | `build_graph(checkpointer=InMemorySaver(), model=fake)` — clear→disposed as agent; true_match→parked at `escalate`, interrupt payload checked; **rebuild graph on same saver + `Command(resume=...)`**→disposed as analyst; missing-DOB→`insufficient_data` via overlay; `get_state_history` yields ≥5 checkpoints. `filterwarnings("error::UserWarning")` guards the checkpoint-serialization regression. |

### 1.4 Gate — PASS
```bash
uv run pytest tests/ -m "not integration" -W error::UserWarning   # 34 passed
uv run ruff check src tests && uv run ruff format --check src tests
```
Stage 1 complete.

---

## Stage 2 — Postgres integration (dev RDS)

### 2.1 conftest — DONE

- `tests/conftest.py`:
  - `load_dotenv()` at import so `.env` (DATABASE_URL, AWS_REGION) reaches `os.environ`.
  - `_resolved_dsn()` → `DATABASE_URL`, else `get_settings().resolved_dsn()` **only if**
    `DB_SECRET_ARN` is set (never the app's localhost default).
  - `pytest_collection_modifyitems` skips every `@pytest.mark.integration` test when
    `_resolved_dsn()` is None → `pytest` with no DB still runs the unit suite.
  - `pg_dsn` (session), `_schema_ready` (session, runs `open_checkpointer` once to create
    checkpoint + audit schema), `clean_db` (function, `TRUNCATE checkpoints,
    checkpoint_blobs, checkpoint_writes, screening_audit` — leaves `checkpoint_migrations`;
    data left in place after the test for inspection), `load_alert` (reads `examples/*.json`).
- `integration` marker already registered in `[tool.pytest.ini_options]` (Stage 1.1).
- `tests/test_integration_harness.py` (3 tests): `clean_db` leaves tables empty; audit
  insert works + `DELETE` blocked by the trigger; `load_alert` loads a fixture.
- Verified: with `.env` → 37 passed (34 unit + 3 integration); no `.env`/`DATABASE_URL`
  → 34 passed, 3 skipped.

### 2.2 Integration tests

| File | Assertions |
|---|---|
| `test_checkpoint_resume.py` | With `open_checkpointer(settings)` + fake model against RDS: **(a)** clear alert via `invoke` → `status: disposed`, `decided_by: agent`; **(b)** true_match alert → `status: escalated`, `interrupt` payload has the analyst fields; **(c) simulated crash:** exit the `with open_checkpointer` block (drops all connections + graph), open a fresh `open_checkpointer` + `build_graph`, `invoke({"thread_id":..., "resume":{...}})` → `status: disposed`, `decided_by: analyst`, `matched_entities` non-empty; **(d)** `SELECT step,actor,event FROM screening_audit WHERE alert_id=...` shows `intake, enrich, evaluate, escalate(interrupt_raised), escalate(resumed), dispose`; **(e)** `graph.get_state_history(cfg)` yields ≥6 checkpoints ending at `next==()`. |
| `test_contract.py` | `invoke({"alert": <clear>})` shape `{status, thread_id, disposition}`; `invoke({"alert": <true_match>})` → `{status:"escalated", interrupt}`; `invoke({"thread_id": tid, "resume": {...}})` closes it; a `context` object with `.session_id` overrides `alert.alert_id` as `thread_id`. |
| `test_audit_immutable.py` | After an `INSERT`, `UPDATE screening_audit SET actor='x'` raises (trigger `screening_audit is append-only`); `DELETE` raises; `INSERT` + `SELECT` still succeed. (Grant-level `INSERT/SELECT`-only is verified in Week 2 when the least-privilege `cap_app` role exists; on dev RDS we connect as the master user, so only the trigger is exercised here.) |

### 2.3 Gate
```bash
uv run pytest tests/ -m integration
```

### 2.4 Manual offline E2E (fake model, against RDS)
```bash
export CAP_FAKE_MODEL=clear      && uv run cap run --alert examples/alert_clear.json
export CAP_FAKE_MODEL=true_match && uv run cap run --alert examples/alert_true_match.json   # -> escalated
uv run cap resume --thread ALRT-MATCH-001 --resolution true_match --analyst-id A123 --rationale "confirmed DOB+nationality"
export CAP_FAKE_MODEL=insufficient && uv run cap run --alert examples/alert_insufficient.json
```
Between the `run` and `resume` above, kill the shell / open a new one — `resume` must still
complete from RDS state. Then:
```bash
psql "$DATABASE_URL" -c "select step,actor,event,model_id,created_at from screening_audit where alert_id='ALRT-MATCH-001' order by created_at;"
```

---

## Stage 3 — Real Bedrock E2E

Prereq: `aws sso login` valid; execution role/user has `bedrock:InvokeModel` for
`$BEDROCK_MODEL_ID`; model access enabled in the region.

```bash
unset CAP_FAKE_MODEL
uv run cap run --alert examples/alert_clear.json          # expect: clear,  decided_by agent
uv run cap run --alert examples/alert_true_match.json     # expect: escalated
uv run cap resume --thread ALRT-MATCH-001 --resolution true_match --analyst-id A123 --rationale "..."
uv run cap run --alert examples/alert_insufficient.json   # expect: insufficient_data (rule overlay, no model call)
```
Confirm in `screening_audit`: the `evaluate` row for the clear/true_match runs has
`llm_request`/`llm_response` populated and the real `model_id`.

---

## Stage 4 — Close-out

1. Resolve the three open questions in `testing-plan.md` (confidence threshold;
   insufficient_data auto-dispose vs escalate; env Bedrock model id) — record the answers
   in `config.py` defaults / a short ADR note if policy changes.
2. Add `[tool.pytest.ini_options]` (asyncio mode, `integration` marker) and a one-line CI
   note (`pytest -m "not integration"` on every push; integration on demand).
3. Commit: `test: Week 1 validation suite + fake-model switch`.
4. Update the memory note / `README` "Testing" section to point here and record results.

## Known gaps carried to Week 2

- Least-privilege `cap_app` DB role + grant-level immutability check.
- RDS provisioning (VPC, SG, KMS, PITR) as IaC.
- `testcontainers` path for CI once a container runtime is available.
