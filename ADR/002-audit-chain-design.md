# ADR 002 — Hash-chained audit + citation-mandatory compliance middleware

- **Status:** Accepted
- **Date:** 2026-09-22
- **Deciders:** Compliance Agent Platform team
- **Scope:** The `sanctions_screening` queue agent (Week 2). Hardens the audit trail
  established in [ADR-001](001-why-langgraph.md); does not replace it.

## Context

ADR-001 established `screening_audit` as the compliance system-of-record: an
append-only table protected by a `BEFORE UPDATE OR DELETE` trigger, with the
application DB role granted `INSERT, SELECT` only. That ADR is explicit that this
immutability is **trigger- and grant-based, not cryptographic** — a party with
elevated DB access (or a backup/restore that silently drops rows) can delete a row
without the trigger noticing, because `DELETE` on a *different, unprivileged*
connection is what the trigger blocks, not the absence of the row afterward.

Two concrete gaps existed going into Week 2, confirmed by reading the code rather
than assumed:

1. **No tamper-evidence for deletion or reordering.** Nothing detected a missing or
   reordered row; an auditor could only trust that rows *present* in the table
   hadn't been edited in place.
2. **No citation enforcement.** `Disposition.evidence` (`schemas/disposition.py`)
   existed as a field but nothing required it to be non-empty — not even for
   `true_match`. An agent could output a disposition citing no source data at all.

This ADR covers the design closing both gaps, plus the S3 Object Lock export and
CloudTrail settings that carry the guarantee outside the live database.

## Decision

Add a `compliance_mw` package that wraps every graph node, a SHA-256 hash chain on
`screening_audit`, and a citation-mandatory policy check — all additive to ADR-001's
trigger, not a replacement for it.

### Hash-chain design

Two nullable columns added to `screening_audit` via an idempotent migration
(`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, since `ensure_audit_schema` re-runs
`schema.sql` on every process start):

- `prev_hash` — the previous row's `record_hash` (or a fixed genesis constant for
  the first row ever chained).
- `record_hash` — `sha256` of this row's business fields (`alert_id`, `thread_id`,
  `step`, `actor`, `event`, `payload_hash`, `llm_request`, `llm_response`,
  `model_id`, `prompt_version`, `detail`) plus `created_at` and `prev_hash`,
  canonically serialized (`json.dumps(..., sort_keys=True)`, the same pattern
  already used by `hash_payload`).

Both columns are nullable **at the DB level**, deliberately: rows written before
this migration (and any row written by raw SQL rather than `write_audit` — see
Consequences) have neither value, and a `NOT NULL`/`CHECK` constraint would break
against that pre-existing data. Non-null-ness for every *new* row is enforced only
in application code, inside `write_audit`.

**Computed in Python, inside `write_audit`, not a DB trigger.** `write_audit` takes
a transaction-scoped Postgres advisory lock (`pg_advisory_xact_lock`) before reading
the current chain tail (`SELECT record_hash ... ORDER BY id DESC LIMIT 1`), so two
concurrent writers can't both read the same tail and produce two rows claiming the
same `prev_hash`. `created_at` is generated in Python (`datetime.now(timezone.utc)`)
rather than left to the column's `DEFAULT now()`, specifically so its value is known
before the row exists and can be included in the hash — otherwise the timestamp
would be the one business field an out-of-band edit could alter without invalidating
anything.

**Chain scope: global and monotonic**, ordered by `screening_audit.id` — one ledger
across every alert, not one chain per alert. A per-alert chain cannot detect an
entire alert's row set being deleted (nothing else references it); a global chain
can, because the next alert's first row still points at the deleted alert's last
hash.

**Order-based, not step-based — tolerates known replay-duplicates.** ADR-001
already documents that `escalate`'s pre-`interrupt()` audit call re-executes when
LangGraph replays that node on resume, producing two `event="interrupt_raised"` rows
for the same alert. `verify_chain` has no notion of "this step already happened"; it
only checks that each row's `prev_hash` matches the actual previous row's
`record_hash`. Two rows with identical `step`/`event` verify fine as long as each
links correctly — this is deliberate, not an oversight, and is covered by
`tests/test_audit_chain.py::test_verify_chain_tolerates_replay_duplicate_rows`.

**Trust boundary.** Because chaining is application code, not a `BEFORE INSERT`
trigger, a party holding write credentials could in theory bypass `write_audit` and
insert a row with a fabricated `prev_hash`/`record_hash`. This is an accepted
tradeoff: ADR-001's trigger — unaffected by this ADR — already fully blocks
in-place tampering of *existing* rows, which is the strongest guarantee available
and doesn't depend on trusting application code at all. What the hash chain adds is
distinct: (a) proving no row was deleted or reordered out from under a live table
(any gap breaks the next row's link), and (b) making an **exported copy
self-verifying without needing to trust the live database at all** — the property
the S3 export below depends on. If a stronger threat model is needed later, the same
columns support moving the computation into a `BEFORE INSERT` trigger with no change
to `write_audit`'s external interface.

### Node-wrapping middleware (`compliance_mw`)

`compliance_mw.wrap_node(name, fn, ctx)` wraps every node at the one place all five
are already added identically (`graph/builder.py`'s `add_node` calls) — no change to
`nodes.py`'s bodies. It adds two things that don't exist without it:

- **`node_failed` audit row on a genuine exception.** Today, a node crash leaves no
  trace at all — audit writes only happen inside a node body on success. The wrapper
  writes `event="node_failed"` with the exception repr before re-raising.
- **A per-node post-hook registry**, currently just `"dispose": check_dispose_citation`.

It deliberately does **not** add a coarse `node_entered`/`node_exited` event for
every node: each node already writes its own fine-grained success audit, and
duplicating that at the wrapper level would double audit volume for no new
compliance value.

**Correctness detail that shapes the whole design:** `interrupt()` (used by
`escalate` for HITL) works by raising `langgraph.errors.GraphInterrupt`, a subclass
of `GraphBubbleUp`, which LangGraph itself catches to suspend the graph — confirmed
by inspecting the installed package rather than assumed. `wrap_node` re-raises
`GraphBubbleUp` *before* its generic failure-audit branch; without that, every
escalation would be misrecorded as a node failure.

### Citation-mandatory enforcement

`compliance_mw.citation.check_dispose_citation` runs as the `dispose` post-hook.
`clear` and `true_match` dispositions require non-empty `Disposition.evidence`;
`insufficient_data` is exempt (there is nothing to cite when enrichment itself was
too thin to decide). This mirrors the existing `matched_entities`-required-for-
`true_match` rule already in `Disposition._check_consistency`
(`schemas/disposition.py`) rather than inventing a new mechanism.

A pre-existing asymmetry was fixed alongside this: `make_dispose`'s analyst branch
only fell back to enrichment-derived evidence (`_fallback_evidence`) on the *agent*
path, so an analyst-closed `true_match` could carry empty evidence while an
agent-closed one couldn't. Both branches now apply the same fallback.

**"Blocks" means:** `check_dispose_citation` writes a chained
`event="compliance_blocked"` audit row (so the trail reads: computed → rejected —
`dispose()`'s own `event="disposition"` row from the same run is *not* erased) and
raises `ComplianceViolationError`. `runtime/contract.py:invoke()` catches it and
returns `{"status": "blocked", "thread_id": ..., "violation": {...}}` instead of a
bare traceback; the disposition never enters graph state. `cap`'s existing exit-code
logic (`result["status"] in {"disposed", "escalated"}`) already treats `"blocked"` as
failure with no code change required there.

### S3 Object Lock (COMPLIANCE mode)

Framed in Retention Lock terms, per the team's storage background: **COMPLIANCE**
mode means nobody — including the AWS account root user — can shorten the retention
period or delete a locked object before it expires (the S3 equivalent of a Retention
Lock Compliance-mode pool policy). This project always uses COMPLIANCE mode, never
GOVERNANCE (the Enterprise/Governance-mode equivalent, overridable by a principal
holding `s3:BypassGovernanceRetention`).

`audit/s3_export.py` batch-exports `screening_audit` to S3 as newline-delimited
JSON via `cap audit export` — deliberately **not** on the graph's hot path (no
background-job infrastructure exists yet, and adding an S3 round-trip to `dispose`
would add latency and a new failure mode to core screening for no benefit). Each
batch writes a manifest recording `{first_id, last_id, row_count, chain_head_hash,
prev_manifest_key}`; the next export reads the previous manifest, resumes from
`last_id + 1`, and — using `verify_chain`'s new `start_hash` parameter — confirms
its first new row's `prev_hash` matches the recorded `chain_head_hash` *before*
writing anything. A gap (rows deleted between export runs) is refused, not silently
skipped. `verify_export` re-derives the entire chain from the exported copies alone,
checking both row-level tampering (a JSONL row no longer matches its own hash) and
manifest-level tampering (a manifest's `chain_head_hash` no longer matches its own
data file's last row) — this is the property the trust-boundary discussion above
depends on: an auditor holding only the S3 export can verify integrity without ever
trusting the live database.

Retention is set once, **at the bucket level**, not per object —
`cap audit bucket-config` prints the exact `aws s3api put-object-lock-configuration`
command from `Settings.audit_retention_days` (env `AUDIT_RETENTION_DAYS`), so the
provisioned bucket can't drift from what the app assumes. Because retention is
never touched by the write path, the exporter's IAM policy needs only
`s3:PutObject`, `s3:GetObject`, `s3:ListBucket` scoped to its prefix — no
`s3:PutObjectRetention` or `s3:BypassGovernanceRetention` — the write path is
structurally incapable of shortening or bypassing retention.

`AUDIT_RETENTION_DAYS` defaults to **30 days (1 month)** — an explicit POC default,
not a production recommendation. A real deployment should set this to whatever the
applicable regulatory recordkeeping requirement is (AML rules commonly call for
multi-year retention); the value is a `Settings` field specifically so this is a
per-environment config change, not a code change.

### CloudTrail log file integrity validation

Pure AWS trail-level setting (`EnableLogFileValidation`) — no application code.
Documented as exact CLI commands in `docs/aws-compliance-setup.md`, using a
**separate** bucket from the audit-export bucket (CloudTrail needs its own
service-principal bucket policy; keeping it decoupled avoids coupling two unrelated
trust boundaries — the exporter's least-privilege IAM and CloudTrail's broader
service-principal write grant — for no benefit).

## Consequences

**Positive**

- An exported copy of the audit trail is self-verifying — an auditor doesn't need
  to trust or even reach the live database.
- Node failures are now audited; previously they left no trace at all.
- A disposition citing no source data is structurally rejected, not just
  discouraged by prompt wording.
- Retention is one config value away from matching whatever a real deployment's
  regulatory requirement turns out to be.
- All of the above required no change to `nodes.py` node bodies (one one-line fix
  aside) and no change to ADR-001's trigger.

**Negative / risks**

- The hash chain's computation trusts application code (`write_audit`); a
  compromised process with write access could in theory fabricate a chain segment.
  Mitigated by ADR-001's trigger remaining the primary in-place-tamper guard, and
  noted as a documented, accepted tradeoff rather than a gap.
- Every `write_audit` call now takes an advisory lock spanning a read + insert,
  serializing all audit appends across the process. Acceptable at this project's
  volume (a handful of rows per screened alert); would need revisiting at
  high-throughput scale.
- A row written by raw SQL rather than `write_audit` (only ever done in test
  fixtures now — see below) has no chain fields and breaks continuity for anything
  written after it. Not a defect in the chain — refusing to treat such a row as a
  valid link is the feature working — but it is an easy trap on a shared dev
  database; documented under "`cap_test` is shared with the automated test suite"
  in `docs/aws-compliance-setup.md`.
- `write_audit`/`AuditRecord` do not yet expose `supersedes_id`, so a correction row
  (the append-only-correction pattern from ADR-001) still has to be inserted outside
  `write_audit` and is therefore not chained. Only exercised today by
  `test_insert_and_select_and_correction_row_work`, a test fixture — no production
  code path writes a correction row yet. Carried forward, not fixed here, to avoid
  changing the hashed-payload shape (and invalidating already-computed hashes) as
  part of this week's scope.
- A blocked disposition still leaves two audit rows for the same `dispose` step
  (`event="disposition"` then `event="compliance_blocked"`) — read together, not a
  duplicate-data bug.

## References

- ADR-001 — Why LangGraph (audit properties, replay-duplicate rows) —
  [`001-why-langgraph.md`](001-why-langgraph.md)
- S3 Object Lock overview —
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-overview.html
- CloudTrail log file integrity validation —
  https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-log-file-validation-intro.html
- `pg_advisory_xact_lock` —
  https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS
