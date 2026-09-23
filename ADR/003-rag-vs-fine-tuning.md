# ADR 003 — RAG over fine-tuning for regulatory content

- **Status:** Accepted
- **Date:** 2026-09-23
- **Deciders:** Compliance Agent Platform team
- **Scope:** Grounding the `evaluate` node's decisions in retrieved regulatory
  guidance (Week 3). Adds one node, `retrieve`, between `enrich` and `evaluate`
  established in [ADR-001](001-why-langgraph.md); extends the citation-mandatory
  design from [ADR-002](002-audit-chain-design.md) rather than replacing it.

## Context

ADR-001 established `evaluate` as a Bedrock (Claude) structured call over the alert
and mock enrichment data (`graph/nodes.py:_build_eval_prompt`). ADR-002 added
citation-mandatory enforcement — `clear`/`true_match` dispositions require non-empty
`Disposition.evidence` — but every evidence item to date has cited enrichment data
(watchlist match features, KYC) only. Confirmed by reading the code rather than
assumed: no step of the pipeline ever consulted the actual text of OFAC guidance,
Nacha operating rules, or MTRA/BSA regulations. The model's apparent "knowledge" of
that content came entirely from base-model training data — unversioned, not tied to
any specific clause, and not something `check_dispose_citation` could verify.

Regulatory guidance changes on its own cadence — a sanctions program update, a Nacha
rules revision, a FinCEN advisory — independent of and typically faster than any
model release cycle. This ADR covers how `evaluate`'s decisions get grounded in that
content going forward, and why retrieval was chosen over fine-tuning a model on it.

## Decision

Add a `retrieve` node that embeds a query built from the alert's structured fields,
searches a vector store of regulatory-corpus chunks, and injects the results into
`evaluate`'s prompt as citable, tagged text — not a fine-tuned model.

### Why not fine-tuning

- A fine-tuned model bakes a training-time snapshot of regulatory text into frozen
  weights. Every regulatory update — even a single amended threshold — would require
  a full retrain-and-revalidate cycle before the change is reflected in production
  decisions, with a new model artifact to version and trust each time.
- No per-decision provenance. A fine-tuned model's output can state a fact learned
  from training but cannot point to *which document, which clause, as of which
  version* it came from — the exact property ADR-002's citation-mandatory
  enforcement depends on. `EvidenceItem.document_id`/`chunk_id` (added this week,
  see below) has nothing to attach to on a fine-tuned model's output.
- The actual need here is fresh, attributable *facts* (what does 31 CFR 501.603
  currently require), not a change in the model's reasoning *behavior*. Fine-tuning
  is the right tool for the latter, not the former.

### Why RAG

- Updating for a regulatory change is `cap rag ingest` — re-parsing and re-embedding
  the changed corpus file, seconds to minutes — not a training run.
- Every retrieved chunk carries `DocumentMetadata` (`document_id`, `program`,
  `section`, `citation`) that flows straight into
  `EvidenceItem.document_id`/`chunk_id` (`schemas/disposition.py`, extended this
  week, additive to the existing `source`/`detail`/`reference` fields per ADR-002's
  "extend, don't invent a parallel mechanism" philosophy). A citation is literally
  the retrieval provenance, not a post-hoc justification the model invents — verified
  end-to-end: a real `cap run` against `examples/alert_clear.json` produced
  evidence items citing `ofac-sanctions-corpus#OFAC-003` and `#OFAC-007` alongside
  the existing enrichment-sourced items with `document_id`/`chunk_id` left `null`.
- Composable with the existing Bedrock Claude model (`llm.py`) unchanged — no new
  model to host, version, or IAM-scope. The embedding model
  (`amazon.titan-embed-text-v2:0`, via the new `embeddings.py`, mirroring `llm.py`'s
  factory + `CAP_FAKE_*` offline-toggle pattern) is the only new model dependency,
  and it is used purely for retrieval, never for generation.

### Alternatives considered

- **Fine-tuning (a Bedrock custom model / continued pretraining):** rejected, above.
- **Prompt-stuffing the entire corpus into every `evaluate` call:** rejected — does
  not scale past a handful of documents, gives the model no ranking signal over
  which passages are actually relevant to *this* alert, and produces citations with
  no chunk-level granularity. This is the literal "prompt-stuffed context" the
  Week 3 goal statement calls out as the thing to move away from.
- **Retrieval folded into `evaluate` itself, rather than a separate `retrieve`
  node:** rejected in favor of a dedicated node — matches the established
  one-state-channel-per-pipeline-stage convention (`ScreeningState["retrieval"]`,
  alongside `alert`/`enrichment`/`evaluation`), gets its own `wrap_node` audit row
  for free, and keeps `evaluate`'s prompt-building code from also owning
  embedding/search calls. The cost is that retrieval always runs even on the
  `insufficient_data` rule-overlay path inside `evaluate`, where its result ends up
  unused — accepted as one cheap embedding call, simpler than conditionally skipping
  the node.

### Corpus, chunking, and embedding

- The supplied corpus (`docs/regulatory-corpus/*.txt` — one file per program: OFAC,
  Nacha, MTRA/BSA) arrives **pre-chunked** at a sensible granularity: each file is a
  sequence of blank-line-separated records (`[CHUNK_ID]`, `[REGULATORY_REF]`,
  `[TOPIC]`, `[COMPLIANCE_LEVEL]`, `[TEXT]`, `[CITATION]`), one clause per record.
  `rag/corpus.py::parse_corpus_file` parses this format directly — there is no
  sliding-window/token-based text splitter in this codebase, because the real
  corpus never needs one. A future free-prose regulatory source would need that
  capability added; not built ahead of an actual need for it.
- Embeddings: Bedrock Titan Embeddings (`amazon.titan-embed-text-v2:0`, 1024
  dimensions), used identically by both vector-store backends so index/query
  vectors — and therefore eval numbers — carry over between local dev and
  production without drift.
- Vector stores: a `rag.base.VectorStore` Protocol (mirroring `adapters/base.py`'s
  existing swappable-provider pattern) with two implementations —
  `FaissVectorStore` (exact flat cosine search, JSON-persisted; local dev, no
  RDS/pgvector round-trip during iteration) and `PgVectorStore` (pgvector on RDS,
  HNSW index — chosen over IVFFlat because IVFFlat's lists are trained from data
  already in the table and degrade on a small, frequently re-ingested corpus, while
  HNSW builds incrementally). `Settings.vector_backend` selects between them; both
  were validated end-to-end in this project's real dev environment (real Bedrock
  embeddings, real `cap_test` RDS instance with the `vector` extension already
  allow-listed) and produced **identical** retrieval-eval numbers (54 labeled
  queries, `mean_precision_at_5 = 0.196`, `mean_recall_at_5 = 0.981`,
  `mean_precision_at_1 = 0.944`) — direct evidence for the backend-parity claim
  above, not just a design intention.

## Consequences

**Positive**

- Citations are structurally traceable to a specific corpus document and chunk,
  closing the "grounded, not prompt-stuffed" gap the Week 3 goal calls out —
  verified against a real model, not just unit-tested against fakes.
- A regulatory update is a re-ingest, not a retrain.
- Measured backend parity (identical eval numbers on FAISS and pgvector) means a
  precision/recall number established during local iteration is trustworthy for the
  deployed backend too.
- All of this required no change to the `evaluate` node's control flow (rule-overlay
  short-circuit, structured-output call) and no change to ADR-002's citation
  enforcement mechanism — only additive fields.

**Negative / risks**

- Retrieval quality now gates decision quality: a poorly retrieved chunk set can
  mislead `evaluate` as much as a bad prompt could. The one measured miss in the
  54-pair eval set (a Travel Rule query pulling in other dollar-threshold clauses
  instead) is a concrete instance of near-miss semantic confusion between
  similarly-worded rules — mitigated by the retrieval-eval harness (`rag/eval.py`,
  `cap retrieval-eval`) existing at all, not eliminated by it.
- Two vector-store implementations must stay in behavioral parity by construction
  (same embedding model, score normalized to "higher is better" in both); the
  parity measurement above is a snapshot, not a standing guarantee — a future change
  to either backend's scoring needs to be re-verified the same way.
- Chunking is inherited from the corpus's own pre-chunking, not derived by this
  codebase — a future corpus that isn't pre-chunked this way needs a real
  chunking strategy added, not assumed to fit the same parser.
- Corpus completeness and currency (whether the supplied documents are actually
  representative and up to date) is outside this ADR's and the codebase's control;
  `cap rag ingest` re-embeds whatever is on disk, but nothing detects staleness.
- `document_chunks` (the pgvector table) is deliberately excluded from
  `tests/conftest.py`'s `clean_db` truncate list, unlike every other table Week 1/2
  established — a reference-data table with different lifecycle rules than
  `screening_audit`/checkpoint tables, documented in `rag/schema.sql`'s header and
  `docs/aws-compliance-setup.md` so it doesn't read as an oversight.

## References

- ADR-001 — Why LangGraph —
  [`001-why-langgraph.md`](001-why-langgraph.md)
- ADR-002 — Hash-chained audit + citation-mandatory compliance middleware —
  [`002-audit-chain-design.md`](002-audit-chain-design.md)
- pgvector — https://github.com/pgvector/pgvector
- pgvector HNSW vs. IVFFlat — https://github.com/pgvector/pgvector#indexing
- Amazon Titan Text Embeddings V2 —
  https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html
- FAISS — https://github.com/facebookresearch/faiss
