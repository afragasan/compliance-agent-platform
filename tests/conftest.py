"""Shared fixtures.

Integration tests are marked ``@pytest.mark.integration`` and need a live PostgreSQL
(the dev RDS ``cap_test`` database). They are skipped automatically when ``DATABASE_URL``
is not resolvable, so ``pytest`` with no database still runs the full unit suite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

# Make .env values (DATABASE_URL, AWS_REGION, ...) visible to os.environ for the test run,
# the same file pydantic-settings reads for the app.
load_dotenv()

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Checkpoint data tables to wipe between integration tests. `checkpoint_migrations`
# is deliberately left alone (it tracks the checkpoint schema version).
_DATA_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "screening_audit")


def _resolved_dsn() -> str | None:
    """The integration DSN, or None if the environment has not configured one.

    Only an explicit ``DATABASE_URL`` (typically from ``.env``) or a ``DB_SECRET_ARN``
    counts — never the app's built-in localhost default, so a machine with neither
    skips the integration tests rather than trying to reach a phantom local server.
    """
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    if os.environ.get("DB_SECRET_ARN"):
        try:
            from compliance_agent_platform.config import get_settings

            return get_settings().resolved_dsn()
        except Exception:
            return None
    return None


def pytest_collection_modifyitems(config, items):
    """Skip every `integration` test when no database is configured."""
    if _resolved_dsn():
        return
    skip = pytest.mark.skip(reason="no DATABASE_URL / DB_SECRET_ARN configured")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = _resolved_dsn()
    if not dsn:
        pytest.skip("no DATABASE_URL / DB_SECRET_ARN configured")
    return dsn


@pytest.fixture(scope="session")
def _schema_ready(pg_dsn: str) -> str:
    """Create the checkpoint + audit schema once per session (idempotent)."""
    from compliance_agent_platform.checkpoint.postgres import open_checkpointer
    from compliance_agent_platform.config import Settings

    with open_checkpointer(Settings(database_url=pg_dsn)):
        pass
    return pg_dsn


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
