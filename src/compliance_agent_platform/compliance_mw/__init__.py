"""Compliance middleware: wraps every graph node with failure auditing and
policy hooks (currently citation-mandatory enforcement on ``dispose``)."""

from __future__ import annotations

from compliance_agent_platform.compliance_mw.errors import ComplianceViolationError
from compliance_agent_platform.compliance_mw.middleware import wrap_node

__all__ = ["ComplianceViolationError", "wrap_node"]
