# Week 3 validation runbook

Executes the plan reviewed and approved at the start of Week 3. Decisions for this
run:

- **Source documents:** real OFAC/Nacha/MTRA-style content supplied by the user as
  `docs/regulatory-corpus/*.txt` (150 chunks total, 50 per program), already
  pre-chunked at the source — see [ADR-003](../ADR/003-rag-vs-fine-tuning.md).
- **Embedding model:** Bedrock Titan Embeddings (`amazon.titan-embed-text-v2:0`,
  1024 dims), identical for both the FAISS and pgvector backends.
- **Eval labels:** drafted by the agent after reading the full corpus (54
  query→chunk_id pairs, `eval/retrieval_labels.json`), for the user's review.
- Branch: `week3-RAG-pipeline-and-regulatory-base`, off `main` (Week 2 already
  merged via PR #2).

---

## Stage A — Schemas, config, Protocol, dependency scaffolding

- New `schemas/retrieval.py`: `DocumentMetadata`, `DocumentChunk`, `RetrievedChunk`,
  `RetrievalBundle`.
- `schemas/state.py`: `ScreeningState` gains a `retrieval: dict` channel.
- `schemas/disposition.py`: `EvidenceItem` gains optional `document_id`/`chunk_id` —
  additive, no existing call site needed to change.
- `config.py`: new `vector_backend`, `bedrock_embedding_model_id`,
  `embedding_dimension`, `faiss_index_path`, `retrieval_top_k`,
  `regulatory_corpus_dir` fields.
- New `embeddings.py` (`get_embedding_model()`, mirrors `llm.py`'s factory +
  `CAP_FAKE_*` offline-toggle pattern — `CAP_FAKE_EMBEDDINGS`).
- New `rag/base.py` (`VectorStore` Protocol, mirrors `adapters/base.py`).
- `pyproject.toml`: added `faiss-cpu>=1.9.0`, `pgvector>=0.3.6`.

**Gate:** `uv run ruff check src tests` clean; unit suite green with no behavior
change to existing code paths.

---

## Stage B — Corpus parsing + ingestion

- New `rag/corpus.py::parse_corpus_file`: parses the real corpus's
  `[TAG]: value` blank-line-separated record format directly — no sliding-window
  text splitter, since the supplied corpus arrives pre-chunked (see ADR-003).
- New `rag/ingest.py`: `load_corpus`, `build_index`.
- New `cap rag ingest [--corpus-dir] [--backend]`, `cap rag search --query`.
- **Found and fixed during real ingestion:** each of the 3 supplied corpus files
  carries one stray trailing NUL byte (valid UTF-8, but Postgres text/JSONB columns
  reject it outright — `psycopg.errors.UntranslatableCharacter` on the pgvector
  upsert). Fixed by stripping `\x00` when reading corpus text
  (`rag/corpus.py::parse_corpus_file`), with a regression test
  (`test_strips_stray_nul_bytes`).

**Gate:** `uv run pytest tests/test_corpus_parsing.py tests/test_rag_ingest.py -q`
green, including `test_real_corpus_files_all_parse_and_are_program_tagged`
(parses all 150 real chunks, asserts unique `chunk_id`s and the 3 expected
programs).

---

## Stage C — Vector store backends

- New `rag/faiss_store.py`: `FaissVectorStore` — exact `IndexFlatIP` search over
  normalized vectors, single-JSON-file persistence (not FAISS's own binary format —
  simpler and diff-able at this corpus's scale).
- New `rag/pgvector_store.py` + `rag/schema.sql`: `PgVectorStore`, `document_chunks`
  table, HNSW index (`vector_cosine_ops`).
- **Found and fixed:** `pgvector.psycopg.register_vector` alone does not adapt
  plain Python `list[float]` params — needs each embedding wrapped in
  `pgvector.Vector(...)` explicitly, both for `INSERT` and for the `<=>` operands in
  `search()`'s `ORDER BY`. Confirmed by running the integration test against the
  real `cap_test` RDS instance and reading the resulting `DatatypeMismatch` error.
- `docker-compose.yml`: `postgres:16` → `pgvector/pgvector:pg16`.
- `docs/aws-compliance-setup.md`: new "pgvector on RDS" section — confirmed the
  `vector` extension was already available on `cap_test` with **no parameter-group
  change needed**; `ensure_rag_schema` applied the schema idempotently on first use,
  same pattern as `ensure_audit_schema`.

**Gate:**
```bash
uv run pytest tests/test_faiss_store.py -q       # 6 passed (offline)
uv run pytest tests/test_pgvector_store.py -q    # 2 passed (real cap_test RDS)
```

---

## Stage D — `retrieve` graph node + `evaluate` integration

- `graph/nodes.py`: new `make_retrieve` (audited via the existing `wrap_node`
  machinery, no `compliance_mw` changes needed) + `build_retrieval_query(alert,
  enrichment)` — built from `alert.hits[].list_name`, candidate `programs`,
  `nationality`, `address_country`, and `enrichment.adverse_media_summaries` (the
  one pre-existing free-text field).
- `_build_eval_prompt` gains a `RetrievalBundle` argument, appending a
  `[document_id#chunk_id]`-tagged "RETRIEVED REGULATORY GUIDANCE" section;
  `_EVAL_SYSTEM` gains one sentence instructing the model to populate
  `EvidenceItem.document_id`/`chunk_id` when citing it.
- `graph/builder.py`: `NodeContext` gains `embedder`/`vector_store`; wired
  `enrich → retrieve → evaluate` (replacing `enrich → evaluate`).
- **Regression found and fixed:** two Week-1/2 integration tests
  (`tests/test_checkpoint_resume.py`) asserted the exact audit-step sequence and
  broke on the new `retrieve` step — updated the expected sequences and added
  `CAP_FAKE_EMBEDDINGS=1` alongside the existing `CAP_FAKE_MODEL` so these tests
  stay fully offline (the docstring's "no Bedrock call is made" claim is now
  actually true again, covering embeddings too).

**Gate:** `uv run pytest -q` (full suite, since a real graph edge and `evaluate`'s
prompt shape changed) → **110 passed** (95 unit + 15 integration against real
`cap_test`).

---

## Stage E — Retrieval eval harness

- New `rag/eval.py`: `RetrievalLabel` (chunk-granularity, not document-granularity —
  with only 3 source documents, document-level precision/recall would be a
  near-trivial signal; each of the 150 corpus chunks is independently retrievable),
  `evaluate_retrieval` (precision@k / recall@k).
- New `cap retrieval-eval --pairs <file> [--k] [--backend] [--verbose]`.

**Gate:** `uv run pytest tests/test_retrieval_eval.py -q` — 4 passed, metric math
validated against a fully controlled synthetic fixture with hand-computed expected
values.

---

## Stage F — ADR-003 + corpus README

- [`ADR/003-rag-vs-fine-tuning.md`](../ADR/003-rag-vs-fine-tuning.md) — full
  RAG-vs-fine-tuning decision record, referencing the real eval numbers below.
- [`docs/regulatory-corpus/README.md`](regulatory-corpus/README.md) — the corpus
  file-format contract and how to add/update a document.

---

## Stage G — Real ingestion, real eval numbers, end-to-end validation

Run against the real corpus (150 chunks, 3 documents) and the real dev RDS
`cap_test` instance:

```bash
uv run cap rag ingest --backend faiss
# {"documents": 3, "chunks": 150, "backend": "faiss"}
uv run cap rag ingest --backend pgvector
# {"documents": 3, "chunks": 150, "backend": "pgvector"}
```

**Real retrieval-eval numbers** (54 labeled query→chunk pairs,
`eval/retrieval_labels.json`), FAISS and pgvector **identical** — direct evidence
for backend parity, not just a design intention:

| k | mean precision@k | mean recall@k |
|---|---|---|
| 1 | 0.944 | 0.944 |
| 3 | 0.321 | 0.963 |
| 5 | 0.196 | 0.981 |

(Precision@k>1 is mechanically bounded by 1/k here since each query has exactly one
labeled relevant chunk — recall is the more informative number at k>1.) The one
miss at k=5: a Travel Rule query (`MTRA-006`) retrieved other dollar-threshold
clauses (`MTRA-001`, `MTRA-008`, `MTRA-048`, ...) instead — a genuine semantic
near-miss between similarly-worded rules, not a harness bug.

**End-to-end real-Bedrock validation** (`cap run`, real Claude + real Titan
embeddings + real retrieval, against `examples/alert_clear.json`): disposed
`clear`, with `Disposition.evidence` containing enrichment-sourced items
(`document_id`/`chunk_id` both `null`) alongside two retrieval-sourced items citing
`ofac-sanctions-corpus#OFAC-003` and `#OFAC-007` — the model correctly distinguished
which citations came from retrieved guidance vs. enrichment data. Confirmed the
`retrieve` step's audit row records the built query and retrieved `chunk_ids`, and
that `verify_chain()` over the full `screening_audit` table still returns `ok=True`
after these runs (Week 2's hash chain is unaffected by Week 3's changes).

**Final gate:**
```bash
uv run ruff check src tests && uv run ruff format --check src tests   # clean
uv run pytest -m "not integration" -q   # 95 passed
uv run pytest -m integration -q         # 15 passed
uv run pytest -q                        # 110 passed
```

---

## Known gaps carried forward (not silently absorbed into Week 3's scope)

- **Corpus is pre-chunked by construction.** `rag/corpus.py` parses the supplied
  `[TAG]: value` record format directly; there is no generic prose
  chunker/splitter. A future free-prose regulatory source (a raw PDF/plain
  narrative document, not pre-segmented into clauses) would need real chunking
  logic added — not assumed to fit the current parser.
- **`document_chunks` embedding dimension is hardcoded at 1024** in
  `rag/schema.sql` (matching `Settings.embedding_dimension`'s default) rather than
  settings-driven at the DB level — changing the embedding model's output dimension
  requires a manual `ALTER TABLE ... ALTER COLUMN embedding TYPE VECTOR(n)`
  migration, documented inline in the schema file.
- **Retrieved-chunk citations aren't independently re-verified.** The model is
  instructed (via `_EVAL_SYSTEM`) to populate `EvidenceItem.document_id`/`chunk_id`
  correctly when citing retrieved guidance, and the one real end-to-end run
  confirmed it does — but nothing in `compliance_mw` checks that a claimed
  `document_id#chunk_id` actually matches a chunk that was really retrieved for that
  alert. A stronger guarantee (cross-checking evidence citations against
  `RetrievalBundle.chunks`) is a natural `check_dispose_citation` extension, not
  built this week.
- **Corpus completeness/currency is unverifiable by the codebase** — `cap rag
  ingest` embeds whatever is on disk; nothing detects that a document is stale
  relative to the regulation it represents. Carried forward from ADR-003's
  Consequences.
- Carried forward from Week 1/2's own lists (`cap_app` least-privilege DB role,
  RDS/S3/CloudTrail provisioning as IaC, `write_audit`/`AuditRecord` not exposing
  `supersedes_id`, fake-model `model_id` cosmetics, the `cap_test` shared-DB
  audit-export trap) — still manual/documented, not automated; none revisited this
  week.
