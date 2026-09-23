"""Citation-mandatory enforcement: a disposition cannot rest on nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from compliance_agent_platform.audit.log import AuditRecord, write_audit
from compliance_agent_platform.compliance_mw.errors import ComplianceViolationError
from compliance_agent_platform.schemas.disposition import Disposition, DispositionType
from compliance_agent_platform.schemas.state import ScreeningState

if TYPE_CHECKING:
    from compliance_agent_platform.graph.builder import NodeContext

# Dispositions that assert something about the subject (cleared, or matched) must
# cite what that assertion rests on. insufficient_data is exempt: by definition
# there wasn't enough enrichment data to cite anything.
EVIDENCE_REQUIRED = {DispositionType.CLEAR, DispositionType.TRUE_MATCH}


def check_dispose_citation(ctx: NodeContext, state: ScreeningState, result: dict) -> None:
    """Post-hook for the ``dispose`` node: block a disposition with no citations.

    ``dispose()`` has already written its own ``event="disposition"`` audit row
    by the time this runs; on a violation this adds a second, chained
    ``event="compliance_blocked"`` row and raises, so the run's audit trail
    honestly reads "computed, then rejected" rather than erasing what the
    agent produced.
    """
    disposition = Disposition.model_validate(result["disposition"])
    if disposition.disposition not in EVIDENCE_REQUIRED or disposition.evidence:
        return

    reason = f"{disposition.disposition.value} disposition has no evidence citations"
    write_audit(
        ctx.audit_conn,
        AuditRecord(
            alert_id=disposition.alert_id,
            thread_id=disposition.alert_id,
            step="dispose",
            actor="system",
            event="compliance_blocked",
            detail={"disposition_type": disposition.disposition.value, "reason": reason},
        ),
    )
    raise ComplianceViolationError(
        alert_id=disposition.alert_id,
        disposition_type=disposition.disposition.value,
        reason=reason,
    )
