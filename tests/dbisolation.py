"""Guard that keeps the integration suite off the application's database.

The suite TRUNCATEs ``screening_audit``, the checkpoint tables and ``document_chunks``.
When it shared ``DATABASE_URL`` with ``cap run`` it destroyed real audit evidence, so it
now requires its own ``TEST_DATABASE_URL`` and refuses one that names the app's database.
"""

from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict

_DEFAULT_PORT = "5432"
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def _identity(dsn: str) -> tuple[str, str, str]:
    """(host, port, dbname) with every spelling of 'this machine' folded together."""
    info = conninfo_to_dict(dsn)
    host = (info.get("host") or "").lower()
    if host in _LOCAL_HOSTS:
        host = "localhost"
    return host, str(info.get("port") or _DEFAULT_PORT), info.get("dbname") or ""


def same_database(a: str, b: str) -> bool:
    """True when two DSNs point at the same host, port and database name."""
    return _identity(a) == _identity(b)
