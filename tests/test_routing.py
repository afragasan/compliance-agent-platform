"""Truth table for the single conditional edge ``evaluate -> [escalate | dispose]``."""

from __future__ import annotations

import pytest

from compliance_agent_platform.config import Settings
from compliance_agent_platform.graph.routing import make_route_after_evaluate

THRESHOLD = 0.85
_route = make_route_after_evaluate(Settings(clear_confidence_threshold=THRESHOLD))


def _state(recommended: str, confidence: float) -> dict:
    return {"evaluation": {"recommended": recommended, "confidence": confidence}}


@pytest.mark.parametrize(
    ("recommended", "confidence", "expected"),
    [
        ("true_match", 0.99, "escalate"),  # always human-confirmed
        ("true_match", 0.10, "escalate"),
        ("escalate_to_analyst", 0.90, "escalate"),
        ("clear", 0.90, "dispose"),  # confident clear -> agent finalizes
        ("clear", THRESHOLD, "dispose"),  # boundary is inclusive
        ("clear", 0.50, "escalate"),  # not confident enough
        ("insufficient_data", 0.99, "dispose"),
        ("insufficient_data", 0.50, "escalate"),
    ],
)
def test_route_after_evaluate(recommended, confidence, expected):
    assert _route(_state(recommended, confidence)) == expected
