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

### 2.2 Integration tests — DONE (11 tests, dev RDS `cap_test`)

The crash/restart is modelled by the fact that `contract.invoke()` opens **and closes**
its own `open_checkpointer` context per call — a second `invoke()` for the same
`thread_id` rebuilds the checkpointer + graph from the DSN and resumes purely from
Postgres. LLM = `CAP_FAKE_MODEL` stub (no Bedrock).

| File | Assertions |
|---|---|
| `test_checkpoint_resume.py` (2) | **clear**: `invoke` → `disposed`/`agent`, audit trail exactly `intake, enrich, evaluate, dispose`. **true_match**: (b) `invoke` → `escalated`, interrupt payload carries `alert_id`/`agent_recommendation`/`agent_rationale`; (c) fresh `invoke({thread_id, resume})` → `disposed`/`analyst`, `analyst_id`, `confidence is None`, `matched_entities` non-empty (fell back to evaluation's); (d) audit trail — `evaluate` exactly once, `escalate/interrupt_raised` present (may repeat on replay), `escalate/resumed` exactly once, ends `dispose/disposition` by `analyst`, `resumed` before `disposition`; (e) fresh `build_graph` + `get_state_history` ≥6 checkpoints, newest `next == ()` and terminal disposition. |
| `test_contract.py` (3) | `invoke({"alert": clear})` → exactly `{status, thread_id, disposition}`, `disposed`; `invoke({"alert": true_match})` → exactly `{status, thread_id, interrupt}`, `escalated`, then `invoke({thread_id, resume: clear})` → `disposed`/`analyst`; `context.session_id` overrides `alert_id` as `thread_id` and the checkpoint row is keyed by the session id. |
| `test_audit_immutable.py` (3) | after a real run: `UPDATE screening_audit` raises `screening_audit is append-only`; `DELETE` raises; `SELECT` works, and a correction row (new insert with `supersedes_id`) succeeds. (Grant-level `INSERT/SELECT`-only is a Week 2 item — the `cap_app` least-privilege role does not exist yet; here we connect as the RDS master user so only the trigger is exercised.) |

Plus `test_integration_harness.py` (3) from 2.1.

### 2.3 Gate — PASS
```bash
uv run pytest -m integration            # 11 passed
uv run pytest -q -W error::UserWarning  # 45 passed (34 unit + 11 integration)
```

### 2.4 Manual offline E2E (fake model, against RDS) — DONE

```bash
set -a; source .env; set +a
CAP_FAKE_MODEL=clear             uv run cap run --alert examples/alert_clear.json
CAP_FAKE_MODEL=true_match        uv run cap run --alert examples/alert_true_match.json   # -> escalated
# --- separate process (the "crash") ---
CAP_FAKE_MODEL=true_match        uv run cap resume --thread ALRT-MATCH-001 \
    --resolution true_match --analyst-id A123 --rationale "confirmed DOB+nationality match"
CAP_FAKE_MODEL=insufficient_data uv run cap run --alert examples/alert_insufficient.json
```
`CAP_FAKE_MODEL` is a shell export; valid names are in `.env.example`
(`insufficient_data`, not `insufficient`). No `psql` on this host — inspect with a
`psycopg` one-liner instead.

Observed (2026-09-09, `cap_test`):

| Alert | Result | audit rows | checkpoints |
|---|---|---|---|
| `alert_clear` | `disposed` / `clear` / agent, conf 0.95 | 4 (`intake,enrich,evaluate,dispose`) | 6 |
| `alert_true_match` (run) | `escalated`; parked with 4 audit rows / 5 checkpoints | — | — |
| `alert_true_match` (resume, new process) | `disposed` / `true_match` / **analyst** `A123`, `confidence null`, `model_id null` | 7 total | 7 |
| `alert_insufficient` | `disposed` / `insufficient_data` / agent; rationale is the enrich **rule-overlay** text, not the fake model's → overlay short-circuits the model | 4 | 6 |

`ALRT-MATCH-001` audit trail (note the two timestamps 18s apart = the process boundary):
```
intake    system  node_completed     21:05:49
enrich    system  node_completed     21:05:49
evaluate  agent   node_completed     21:05:49   model_id=<bedrock id>
escalate  agent   interrupt_raised   21:05:49
escalate  agent   interrupt_raised   21:06:07   <- escalate node re-runs on replay
escalate  analyst resumed            21:06:07
dispose   analyst disposition        21:06:07
```
Crash-recovery confirmed: process 2 exited fully with the alert parked in Postgres;
process 3 (fresh) resumed from RDS state and disposed as analyst.

### Stage 2 — COMPLETE

Carried to Stage 4 open items: with `CAP_FAKE_MODEL` set, an **agent** `Disposition`
still records the real `model_id` from settings (rationale text does say `[CAP_FAKE_MODEL=…]`).
Harmless (fake never runs in prod) but consider forcing `model_id="fake:<profile>"`.

---

## Stage 3 — Real Bedrock E2E — DONE (2026-09-10)

Model: **`us.anthropic.claude-sonnet-4-6`** (US cross-region inference profile — the
account has SSO `AdministratorAccess`, account `784137772067`, region `us-east-1`).
Confirmed `ACTIVE` via `aws bedrock list-inference-profiles`; raw
`aws bedrock-runtime converse` and the app's `get_chat_model()` / structured-output path
both smoke-tested before the run. `BEDROCK_MODEL_ID` updated in `.env`, `.env.example`,
and the `Settings` default in `config.py`.

```bash
unset CAP_FAKE_MODEL
uv run cap run --alert examples/alert_clear.json          # -> clear,  decided_by agent
uv run cap run --alert examples/alert_true_match.json     # -> escalated
uv run cap resume --thread ALRT-MATCH-001 --resolution true_match --analyst-id A123 --rationale "..."
uv run cap run --alert examples/alert_insufficient.json   # -> insufficient_data (rule overlay, no model call)
```

Results, all against `cap_test`:

| Alert | Disposition | Notes |
|---|---|---|
| `alert_clear` | `clear`, agent, conf 0.98 | model correctly ruled out the weak 0.41-score OFAC hit on name/DOB/nationality; still returned `matched_entities` (candidate it considered, not a match) — allowed by the schema, only `true_match` requires it non-empty |
| `alert_true_match` | escalated at conf 0.99 → resumed as **analyst** `A123` | real model reasoning cited exact name/DOB/nationality match + the adverse-media hit + high risk rating |
| `alert_insufficient` | `insufficient_data`, agent | rule-overlay rationale text; **no model call** |

Verified in `screening_audit`:
```
evaluate  ALRT-CLEAR-001   model=us.anthropic.claude-sonnet-4-6  llm_request=yes  llm_response=yes
evaluate  ALRT-MATCH-001   model=us.anthropic.claude-sonnet-4-6  llm_request=yes  llm_response=yes
evaluate  ALRT-INSUF-001   model=us.anthropic.claude-sonnet-4-6  llm_request=no   llm_response=no   <- overlay, not called
```
`ruff` + full `pytest` (45 passed) re-run clean after the `config.py` default change.
`cap_test` truncated after the run.

### Stage 3 — COMPLETE

---

## Stage 4 — Close-out

1. Resolve the three open questions in `testing-plan.md` (confidence threshold;
   insufficient_data auto-dispose vs escalate; env Bedrock model id) — record the answers
   in `config.py` defaults / a short ADR note if policy changes.
2. `[tool.pytest.ini_options]` (asyncio mode, `integration` marker) — DONE in Stage 1.1.
   Still to add: a one-line CI note (`pytest -m "not integration"` on every push;
   integration on demand against a CI Postgres service).
3. Commits: done incrementally on `validation/week1-stage1`
   (`9ff66f9`, `f738425`, `5a52d7c`, `00c07f1`, + Stage 2.4). Open a PR / fast-forward `main`.
4. Update `README` "Testing" section to point here and record results.
5. Fake-model `model_id` cosmetics: when `CAP_FAKE_MODEL` is set, force the agent
   `Disposition.model_id` / audit `model_id` to `fake:<profile>` instead of the real
   settings value.

## Known gaps carried to Week 2

- Least-privilege `cap_app` DB role + grant-level immutability check.
- RDS provisioning (VPC, SG, KMS, PITR) as IaC.
- `testcontainers` path for CI once a container runtime is available.
