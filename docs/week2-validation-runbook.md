# Week 2 validation runbook

Executes the plan reviewed and approved at the start of Week 2. Decisions for this
run:

- **S3 Object Lock retention:** `AUDIT_RETENTION_DAYS=30` (1 month) — an explicit
  **POC default**, exposed as a `Settings` field so production deployments set it
  per their own regulatory retention requirement.
- **AWS provisioning:** manual, by the user, from exact commands in
  `docs/aws-compliance-setup.md` — same pattern as RDS/Bedrock in Week 1. No code
  creates AWS resources.
- Branch: `week2-complaince-mw-and-auditchain`, off `main` (Week 1 already merged
  via PR #1).

---

## Stage A — Hash-chain the `screening_audit` log — DONE (`23aa620`)

- `audit/schema.sql`: additive, idempotent `ALTER TABLE ... ADD COLUMN IF NOT
  EXISTS prev_hash TEXT` / `record_hash TEXT`. Both nullable at the DB level —
  pre-existing rows have neither.
- `audit/log.py`: `write_audit()` takes `pg_advisory_xact_lock`, reads the current
  chain tail, computes `record_hash = sha256(business fields + created_at +
  prev_hash)` before inserting (`created_at` generated in Python, not DB `now()`,
  so it's known before the row exists and can be hashed). New `verify_chain(rows,
  start_hash=GENESIS)` recomputes and reports the first break, if any.
- `tests/fakes.py`: `FakeAuditConn` replaces the two duplicated no-op `_FakeConn`s
  in `test_nodes.py`/`test_graph_inmemory.py` — extended to answer the chain-tail
  `SELECT`.
- `tests/test_audit_chain.py` (new, 11 tests): hash determinism, chain linking,
  genesis handling, tampered-field detection, deleted-row-gap detection, and
  explicit tolerance of `escalate`'s known replay-duplicate rows (order-based, not
  step-based chain).

**Gate:**
```bash
uv run ruff check src tests && uv run ruff format --check src tests   # clean
uv run pytest -m "not integration" -q                                  # 45 passed
uv run pytest -m integration -q                                        # 11 passed
```

**Manual real-DB confirmation** (`cap_test`, fake model):
```bash
CAP_FAKE_MODEL=clear uv run cap run --alert examples/alert_clear.json
```
4 rows written (`intake, enrich, evaluate, dispose`), `verify_chain()` over them ->
`ok=True`.

---

## Stage B — `compliance_mw` package + citation-mandatory enforcement — DONE (`577d59e`)

- New `compliance_mw` package: `wrap_node(name, fn, ctx)` wraps all 5 nodes at the
  single choke point where they're added (`graph/builder.py`). Writes a new
  `event="node_failed"` audit row on a genuine exception (previously unaudited);
  correctly re-raises `langgraph.errors.GraphBubbleUp` so `escalate`'s
  `interrupt()` is never misrecorded as a failure (verified against the installed
  `langgraph` package, not assumed).
- `compliance_mw.citation.check_dispose_citation`: `clear`/`true_match`
  dispositions with empty `evidence` are rejected — writes a chained
  `event="compliance_blocked"` row, then raises `ComplianceViolationError`.
  `insufficient_data` is exempt.
- `runtime/contract.py`: catches the violation, returns
  `{"status": "blocked", "thread_id", "violation": {...}}`. `cap`'s existing
  exit-code logic already treats non-`disposed`/`escalated` statuses as failure.
- One-line fix in `make_dispose`'s analyst branch: the evidence fallback
  (`_fallback_evidence`) now applies on both the agent and analyst paths — it
  previously only applied to the agent path, an accidental asymmetry.
- `tests/test_compliance_mw.py` (new, 10 tests) + extended `test_graph_inmemory.py`
  (+1) and `test_contract.py` (+1, integration).

**Gate:**
```bash
uv run pytest -q   # 68 passed (unit + integration against cap_test)
```

**Manual confirmation** — an alert with no watchlist hits (-> no enrichment
candidates -> no fallback evidence), `CAP_FAKE_MODEL=clear`:
```
{"status": "blocked", "thread_id": "ALRT-NOEV-001",
 "violation": {"disposition_type": "clear", "reason": "clear disposition has no evidence citations"}}
```
`cap run` exit code `1`. Real `cap_test` audit trail for that alert:
`intake, enrich, evaluate` (`node_completed`) -> `dispose/disposition` ->
`dispose/compliance_blocked`. `verify_chain()` over all 5 rows -> `ok=True`
(the block does not break the chain — it's one more correctly-linked row).

---

## Stage C — S3 Object Lock export + CloudTrail provisioning docs — DONE (`ecd22c5`, `36899ba`)

- `audit/s3_export.py` (new): `export_batch()` writes new `screening_audit` rows as
  JSONL to S3, one manifest per batch recording `{first_id, last_id, row_count,
  chain_head_hash, prev_manifest_key}`. The next export resumes from
  `last_id + 1` and verifies its first new row's `prev_hash` matches the previous
  manifest's `chain_head_hash` (via `verify_chain`'s new `start_hash` parameter)
  *before* writing anything — refuses rather than silently skipping a gap.
  `verify_export()` re-derives the whole chain from the exported copies alone,
  independent of the live DB, catching both a tampered row and a tampered
  manifest.
- `object_lock_configuration(settings)`: builds the bucket's Object Lock body
  (`COMPLIANCE` mode, `Settings.audit_retention_days`) — `cap audit bucket-config`
  prints the exact `aws s3api put-object-lock-configuration` command from it, so
  the provisioned bucket can't drift from app config.
- `cap audit export` / `cap audit verify` / `cap audit bucket-config` CLI
  subcommands.
- `moto[s3]` added as a dev dependency; `tests/test_s3_export.py` (new, 8 tests,
  no network): export-from-genesis, idempotent no-op, resume from last export,
  multi-batch verify, tampered-row detection, tampered-manifest-hash detection,
  refusal when the live table no longer matches the last export.
- `docs/aws-compliance-setup.md` (new): manual provisioning — bucket creation with
  Object Lock enabled at creation, versioning, COMPLIANCE-mode retention, IAM
  policy creation *and attachment* (dedicated `cap-audit-exporter` IAM user for
  this POC), a separate CloudTrail bucket + trail with
  `--enable-log-file-validation`, and a verification checklist including
  confirming a delete actually gets rejected by Object Lock.

**Gate:**
```bash
uv run pytest tests/test_s3_export.py -q   # 8 passed (moto, no network)
uv run pytest -q                            # 76 passed
```

**Operational finding, fixed within this stage:** running `cap audit export`
against `cap_test` initially raised `RuntimeError: refusing to export: chain
continuity broken`. Root-caused (not a chain bug) to `cap_test` being shared
between the automated test suite (`clean_db` truncates it before every
integration test) and manual `cap` runs — a stray raw-SQL test row, and later a
genuine truncate-between-exports gap, both correctly triggered the continuity
check. Fixed the avoidable half (`test_integration_harness.py` now inserts
through `write_audit`, not raw SQL — see Stage D); documented the structural half
("`cap_test` is shared with the automated test suite" in
`docs/aws-compliance-setup.md`) with the remedy (bump `AUDIT_EXPORT_PREFIX` to
start a fresh chain; old prefix's objects remain under Object Lock regardless).
Confirmed working end-to-end afterward:
```bash
uv run cap audit export   # {"first_id": ..., "last_id": ..., "row_count": N, "manifest_key": "..."}
uv run cap audit verify   # {"ok": true, "broken_at_id": null, "reason": null}
```

**Still to be run by the user** (manual AWS provisioning, per the agreed division
of work): the `aws s3api` / `aws iam` / `aws cloudtrail` commands in
`docs/aws-compliance-setup.md` against the real account, followed by the doc's
verification checklist (including the delete-is-rejected check).

---

## Stage D — ADR-002 + integration tests + close-out — DONE (`4bfcee0` + this stage)

- `ADR/002-audit-chain-design.md` (new): hash-chain design (columns, what's
  hashed, advisory-lock concurrency, global chain scope, replay-duplicate
  tolerance, trust boundary), node-wrapping middleware + the `GraphBubbleUp`
  correctness detail, citation enforcement mechanics, S3 Object Lock (Retention
  Lock framing), CloudTrail, and Consequences — including gaps carried forward
  (see below). Matches ADR-001's structure.
- `test_integration_harness.py`: the immutability-trigger test now inserts its row
  through `write_audit` instead of raw SQL, so the row it leaves behind in the
  shared `cap_test` database is properly chained (trigger coverage is unaffected —
  the trigger fires regardless of how a row was inserted).
- `test_audit_immutable.py` (+1 integration test): after a real `disposed_run`,
  every row has non-null `prev_hash`/`record_hash`, and `verify_chain()` over them
  returns `ok=True` — the first assertion of chain correctness against a real
  Postgres round-trip (JSONB read-back, `TIMESTAMPTZ` read-back), not just the
  `FakeAuditConn` double used everywhere else.

**Final gate:**
```bash
uv run ruff check src tests && uv run ruff format --check src tests   # clean
uv run pytest -m "not integration" -q   # 64 passed
uv run pytest -m integration -q         # 13 passed
uv run pytest -q                        # 77 passed
```

---

## Known gaps carried forward (not silently absorbed into Week 2's scope)

- **`write_audit`/`AuditRecord` don't expose `supersedes_id`.** The append-only
  correction pattern from ADR-001 still has to bypass `write_audit` (raw SQL,
  exercised only by `test_insert_and_select_and_correction_row_work`), so a
  correction row is not yet chained. Not fixed now to avoid changing the hashed
  payload shape mid-week (would invalidate already-computed hashes for a schema
  change with no forcing production need yet).
- **Fake-model `model_id` cosmetics** (carried from Week 1): with `CAP_FAKE_MODEL`
  set, an agent `Disposition.model_id` still records the real configured Bedrock
  model id rather than `fake:<profile>`.
- **Least-privilege `cap_app` DB role + grant-level immutability check**, and
  **RDS/S3/CloudTrail provisioning as IaC** (carried from Week 1's own carried-
  forward list) — still manual/documented, not automated.
- **`cap_test` is a shared dev database.** Running the test suite between manual
  `cap audit export` demos requires bumping `AUDIT_EXPORT_PREFIX` (documented in
  `docs/aws-compliance-setup.md`) — a real operational constraint on this project's
  current dev setup, not something code can safely paper over without weakening
  the chain's guarantee.
