"""The LangGraph shared state for the sanctions-screening graph.

State channels hold **plain JSON-serializable dicts**, not Pydantic instances. Nodes
parse the relevant model at entry and write ``model.model_dump(mode="json")`` back.
This keeps every Postgres checkpoint human-readable (an audit win) and avoids coupling
the checkpoint wire format to our class paths.
"""

from __future__ import annotations

from typing import Annotated, TypedDict


def _extend(left: list, right: list) -> list:
    return (left or []) + (right or [])


class ScreeningState(TypedDict, total=False):
    alert: dict
    enrichment: dict
    evaluation: dict
    analyst_decision: dict
    disposition: dict
    errors: Annotated[list[str], _extend]
