"""The integration-suite guard: same-database detection (pure, no DB needed)."""

from __future__ import annotations

import pytest
from dbisolation import same_database

APP = "postgresql://cap:secret@cap-test.example.us-east-1.rds.amazonaws.com:5432/cap_test"


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        (APP, True),
        ("postgresql://other:pw@cap-test.example.us-east-1.rds.amazonaws.com/cap_test", True),
        (
            "postgresql://cap:secret@CAP-TEST.example.us-east-1.rds.amazonaws.com:5432/cap_test",
            True,
        ),
        ("postgresql://cap:secret@cap-test.example.us-east-1.rds.amazonaws.com:5432/cap_it", False),
        ("postgresql://cap:cap@localhost:5433/cap_it", False),
    ],
)
def test_same_database_against_remote_app_db(other, expected):
    assert same_database(APP, other) is expected


@pytest.mark.parametrize(
    "other",
    [
        "postgresql://cap:cap@localhost:5432/cap",
        "postgresql://cap:cap@127.0.0.1/cap",
        "postgresql://cap:cap@[::1]:5432/cap",
        "postgresql:///cap",
    ],
)
def test_local_spellings_are_the_same_database(other):
    assert same_database("postgresql://cap:cap@localhost:5432/cap", other) is True


def test_different_port_on_localhost_is_a_different_database():
    assert not same_database(
        "postgresql://cap:cap@localhost:5432/cap", "postgresql://cap:cap@localhost:5433/cap"
    )
