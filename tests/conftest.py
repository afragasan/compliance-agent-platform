"""Shared fixtures.

Integration tests are marked ``@pytest.mark.integration`` and need their OWN PostgreSQL,
named by ``TEST_DATABASE_URL`` (``docker compose up -d postgres_test`` gives one at
``postgresql://cap:cap@localhost:5433/cap_it``). They are skipped when it is unset, and
the run is aborted if it names the same database as ``DATABASE_URL`` - the suite
TRUNCATEs the audit and checkpoint tables, and once destroyed real ``cap run`` evidence
in the shared dev database. During an integration test the app's own settings are
pointed at the test database too, so ``invoke()`` never touches the dev database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest
from dbisolation import same_database
from dotenv import load_dotenv

# Make .env values (DATABASE_URL, AWS_REGION, ...) visible to os.environ for the test run,
# the same file pydantic-settings reads for the app.
load_dotenv()

# Default the whole suite to the offline guardrails fake: `evaluate` calls
# guardrails.service.check_input/check_output unconditionally, even when a test
# injects a plain test-double `model=` that a real nemoguardrails LLMRails can't
# wrap. A real-rails test (Stage F's live validation) opts back in with
# `monkeypatch.delenv("CAP_FAKE_GUARDRAILS")`.
os.environ.setdefault("CAP_FAKE_GUARDRAILS", "1")

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Checkpoint data tables to wipe between integration tests. `checkpoint_migrations`
# is deliberately left alone (it tracks the checkpoint schema version).
_DATA_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "screening_audit")


def _test_dsn() -> str | None:
    """The integration suite's DSN: ``TEST_DATABASE_URL`` only, never ``DATABASE_URL``."""
    return os.environ.get("TEST_DATABASE_URL") or None


def _app_dsns() -> list[str]:
    """DSNs the application itself would use, which the suite must never share."""
    dsns = [os.environ["DATABASE_URL"]] if os.environ.get("DATABASE_URL") else []
    if os.environ.get("DB_SECRET_ARN"):
        try:
            from compliance_agent_platform.config import get_settings

            dsns.append(get_settings().resolved_dsn())
        except Exception:  # noqa: BLE001 - can't resolve the secret: the DATABASE_URL check stands
            pass
    return dsns


def pytest_collection_modifyitems(config, items):
    """Skip integration tests without a test DB; abort if it is the application's DB."""
    integration = [item for item in items if "integration" in item.keywords]
    if not integration:
        return
    dsn = _test_dsn()
    if not dsn:
        skip = pytest.mark.skip(
            reason="no TEST_DATABASE_URL (docker compose up -d postgres_test; see .env.example)"
        )
        for item in integration:
            item.add_marker(skip)
        return
    for app_dsn in _app_dsns():
        if same_database(dsn, app_dsn):
            raise pytest.UsageError(
                "TEST_DATABASE_URL points at the same database as the application "
                "(DATABASE_URL / DB_SECRET_ARN). The integration suite TRUNCATEs audit and "
                "checkpoint tables; refusing to run. Use a dedicated test database."
            )


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("no TEST_DATABASE_URL configured")
    return dsn


@pytest.fixture(scope="session")
def _schema_ready(pg_dsn: str) -> str:
    """Create the checkpoint + audit schema once per session (idempotent)."""
    from compliance_agent_platform.checkpoint.postgres import open_checkpointer
    from compliance_agent_platform.config import Settings

    with open_checkpointer(Settings(database_url=pg_dsn)):
        pass
    return pg_dsn


@pytest.fixture(autouse=True)
def _app_uses_test_db(request):
    """Point the application's settings at the test database for integration tests.

    ``invoke()`` and friends build ``Settings`` from the environment, so without this they
    would read/write the dev ``DATABASE_URL`` while ``clean_db`` truncated the test DB.
    """
    if "integration" not in request.keywords:
        yield
        return
    from compliance_agent_platform.config import get_settings

    request.addfinalizer(get_settings.cache_clear)  # runs after monkeypatch restores env
    dsn = request.getfixturevalue("pg_dsn")
    monkeypatch = request.getfixturevalue("monkeypatch")
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("DB_SECRET_ARN", "")  # blank beats a value in .env
    get_settings.cache_clear()
    yield


@pytest.fixture
def clean_db(pg_dsn: str, _schema_ready: str) -> str:
    """Truncate all screening data before the test. Data is left in place afterwards
    so a failed run can be inspected."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {', '.join(_DATA_TABLES)}")
    return pg_dsn


@pytest.fixture
def load_alert():
    """Return a loader for the JSON fixtures in ``examples/``."""

    def _load(name: str) -> dict:
        path = _EXAMPLES / (name if name.endswith(".json") else f"{name}.json")
        return json.loads(path.read_text())

    return _load
