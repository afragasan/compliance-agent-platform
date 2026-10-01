"""Chat-model factory and inference routing.

Which backend serves a given queue is a policy, not an ``if`` in node code:
:func:`resolve_chat_model` looks up ``Settings.llm_routing_policy[queue]`` and returns a
:class:`ResolvedBackend` naming which backend/model actually answered (so audit rows can
record the truth even when a fallback fires), plus an optional fallback
``ResolvedBackend`` for the caller to retry against on failure. Today's policy routes
``sanctions_screening`` to the self-hosted NVIDIA NIM endpoint (low-latency, no
per-token cost) with Amazon Bedrock Claude as the automatic cloud fallback, and
``aml_analysis`` straight to Bedrock (reasoning-intensive, no latency pressure).

Setting the ``CAP_FAKE_MODEL`` environment variable to one of :data:`_FAKE_PROFILES`
short-circuits *before* the routing policy is consulted: it swaps in a deterministic
canned ``EvaluationResult`` and makes **no** network call at all (not NIM, not
Bedrock). It is for offline runs of ``cap run`` / ``cap resume``, CI smoke tests, and
routing tests — never for production, where the variable is absent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
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


class _FakeRawMessage:
    """Stand-in for the ``AIMessage`` a real ``include_raw=True`` call would return."""

    def __init__(self, content: str) -> None:
        self.content = content
        # Small fixed counts: deterministic, never zero (so cost-math tests can tell
        # "a call happened" apart from "no usage data available").
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


class _FakeStructured:
    def __init__(self, profile: str) -> None:
        self._profile = profile

    def invoke(self, messages: object, **kwargs: object) -> dict:  # noqa: ARG002
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
        parsed = EvaluationResult(
            recommended=recommended,
            confidence=confidence,
            rationale=f"[CAP_FAKE_MODEL={self._profile}] canned evaluation",
            matched_entities=matched,
            evidence=[],
        )
        return {
            "raw": _FakeRawMessage(parsed.rationale),
            "parsed": parsed,
            "parsing_error": None,
        }


class _FakeChatModel:
    def __init__(self, profile: str) -> None:
        self._profile = profile

    def with_structured_output(self, schema: object, **kwargs: object) -> _FakeStructured:  # noqa: ARG002
        return _FakeStructured(self._profile)


@dataclass(frozen=True)
class ResolvedBackend:
    """Which chat model actually answers a queue, and what to call it in the audit trail."""

    model: object
    backend: str  # "nim" | "bedrock" | "fake"
    model_id: str


def resolve_chat_model(queue: str) -> tuple[ResolvedBackend, ResolvedBackend | None]:
    """Resolve the primary chat model for ``queue`` per the routing policy.

    Returns ``(primary, fallback)`` — ``fallback`` is ``None`` unless the policy routes
    ``queue`` to ``"nim"`` and ``Settings.bedrock_fallback_enabled`` is true, in which
    case it's the Bedrock backend the caller should retry against if the primary call
    fails. ``CAP_FAKE_MODEL`` short-circuits ahead of the policy entirely, so offline
    tests never touch NIM or Bedrock and never get a fallback to worry about.
    """
    profile = os.environ.get("CAP_FAKE_MODEL")
    if profile:
        if profile not in _FAKE_PROFILES:
            raise ValueError(
                f"CAP_FAKE_MODEL={profile!r} is not valid; choose one of {sorted(_FAKE_PROFILES)}"
            )
        logger.warning("CAP_FAKE_MODEL active (%s) - no network call will be made", profile)
        return ResolvedBackend(_FakeChatModel(profile), "fake", f"fake:{profile}"), None

    settings = get_settings()
    backend = settings.llm_routing_policy.get(queue, "bedrock")
    if backend == "nim":
        primary = ResolvedBackend(_nim_chat_model(), "nim", settings.nim_model_id)
        fallback = None
        if settings.bedrock_fallback_enabled:
            fallback = ResolvedBackend(_bedrock_chat_model(), "bedrock", settings.bedrock_model_id)
        return primary, fallback
    return ResolvedBackend(_bedrock_chat_model(), "bedrock", settings.bedrock_model_id), None


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


@lru_cache
def _nim_chat_model():
    """Return a deterministic (temperature 0) client for the self-hosted NIM endpoint.

    NIM exposes an OpenAI-compatible ``/v1/chat/completions`` API, so this is
    ``model_provider="openai"`` pointed at ``nim_base_url`` rather than a new SDK — the
    same ``init_chat_model`` helper used for Bedrock above, just a different provider.
    """
    settings = get_settings()
    return init_chat_model(
        settings.nim_model_id,
        model_provider="openai",
        base_url=settings.nim_base_url,
        api_key=settings.nim_api_key,
        temperature=0,
    )
