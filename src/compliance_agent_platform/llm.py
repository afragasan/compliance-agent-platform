"""Chat-model factory.

By default all agent reasoning goes through Amazon Bedrock (Claude via the Converse
API) so access is IAM-scoped and consistent with the eventual AgentCore Runtime
deployment.

Setting the ``CAP_FAKE_MODEL`` environment variable to one of :data:`_FAKE_PROFILES`
swaps in a deterministic canned ``EvaluationResult`` and makes **no** Bedrock call. It is
for offline runs of ``cap run`` / ``cap resume``, CI smoke tests, and routing tests —
never for production, where the variable is absent.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from langchain.chat_models import init_chat_model

from compliance_agent_platform.config import get_settings
from compliance_agent_platform.schemas.disposition import DispositionType, MatchedEntity
from compliance_agent_platform.schemas.evaluation import EvaluationResult

logger = logging.getLogger(__name__)

# CAP_FAKE_MODEL value -> (recommended disposition, confidence)
_FAKE_PROFILES: dict[str, tuple[DispositionType, float]] = {
    "clear": (DispositionType.CLEAR, 0.95),
    "clear_low": (DispositionType.CLEAR, 0.40),  # exercises confidence < threshold -> escalate
    "true_match": (DispositionType.TRUE_MATCH, 0.93),
    "escalate": (DispositionType.ESCALATE_TO_ANALYST, 0.60),
    "escalate_to_analyst": (DispositionType.ESCALATE_TO_ANALYST, 0.60),
    "insufficient_data": (DispositionType.INSUFFICIENT_DATA, 0.90),
}


class _FakeStructured:
    def __init__(self, profile: str) -> None:
        self._profile = profile

    def invoke(self, messages: object) -> EvaluationResult:  # noqa: ARG002 - signature parity
        recommended, confidence = _FAKE_PROFILES[self._profile]
        matched = (
            [
                MatchedEntity(
                    list_name="OFAC SDN",
                    entry_id="SDN-FAKE",
                    primary_name="Fake Match",
                    programs=["TEST"],
                )
            ]
            if recommended is DispositionType.TRUE_MATCH
            else []
        )
        return EvaluationResult(
            recommended=recommended,
            confidence=confidence,
            rationale=f"[CAP_FAKE_MODEL={self._profile}] canned evaluation",
            matched_entities=matched,
            evidence=[],
        )


class _FakeChatModel:
    def __init__(self, profile: str) -> None:
        self._profile = profile

    def with_structured_output(self, schema: object) -> _FakeStructured:  # noqa: ARG002
        return _FakeStructured(self._profile)


def get_chat_model():
    """Return the chat model: a canned fake when ``CAP_FAKE_MODEL`` is set, else Bedrock."""
    profile = os.environ.get("CAP_FAKE_MODEL")
    if profile:
        if profile not in _FAKE_PROFILES:
            raise ValueError(
                f"CAP_FAKE_MODEL={profile!r} is not valid; choose one of {sorted(_FAKE_PROFILES)}"
            )
        logger.warning("CAP_FAKE_MODEL active (%s) - no Bedrock call will be made", profile)
        return _FakeChatModel(profile)
    return _bedrock_chat_model()


@lru_cache
def _bedrock_chat_model():
    """Return a deterministic (temperature 0) Bedrock chat model."""
    settings = get_settings()
    return init_chat_model(
        settings.bedrock_model_id,
        model_provider="bedrock_converse",
        region_name=settings.aws_region,
        temperature=0,
    )
