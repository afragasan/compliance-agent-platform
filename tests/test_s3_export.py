"""S3 export/verify of the hash-chained audit log (moto, in-memory - no network)."""

from __future__ import annotations

import boto3
import pytest
from fakes import FakeAuditConn
from moto import mock_aws
from test_audit_chain import _record

from compliance_agent_platform.audit.log import write_audit
from compliance_agent_platform.audit.s3_export import export_batch, verify_export

_BUCKET = "cap-audit-test"


@pytest.fixture
def settings():
    from compliance_agent_platform.config import Settings

    return Settings(audit_export_bucket=_BUCKET, audit_export_prefix="screening-audit")


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield client


def _seed_rows(*steps: str) -> FakeAuditConn:
    conn = FakeAuditConn()
    for step in steps:
        write_audit(conn, _record(step=step, event="node_completed"))
    return conn


def test_export_from_genesis_writes_data_and_manifest(s3_client, settings):
    conn = _seed_rows("intake", "enrich", "evaluate", "dispose")

    result = export_batch(conn, s3_client, settings)

    assert result.first_id == 1
    assert result.last_id == 4
    assert result.row_count == 4
    assert result.manifest_key is not None

    keys = {o["Key"] for o in s3_client.list_objects_v2(Bucket=_BUCKET).get("Contents", [])}
    assert result.manifest_key in keys
    assert any(k.endswith(".jsonl") for k in keys)


def test_export_is_idempotent_when_no_new_rows(s3_client, settings):
    conn = _seed_rows("intake")
    export_batch(conn, s3_client, settings)

    second = export_batch(conn, s3_client, settings)

    assert second.row_count == 0
    assert second.manifest_key is None


def test_second_export_resumes_from_last_id(s3_client, settings):
    conn = _seed_rows("intake", "enrich")
    first = export_batch(conn, s3_client, settings)
    assert (first.first_id, first.last_id) == (1, 2)

    write_audit(conn, _record(step="evaluate", event="node_completed"))
    write_audit(conn, _record(step="dispose", event="disposition"))
    second = export_batch(conn, s3_client, settings)

    assert (second.first_id, second.last_id) == (3, 4)
    assert second.row_count == 2


def test_verify_export_ok_across_multiple_batches(s3_client, settings):
    conn = _seed_rows("intake", "enrich")
    export_batch(conn, s3_client, settings)
    write_audit(conn, _record(step="evaluate", event="node_completed"))
    write_audit(conn, _record(step="dispose", event="disposition"))
    export_batch(conn, s3_client, settings)

    result = verify_export(s3_client, settings)
    assert result.ok is True


def test_verify_export_empty_bucket_is_ok(s3_client, settings):
    result = verify_export(s3_client, settings)
    assert result.ok is True


def test_verify_export_detects_tampered_manifest_chain_head_hash(s3_client, settings):
    conn = _seed_rows("intake", "enrich")
    first = export_batch(conn, s3_client, settings)

    import json

    manifest_body = json.loads(
        s3_client.get_object(Bucket=_BUCKET, Key=first.manifest_key)["Body"].read()
    )
    manifest_body["chain_head_hash"] = "tampered" * 8
    s3_client.put_object(
        Bucket=_BUCKET, Key=first.manifest_key, Body=json.dumps(manifest_body).encode()
    )

    result = verify_export(s3_client, settings)
    assert result.ok is False
    assert "chain_head_hash" in result.reason


def test_verify_export_detects_tampered_row_in_data_file(s3_client, settings):
    import json

    conn = _seed_rows("intake", "enrich", "evaluate")
    export_batch(conn, s3_client, settings)
    data_key = _data_key(s3_client)

    lines = s3_client.get_object(Bucket=_BUCKET, Key=data_key)["Body"].read().decode().splitlines()
    middle = json.loads(lines[1])
    middle["event"] = "tampered"
    lines[1] = json.dumps(middle)
    s3_client.put_object(Bucket=_BUCKET, Key=data_key, Body="\n".join(lines).encode())

    result = verify_export(s3_client, settings)
    assert result.ok is False


def _data_key(s3_client) -> str:
    for obj in s3_client.list_objects_v2(Bucket=_BUCKET).get("Contents", []):
        if obj["Key"].endswith(".jsonl"):
            return obj["Key"]
    raise AssertionError("no data file found")


def test_export_batch_refuses_when_live_table_no_longer_matches_last_export(s3_client, settings):
    conn = _seed_rows("intake", "enrich")
    export_batch(conn, s3_client, settings)

    # Simulate the live table's chain tail diverging from what was already
    # exported (e.g. row 2 deleted and silently reinserted, or otherwise
    # altered) - anything appended after that point chains from a hash the
    # last manifest never recorded, so the next export must refuse.
    conn.rows[-1]["record_hash"] = "corrupted" * 8
    write_audit(conn, _record(step="evaluate", event="node_completed"))

    with pytest.raises(RuntimeError, match="chain continuity broken"):
        export_batch(conn, s3_client, settings)
