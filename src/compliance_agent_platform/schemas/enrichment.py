"""Data assembled by the ``enrich`` node from the (mock) provider adapters."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class WatchlistCandidate(BaseModel):
    """A full watchlist entry retrieved for a hit, with comparison features."""

    list_name: str
    entry_id: str
    primary_name: str
    aliases: list[str] = Field(default_factory=list)
    date_of_birth: date | None = None
    nationality: str | None = None
    programs: list[str] = Field(default_factory=list, description="Sanctions programs, e.g. 'SDGT'.")
    source_url: str | None = None

    # Derived match features (0..1), populated by the enrich node.
    name_similarity: float | None = None
    dob_match: bool | None = None
    nationality_match: bool | None = None


class CustomerProfile(BaseModel):
    """KYC data for the screened party, when it is a known customer."""

    subject_id: str
    legal_name: str
    date_of_birth: date | None = None
    nationality: str | None = None
    residence_country: str | None = None
    risk_rating: str | None = None


class EnrichmentBundle(BaseModel):
    candidates: list[WatchlistCandidate] = Field(default_factory=list)
    customer: CustomerProfile | None = None
    adverse_media_summaries: list[str] = Field(default_factory=list)

    # Set by the enrich node when it cannot gather enough to evaluate.
    insufficient_data: bool = False
    missing: list[str] = Field(default_factory=list)
