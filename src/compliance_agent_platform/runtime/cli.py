"""Local driver for the screening agent (stand-in for the AgentCore Runtime).

    cap run    --alert examples/alert_true_match.json
    cap resume --thread <alert_id> --resolution true_match --analyst-id A123 \
               --rationale "Confirmed DOB and nationality match"
    cap audit export         # push new screening_audit rows to the Object Lock bucket
    cap audit verify         # re-derive the hash chain across every exported batch
    cap audit bucket-config  # print the aws s3api command to (re)provision the bucket
    cap rag ingest           # chunk + embed the regulatory corpus into the vector store
    cap rag search           # ad-hoc retrieval, for manual sanity-checking
    cap retrieval-eval       # precision@k / recall@k over labeled query->chunk pairs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from compliance_agent_platform.runtime.contract import invoke


def _print(result: dict) -> None:
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _cmd_run(args: argparse.Namespace) -> int:
    alert = json.loads(Path(args.alert).read_text())
    result = invoke({"alert": alert})
    _print(result)
    return 0 if result["status"] in {"disposed", "escalated"} else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    resume = {
        "resolution": args.resolution,
        "analyst_id": args.analyst_id,
        "rationale": args.rationale,
    }
    result = invoke({"thread_id": args.thread, "resume": resume})
    _print(result)
    return 0 if result["status"] == "disposed" else 1


def _cmd_audit_export(args: argparse.Namespace) -> int:
    import boto3
    import psycopg

    from compliance_agent_platform.audit.s3_export import export_batch
    from compliance_agent_platform.config import get_settings

    settings = get_settings()
    s3 = boto3.client("s3", region_name=settings.aws_region)
    with psycopg.connect(settings.resolved_dsn(), autocommit=True) as conn:
        result = export_batch(conn, s3, settings)
    _print(
        {
            "first_id": result.first_id,
            "last_id": result.last_id,
            "row_count": result.row_count,
            "manifest_key": result.manifest_key,
        }
    )
    return 0


def _cmd_audit_verify(args: argparse.Namespace) -> int:
    import boto3

    from compliance_agent_platform.audit.s3_export import verify_export
    from compliance_agent_platform.config import get_settings

    settings = get_settings()
    s3 = boto3.client("s3", region_name=settings.aws_region)
    result = verify_export(s3, settings)
    _print({"ok": result.ok, "broken_at_id": result.broken_at_id, "reason": result.reason})
    return 0 if result.ok else 1


def _cmd_audit_bucket_config(args: argparse.Namespace) -> int:
    from compliance_agent_platform.audit.s3_export import object_lock_configuration
    from compliance_agent_platform.config import get_settings

    settings = get_settings()
    config = object_lock_configuration(settings)
    bucket = settings.audit_export_bucket or "<AUDIT_EXPORT_BUCKET>"
    print(f"# Object Lock retention: {settings.audit_retention_days} days, COMPLIANCE mode")
    print(
        "aws s3api put-object-lock-configuration "
        f"--bucket {bucket} "
        f"--object-lock-configuration '{json.dumps(config)}'"
    )
    return 0


def _open_vector_store(backend: str | None):
    """Open the requested (or configured) backend for a one-off CLI command.

    FAISS is loaded/saved explicitly here (rather than via ``rag.factory``) because
    a CLI invocation is the only place a FAISS store's on-disk file is written back
    after being mutated (``ingest``); the graph's `retrieve` node only ever reads it.
    """
    import psycopg

    from compliance_agent_platform.config import get_settings
    from compliance_agent_platform.rag.faiss_store import FaissVectorStore
    from compliance_agent_platform.rag.pgvector_store import PgVectorStore

    settings = get_settings()
    chosen = backend or settings.vector_backend
    if chosen != settings.vector_backend:
        settings = settings.model_copy(update={"vector_backend": chosen})
    if chosen == "faiss":
        return (
            FaissVectorStore.load(settings.faiss_index_path, settings.embedding_dimension),
            settings,
            None,
        )
    conn = psycopg.connect(settings.resolved_dsn(), autocommit=False)
    return PgVectorStore(conn), settings, conn


def _cmd_rag_ingest(args: argparse.Namespace) -> int:
    from compliance_agent_platform.rag.faiss_store import FaissVectorStore
    from compliance_agent_platform.rag.ingest import build_index

    store, settings, conn = _open_vector_store(args.backend)
    if args.corpus_dir:
        settings = settings.model_copy(update={"regulatory_corpus_dir": args.corpus_dir})
    try:
        summary = build_index(settings, store)
        if isinstance(store, FaissVectorStore):
            store.save(settings.faiss_index_path)
    finally:
        if conn is not None:
            conn.close()
    _print({"documents": summary.documents, "chunks": summary.chunks, "backend": summary.backend})
    return 0


def _cmd_rag_search(args: argparse.Namespace) -> int:
    from compliance_agent_platform.embeddings import get_embedding_model

    store, _settings, conn = _open_vector_store(args.backend)
    try:
        embedder = get_embedding_model()
        results = store.search(embedder.embed_query(args.query), k=args.k)
    finally:
        if conn is not None:
            conn.close()
    _print({"query": args.query, "results": [r.model_dump(mode="json") for r in results]})
    return 0


def _cmd_retrieval_eval(args: argparse.Namespace) -> int:
    from compliance_agent_platform.embeddings import get_embedding_model
    from compliance_agent_platform.rag.eval import evaluate_retrieval, load_labels

    store, _settings, conn = _open_vector_store(args.backend)
    try:
        labels = load_labels(args.pairs)
        embedder = get_embedding_model()
        report = evaluate_retrieval(store, embedder, labels, k=args.k)
    finally:
        if conn is not None:
            conn.close()
    _print(report.model_dump(mode="json") if args.verbose else report.model_dump(exclude={"rows"}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cap", description="Sanctions-screening agent runner")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Screen a new alert")
    run.add_argument("--alert", required=True, help="Path to an alert JSON file")
    run.set_defaults(func=_cmd_run)

    resume = sub.add_parser("resume", help="Supply an analyst decision for an escalated alert")
    resume.add_argument("--thread", required=True, help="thread_id (the alert_id)")
    resume.add_argument(
        "--resolution", required=True, choices=["clear", "true_match", "insufficient_data"]
    )
    resume.add_argument("--analyst-id", required=True, dest="analyst_id")
    resume.add_argument("--rationale", required=True)
    resume.set_defaults(func=_cmd_resume)

    audit = sub.add_parser("audit", help="Hash-chained audit log export to S3 Object Lock")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)

    audit_export = audit_sub.add_parser("export", help="Push new audit rows to S3")
    audit_export.set_defaults(func=_cmd_audit_export)

    audit_verify = audit_sub.add_parser("verify", help="Re-derive the chain across all exports")
    audit_verify.set_defaults(func=_cmd_audit_verify)

    audit_bucket_config = audit_sub.add_parser(
        "bucket-config", help="Print the aws s3api command to (re)provision the bucket"
    )
    audit_bucket_config.set_defaults(func=_cmd_audit_bucket_config)

    rag = sub.add_parser("rag", help="Regulatory-corpus retrieval pipeline")
    rag_sub = rag.add_subparsers(dest="rag_command", required=True)

    rag_ingest = rag_sub.add_parser("ingest", help="Chunk + embed the corpus into the vector store")
    rag_ingest.add_argument("--corpus-dir", default=None, help="Overrides regulatory_corpus_dir")
    rag_ingest.add_argument("--backend", choices=["faiss", "pgvector"], default=None)
    rag_ingest.set_defaults(func=_cmd_rag_ingest)

    rag_search = rag_sub.add_parser("search", help="Ad-hoc retrieval for manual sanity-checking")
    rag_search.add_argument("--query", required=True)
    rag_search.add_argument("--k", type=int, default=5)
    rag_search.add_argument("--backend", choices=["faiss", "pgvector"], default=None)
    rag_search.set_defaults(func=_cmd_rag_search)

    retrieval_eval = sub.add_parser(
        "retrieval-eval", help="precision@k / recall@k over labeled query->chunk pairs"
    )
    retrieval_eval.add_argument("--pairs", required=True, help="Path to a labeled-pairs JSON file")
    retrieval_eval.add_argument("--k", type=int, default=5)
    retrieval_eval.add_argument("--backend", choices=["faiss", "pgvector"], default=None)
    retrieval_eval.add_argument("--verbose", action="store_true", help="Include per-query rows")
    retrieval_eval.set_defaults(func=_cmd_retrieval_eval)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
