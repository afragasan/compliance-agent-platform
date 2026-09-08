"""Structured output of the ``evaluate`` node and the HITL analyst decision."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from compliance_agent_platform.schemas.disposition import (
    DispositionType,
    EvidenceItem,
    MatchedEntity,
)


class EvaluationResult(BaseModel):
    """What the LLM returns for a screening alert. Not itself the final disposition."""

    recommended: DispositionType
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)
    matched_entities: list[MatchedEntity] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)


class AnalystResolution(str, Enum):
    CLEAR = "clear"
    TRUE_MATCH = "true_match"
    INSUFFICIENT_DATA = "insufficient_data"


class AnalystDecision(BaseModel):
    """Payload supplied via ``Command(resume=...)`` to close an escalated alert."""

    resolution: AnalystResolution
    analyst_id: str
    rationale: str = Field(min_length=1)
    matched_entities: list[MatchedEntity] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
