"""Input (PII/jailbreak) and output (topic boundary) self-check rails around
`evaluate`'s LLM calls, via the real `nemoguardrails` library.

The self-check rail always runs on the *same resolved backend model* as the call it's
guarding (passed in by the caller, e.g. ``ctx.model``/``ctx.model_fallback`` in
``graph/nodes.py::make_evaluate``) - a NIM-routed call's guardrail check also goes to
NIM, a Bedrock-routed call's check also goes to Bedrock - so a PII-bearing prompt never
takes a third, unreviewed path.

Setting ``CAP_FAKE_GUARDRAILS`` short-circuits to a deterministic heuristic (block iff
the text contains the literal marker ``__BLOCK__``), the same offline-toggle pattern as
``CAP_FAKE_MODEL``/``CAP_FAKE_EMBEDDINGS`` in ``llm.py``/``embeddings.py``, so the unit
suite never constructs a real ``LLMRails`` instance or makes a self-check LLM call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from compliance_agent_platform.guardrails.errors import GuardrailViolationError

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent / "config"
_FAKE_BLOCK_MARKER = "__BLOCK__"

# One LLMRails per distinct resolved model instance (NIM vs Bedrock are each
# process-wide singletons already, per llm.py's @lru_cache factories), keyed by
# `id(model)` rather than the model itself since LangChain chat models aren't
# guaranteed hashable.
_RAILS_CACHE: dict[int, object] = {}


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    rail: str
    reason: str | None = None


def check_input(model: object, text: str) -> GuardrailResult:
    """Run the input rail (PII / jailbreak) over `text`; raises on block."""
    return _check(model, text, rail="input")


def check_output(model: object, text: str) -> GuardrailResult:
    """Run the output rail (topic boundary) over `text`; raises on block."""
    return _check(model, text, rail="output")


def _check(model: object, text: str, *, rail: str) -> GuardrailResult:
    # CAP_FAKE_MODEL implies fake guardrails too: it hands `evaluate` a plain
    # `_FakeChatModel`, not a real LangChain chat model, and nemoguardrails can only
    # wrap the latter - there is no real backend here for a self-check rail to run on.
    if os.environ.get("CAP_FAKE_GUARDRAILS") or os.environ.get("CAP_FAKE_MODEL"):
        result = _fake_check(text, rail=rail)
    else:
        result = _real_check(model, text, rail=rail)
    if not result.allowed:
        raise GuardrailViolationError(rail=result.rail, reason=result.reason)
    return result


def _fake_check(text: str, *, rail: str) -> GuardrailResult:
    logger.warning("CAP_FAKE_GUARDRAILS active (%s rail) - no nemoguardrails call made", rail)
    blocked = _FAKE_BLOCK_MARKER in text
    return GuardrailResult(
        allowed=not blocked,
        rail=rail,
        reason=f"contains {_FAKE_BLOCK_MARKER!r}" if blocked else None,
    )


def _real_check(model: object, text: str, *, rail: str) -> GuardrailResult:
    from nemoguardrails.rails.llm.options import RailStatus, RailType

    rails = _rails_for(model)
    role = "user" if rail == "input" else "assistant"
    rail_type = RailType.INPUT if rail == "input" else RailType.OUTPUT
    outcome = rails.check([{"role": role, "content": text}], rail_types=[rail_type])
    allowed = outcome.status != RailStatus.BLOCKED
    return GuardrailResult(
        allowed=allowed,
        rail=outcome.rail or rail,
        reason=None if allowed else f"blocked by '{outcome.rail or rail}' self-check rail",
    )


def _rails_for(model: object):
    key = id(model)
    rails = _RAILS_CACHE.get(key)
    if rails is None:
        from nemoguardrails import LLMRails, RailsConfig

        config = RailsConfig.from_path(str(_CONFIG_DIR))
        rails = LLMRails(config, llm=model)
        _RAILS_CACHE[key] = rails
    return rails
