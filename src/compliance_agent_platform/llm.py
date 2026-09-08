"""Bedrock chat-model factory.

All agent reasoning goes through Amazon Bedrock (Claude via the Converse API) so that
access is IAM-scoped and consistent with the eventual AgentCore Runtime deployment.
"""

from __future__ import annotations

from functools import lru_cache

from langchain.chat_models import init_chat_model

from compliance_agent_platform.config import get_settings


@lru_cache
def get_chat_model():
    """Return a deterministic (temperature 0) Bedrock chat model."""
    settings = get_settings()
    return init_chat_model(
        settings.bedrock_model_id,
        model_provider="bedrock_converse",
        region_name=settings.aws_region,
        temperature=0,
    )
