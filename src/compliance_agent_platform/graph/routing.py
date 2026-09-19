"""The single conditional edge: ``evaluate -> [escalate | dispose]``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from compliance_agent_platform.config import Settings
from compliance_agent_platform.schemas.disposition import DispositionType
from compliance_agent_platform.schemas.state import ScreeningState

Route = Literal["escalate", "dispose"]

# Policy: a candidate true match is always confirmed by a human before it is booked.
_ALWAYS_ESCALATE = {DispositionType.TRUE_MATCH.value, DispositionType.ESCALATE_TO_ANALYST.value}
# Policy: these are safe for the agent to finalize when it is confident enough.
_AGENT_TERMINAL = {DispositionType.CLEAR.value, DispositionType.INSUFFICIENT_DATA.value}


def make_route_after_evaluate(settings: Settings) -> Callable[[ScreeningState], Route]:
    def route_after_evaluate(state: ScreeningState) -> Route:
        evaluation = state["evaluation"]
        recommended = evaluation["recommended"]
        if recommended in _ALWAYS_ESCALATE:
            return "escalate"
        if recommended in _AGENT_TERMINAL:
            if evaluation["confidence"] >= settings.clear_confidence_threshold:
                return "dispose"
            return "escalate"
        return "escalate"

    return route_after_evaluate
