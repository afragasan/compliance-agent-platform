# Regulatory corpus

Source documents for the RAG pipeline (`rag/corpus.py`, `cap rag ingest`) — one
`.txt` file per source document/program. Ingestion walks every `*.txt` file in this
directory; the filename stem becomes `DocumentChunk.document_id`.

## File format

Each file is a sequence of blank-line-separated records, one per retrievable chunk:

```
[CHUNK_ID]: OFAC-014
[REGULATORY_REF]: OFAC Guidance on True Match Escalation
[TOPIC]: True Match Freeze Protocol & Escrow Placement
[COMPLIANCE_LEVEL]: Mandatory
[TEXT]: Upon confirming a True Match against an SDN target, transaction routing
must be instantly halted, funds must be placed into an isolated, interest-bearing
escrow block account, and the compliance officer must be notified immediately for
10-day reporting.
[CITATION]: OFAC True Match Handling & Property Freeze Manual (31 CFR 501.308)
```

- `CHUNK_ID` — must be unique across the *entire* corpus, not just within one file
  (retrieval keys and citations are chunk-level, corpus-wide). The convention used
  here is `<PROGRAM>-<NNN>` (e.g. `OFAC-014`) — the parser infers `program` from the
  text before the first `-`, so a new program just needs its own consistent prefix.
- `TEXT` and `CITATION` are the two fields that matter most downstream: `TEXT` is
  what gets embedded and shown to the model; `CITATION` becomes
  `DocumentMetadata.citation`, surfaced in `cap rag search` output and available to
  the model as retrieval context (not currently injected verbatim into evidence,
  but present in metadata for a future citation-formatting improvement).
- A record missing `CHUNK_ID` or `TEXT` is skipped, not treated as a fatal error —
  see `rag/corpus.py::parse_corpus_file`.

## Adding or updating a document

1. Drop a new `<name>-corpus.txt` file here (or edit an existing one) following the
   format above.
2. Re-run ingestion for whichever backend(s) you use:
   ```bash
   uv run cap rag ingest --backend faiss
   uv run cap rag ingest --backend pgvector
   ```
   Both are idempotent — `chunk_id` is the upsert key, so editing a chunk's `TEXT`
   and re-ingesting replaces it in place; nothing needs to be deleted first.
3. Spot-check retrieval before trusting it in an evaluate call:
   ```bash
   uv run cap rag search --query "your query" --k 5 --backend faiss
   ```
4. If you're adding queries that should retrieve the new/changed content, add
   labeled pairs to `eval/retrieval_labels.json` and re-run
   `cap retrieval-eval` (see the top-level README / ADR-003 for the eval harness).

## Current corpus

Three files (150 chunks total), covering the three programs this agent's alerts
reference: `ofac-sanctions-corpus.txt` (OFAC), `nacha-ach-corpus.txt` (Nacha ACH
operating rules), `mtra-bsa-corpus.txt` (MTRA/BSA money-transmitter regulations).
Illustrative regulatory content assembled for this project, not a verified legal
source of truth — see ADR-003's "corpus completeness" note under Consequences.
