"""Inbound alert payload for the ``sanctions_screening`` queue."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class WatchlistHit(BaseModel):
    """A raw hit produced by the upstream screening engine (fuzzy name match)."""

    list_name: str = Field(description="e.g. 'OFAC SDN', 'EU Consolidated', 'UN'.")
    entry_id: str = Field(description="Stable identifier of the list entry.")
    matched_name: str
    score: float = Field(ge=0.0, le=1.0, description="Upstream match score.")


class ScreeningAlert(BaseModel):
    """Normalized screening alert. ``alert_id`` doubles as the LangGraph thread id."""

    alert_id: str
    queue: str = "sanctions_screening"

    # Screened party (typically a customer or a counterparty on a payment).
    subject_id: str | None = Field(default=None, description="Internal customer / party id.")
    screened_name: str
    date_of_birth: date | None = None
    nationality: str | None = None
    address_country: str | None = None

    # Context.
    transaction_ref: str | None = None
    hits: list[WatchlistHit] = Field(default_factory=list)
