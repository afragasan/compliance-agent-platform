"""Deterministic PII detectors for the guardrail rails.

The LLM self-check rail is probabilistic and, on the self-hosted 8B NIM model, measurably
weak at spotting an SSN buried in a long prompt. These checks are the backend-independent
floor: pure code, no network, so they run (and block) even when NIM is down. They cover
only well-formed SSNs and card numbers; passport / bank-account numbers have no fixed
format and stay with the LLM rail.

Detectors return category names only - never the matched value - so a block reason
logged or written to the audit chain does not itself leak the PII.
"""

from __future__ import annotations

import re

# Dashed/spaced SSN: area not 000/666/9xx, group not 00, serial not 0000.
_SSN_SEPARATED = re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}([- ])(?!00)\d{2}\1(?!0000)\d{4}(?!\d)")
# Bare 9 digits are too common (ids, refs) to flag alone; require an SSN label before them.
_SSN_LABELLED = re.compile(
    r"(?:ssn|social\s+security(?:\s+(?:number|no\.?))?)\W{0,3}(?!000|666|9\d\d)\d{3}(?!00)\d{2}(?!0000)\d{4}(?!\d)",
    re.IGNORECASE,
)

# 13-19 digits with optional space/dash grouping, not embedded in a longer digit run.
_CARD_CANDIDATE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")


def _issuer_prefix_ok(digits: str) -> bool:
    """Major issuer ranges (Visa, Mastercard, Amex, Discover, Diners, JCB).

    With the Luhn check this keeps arbitrary 13+ digit ids from reading as cards.
    """
    p2, p4 = int(digits[:2]), int(digits[:4])
    return (
        digits[0] == "4"
        or 51 <= p2 <= 55
        or 2221 <= p4 <= 2720
        or p2 in (34, 37, 36, 38, 39, 65)
        or p4 == 6011
        or 3000 <= p4 <= 3059
        or 3528 <= p4 <= 3589
    )


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _has_card(text: str) -> bool:
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _issuer_prefix_ok(digits) and _luhn_ok(digits):
            return True
    return False


def find_pii(text: str) -> list[str]:
    """Return the PII categories present in ``text`` (``"ssn"``, ``"card_number"``)."""
    found: list[str] = []
    if _SSN_SEPARATED.search(text) or _SSN_LABELLED.search(text):
        found.append("ssn")
    if _has_card(text):
        found.append("card_number")
    return found
