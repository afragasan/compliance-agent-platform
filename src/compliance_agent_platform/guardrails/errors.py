"""Raised when a guardrail blocks content. Mirrors ``ComplianceViolationError``'s role
in ``compliance_mw``: a plain exception that ``wrap_node``'s generic failure-audit path
already knows how to catch and record (see ``compliance_mw/middleware.py``)."""

from __future__ import annotations


class GuardrailViolationError(Exception):
    def __init__(self, *, rail: str, reason: str | None = None) -> None:
        self.rail = rail
        self.reason = reason
        super().__init__(f"Guardrail blocked ({rail}): {reason or 'no reason given'}")
