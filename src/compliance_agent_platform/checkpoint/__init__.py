"""PostgreSQL checkpoint persistence for the screening graph."""

from compliance_agent_platform.checkpoint.postgres import open_checkpointer

__all__ = ["open_checkpointer"]
