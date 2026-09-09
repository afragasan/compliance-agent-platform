"""Typed disposition schema — the terminal, audited output of the graph."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class DispositionType(str, Enum):
    CLEAR = "clear"
    TRUE_MATCH = "true_match"
    ESCALATE_TO_ANALYST = "escalate_to_analyst"
    INSUFFICIENT_DATA = "insufficient_data"


class DecidedBy(str, Enum):
    AGENT = "agent"
    ANALYST = "analyst"


class MatchedEntity(BaseModel):
    list_name: str
    entry_id: str
    primary_name: str
    programs: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    """A citation into the enrichment data supporting the disposition."""

    source: str = Field(description="e.g. 'OFAC SDN', 'customer KYC', 'adverse media'.")
    detail: str
    reference: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Disposition(BaseModel):
    """Immutable decision record. Corrections are new records, never edits."""

    alert_id: str
    queue: str = "sanctions_screening"
    disposition: DispositionType
    rationale: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    matched_entities: list[MatchedEntity] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    decided_by: DecidedBy
    analyst_id: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _check_consistency(self) -> Disposition:
        if self.decided_by is DecidedBy.ANALYST and not self.analyst_id:
            raise ValueError("analyst_id is required when decided_by == 'analyst'")
        if self.decided_by is DecidedBy.AGENT and not self.model_id:
            raise ValueError("model_id is required when decided_by == 'agent'")
        if self.disposition is DispositionType.TRUE_MATCH and not self.matched_entities:
            raise ValueError("true_match disposition requires at least one matched_entity")
        if (
            self.decided_by is DecidedBy.AGENT
            and self.disposition in {DispositionType.CLEAR, DispositionType.TRUE_MATCH}
            and self.confidence is None
        ):
            raise ValueError("agent clear/true_match disposition requires a confidence score")
        return self
