"""Adapter Protocols and the dependency bundle injected into the graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from compliance_agent_platform.schemas.alert import ScreeningAlert
from compliance_agent_platform.schemas.enrichment import CustomerProfile, WatchlistCandidate


class WatchlistProvider(Protocol):
    def fetch_entry(self, list_name: str, entry_id: str) -> WatchlistCandidate | None: ...


class CustomerProvider(Protocol):
    def fetch_profile(self, subject_id: str) -> CustomerProfile | None: ...


class AdverseMediaProvider(Protocol):
    def search(self, alert: ScreeningAlert) -> list[str]: ...


@dataclass(frozen=True)
class ScreeningDeps:
    """Everything the graph nodes need from the outside world."""

    watchlist: WatchlistProvider
    customer: CustomerProvider
    adverse_media: AdverseMediaProvider
