"""Deterministic PII detectors: what they catch and, as importantly, what they leave alone."""

from __future__ import annotations

import pytest

from compliance_agent_platform.guardrails.patterns import find_pii


@pytest.mark.parametrize(
    "text",
    [
        "Customer SSN: 123-45-6789",
        "ssn 123 45 6789",
        "SSN 123456789",  # bare digits are fine when labelled
        "Social Security Number: 123456789",
        "embedded 078-05-1120 in a sentence",
    ],
)
def test_detects_ssn(text):
    assert find_pii(text) == ["ssn"]


@pytest.mark.parametrize(
    "text",
    [
        "123456789",  # bare 9 digits, unlabelled: ids/refs are common
        "000-12-3456",  # invalid area
        "666-12-3456",  # invalid area
        "900-12-3456",  # 9xx area
        "123-00-6789",  # invalid group
        "123-45-0000",  # invalid serial
        "1234-56-78901",  # embedded in longer digit runs
    ],
)
def test_ignores_non_ssn(text):
    assert find_pii(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "paid with 4111 1111 1111 1111",
        "card 4111-1111-1111-1111",
        "4111111111111111",
        "Mastercard 5555555555554444",
        "Amex 378282246310005",
        "Discover 6011111111111117",
    ],
)
def test_detects_card_number(text):
    assert find_pii(text) == ["card_number"]


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1112",  # fails Luhn
        "1234567890123",  # 13 digits, unknown issuer prefix
        "SDN-88888",
        "ALRT-CLEAR-001",
        "DOB 1990-07-22, score 0.41",
        "2026-11-01T22:57:36.002388Z",
        "ref 20261101225736",  # 14-digit timestamp-like id
    ],
)
def test_ignores_non_card(text):
    assert find_pii(text) == []


def test_reports_categories_never_values():
    found = find_pii("SSN 123-45-6789 and card 4111111111111111")
    assert found == ["ssn", "card_number"]
    assert not any(ch.isdigit() for name in found for ch in name)
