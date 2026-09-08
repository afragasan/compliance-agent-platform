"""Invocation surface.

:func:`compliance_agent_platform.runtime.contract.invoke` is written to the Amazon
Bedrock AgentCore Runtime entrypoint contract (``(payload: dict, context) -> dict``).
Week 1 calls it directly from Python / the CLI; Week 2 decorates it with
``@app.entrypoint`` on a ``BedrockAgentCoreApp``.
"""

from compliance_agent_platform.runtime.contract import invoke

__all__ = ["invoke"]
