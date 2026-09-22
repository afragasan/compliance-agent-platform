"""Batch export of the hash-chained audit log to an Object-Locked S3 bucket.

Not part of the graph's hot path — `dispose` never touches S3. This module is
invoked out-of-band via ``cap audit export`` (cron-able, not scheduled by this
project), matching the "document infra, don't stand up a service for it"
pattern already used for RDS/Bedrock in Week 1.

Layout under ``s3://<bucket>/<prefix>/``::

    data/YYYY/MM/DD/export-<ts>-<first_id>-<last_id>.jsonl   # one audit row per line
    manifests/export-<ts>-<first_id>-<last_id>.manifest.json # {first_id, last_id,
                                                               #  row_count, chain_head_hash,
                                                               #  prev_manifest_key, data_key}

Each manifest's ``chain_head_hash`` is the ``record_hash`` of the batch's last
row, so the *next* export can resume from ``last_id + 1`` and confirm its
first new row's ``prev_hash`` matches before writing anything — a skipped
DB row range between export runs breaks that check rather than silently
disappearing. No new DB table tracks export state; the bucket is the ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg

from compliance_agent_platform.audit.log import (
    _CHAIN_GENESIS,
    ChainVerificationResult,
    _default,
    verify_chain,
)
from compliance_agent_platform.config import Settings

_MANIFEST_SUFFIX = ".manifest.json"


def object_lock_configuration(settings: Settings) -> dict:
    """The ``ObjectLockConfiguration`` body for ``put-object-lock-configuration``.

    COMPLIANCE mode: nobody, including the account root user, can shorten the
    retention period or delete the object before it expires (equivalent to a
    Retention Lock Compliance-mode pool policy) — no per-object retention
    headers are ever set by the write path (`export_batch`); this bucket-level
    default is the only place retention is configured.
    """
    return {
        "ObjectLockEnabled": "Enabled",
        "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": settings.audit_retention_days}},
    }


@dataclass
class ExportResult:
    first_id: int | None
    last_id: int | None
    row_count: int
    manifest_key: str | None


def _manifest_prefix(settings: Settings) -> str:
    return f"{settings.audit_export_prefix.rstrip('/')}/manifests/"


def _require_bucket(settings: Settings) -> str:
    if not settings.audit_export_bucket:
        raise ValueError("Settings.audit_export_bucket is not configured")
    return settings.audit_export_bucket


def _list_manifests(s3_client, settings: Settings) -> list[dict]:
    """All manifests, sorted by first_id ascending."""
    bucket = _require_bucket(settings)
    manifests: list[dict] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=_manifest_prefix(settings)):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(_MANIFEST_SUFFIX):
                continue
            body = s3_client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            manifests.append(json.loads(body))
    manifests.sort(key=lambda m: m["first_id"])
    return manifests


def _read_jsonl(s3_client, bucket: str, key: str) -> list[dict]:
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    return [json.loads(line) for line in body.splitlines() if line]


def export_batch(conn: psycopg.Connection, s3_client, settings: Settings) -> ExportResult:
    """Export any ``screening_audit`` rows not yet exported. Idempotent, resumable."""
    bucket = _require_bucket(settings)
    manifests = _list_manifests(s3_client, settings)
    prev_manifest = manifests[-1] if manifests else None
    since_id = prev_manifest["last_id"] if prev_manifest else 0
    start_hash = prev_manifest["chain_head_hash"] if prev_manifest else _CHAIN_GENESIS

    cols = [d.name for d in conn.execute("SELECT * FROM screening_audit LIMIT 0").description]
    raw_rows = conn.execute(
        "SELECT * FROM screening_audit WHERE id > %s ORDER BY id", (since_id,)
    ).fetchall()
    rows = [dict(zip(cols, r, strict=True)) for r in raw_rows]

    if not rows:
        return ExportResult(first_id=None, last_id=None, row_count=0, manifest_key=None)

    result = verify_chain(rows, start_hash=start_hash)
    if not result.ok:
        raise RuntimeError(
            f"refusing to export: chain continuity broken at row {result.broken_at_id} "
            f"({result.reason}) - the live table no longer matches the last export"
        )

    first_id, last_id = rows[0]["id"], rows[-1]["id"]
    ts = datetime.now(timezone.utc)
    stamp = ts.strftime("%Y%m%dT%H%M%S%fZ")
    prefix = settings.audit_export_prefix.rstrip("/")
    data_key = f"{prefix}/data/{ts:%Y/%m/%d}/export-{stamp}-{first_id}-{last_id}.jsonl"
    manifest_key = f"{prefix}/manifests/export-{stamp}-{first_id}-{last_id}{_MANIFEST_SUFFIX}"

    body = "\n".join(json.dumps(row, sort_keys=True, default=_default) for row in rows)
    s3_client.put_object(Bucket=bucket, Key=data_key, Body=body.encode())

    manifest: dict[str, Any] = {
        "first_id": first_id,
        "last_id": last_id,
        "row_count": len(rows),
        "chain_head_hash": rows[-1]["record_hash"],
        "prev_manifest_key": prev_manifest["manifest_key"] if prev_manifest else None,
        "manifest_key": manifest_key,
        "data_key": data_key,
        "created_at": ts.isoformat(),
    }
    s3_client.put_object(
        Bucket=bucket, Key=manifest_key, Body=json.dumps(manifest, indent=2).encode()
    )

    return ExportResult(
        first_id=first_id, last_id=last_id, row_count=len(rows), manifest_key=manifest_key
    )


def verify_export(s3_client, settings: Settings) -> ChainVerificationResult:
    """Re-derive the hash chain across every exported batch, in manifest order.

    Returns the same :class:`~compliance_agent_platform.audit.log.ChainVerificationResult`
    shape as ``verify_chain``. Catches both row-level tampering (a JSONL row's
    content no longer matches its stored hash) and manifest-level tampering (a
    manifest's ``chain_head_hash`` no longer matches its own data file's last
    row) — an exported copy proves its own integrity without needing to trust
    the live database at all.
    """
    bucket = _require_bucket(settings)
    manifests = _list_manifests(s3_client, settings)
    if not manifests:
        return ChainVerificationResult(True)

    prev_key: str | None = None
    all_rows: list[dict] = []
    for manifest in manifests:
        if manifest.get("prev_manifest_key") != prev_key:
            return ChainVerificationResult(
                False,
                manifest.get("first_id"),
                "manifest chain is broken: prev_manifest_key mismatch",
            )
        rows = _read_jsonl(s3_client, bucket, manifest["data_key"])
        if rows and rows[-1].get("record_hash") != manifest.get("chain_head_hash"):
            return ChainVerificationResult(
                False,
                manifest.get("last_id"),
                "manifest chain_head_hash does not match its data file's last record_hash",
            )
        all_rows.extend(rows)
        prev_key = manifest["manifest_key"]

    if not all_rows:
        return ChainVerificationResult(True)
    return verify_chain(all_rows, start_hash=all_rows[0].get("prev_hash") or _CHAIN_GENESIS)
