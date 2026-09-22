"""Exceptions raised by compliance middleware policy checks."""

from __future__ import annotations


class ComplianceViolationError(Exception):
    """A node's output violates a compliance policy and must not become state.

    Raised from a post-node hook inside :func:`wrap_node`, after the node's own
    audit row has already been written (so the run's history reads: computed →
    rejected → never committed). ``runtime.contract.invoke`` catches this and
    reports ``status: "blocked"`` instead of letting it escape as a bare 500.
    """

    def __init__(self, *, alert_id: str, disposition_type: str, reason: str) -> None:
        self.alert_id = alert_id
        self.disposition_type = disposition_type
        self.reason = reason
        super().__init__(f"{alert_id}: {disposition_type} blocked - {reason}")
