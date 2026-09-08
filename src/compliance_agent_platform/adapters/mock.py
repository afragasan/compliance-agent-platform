"""Deterministic, fixture-backed provider implementations for Week 1."""

from __future__ import annotations

from datetime import date

from compliance_agent_platform.adapters.base import ScreeningDeps
from compliance_agent_platform.schemas.alert import ScreeningAlert
from compliance_agent_platform.schemas.enrichment import CustomerProfile, WatchlistCandidate

# --- Fixture data ---------------------------------------------------------------

_WATCHLIST: dict[tuple[str, str], WatchlistCandidate] = {
    ("OFAC SDN", "SDN-12345"): WatchlistCandidate(
        list_name="OFAC SDN",
        entry_id="SDN-12345",
        primary_name="Viktor Petrov",
        aliases=["Victor Petroff", "V. Petrov"],
        date_of_birth=date(1969, 4, 12),
        nationality="RU",
        programs=["UKRAINE-EO13662", "RUSSIA-EO14024"],
        source_url="https://sanctionssearch.ofac.treas.gov/Details.aspx?id=12345",
    ),
    ("OFAC SDN", "SDN-88888"): WatchlistCandidate(
        list_name="OFAC SDN",
        entry_id="SDN-88888",
        primary_name="Maria Gonzalez Herrera",
        aliases=["Maria G. Herrera"],
        date_of_birth=date(1981, 11, 3),
        nationality="MX",
        programs=["SDNTK"],
        source_url="https://sanctionssearch.ofac.treas.gov/Details.aspx?id=88888",
    ),
}

_CUSTOMERS: dict[str, CustomerProfile] = {
    "CUST-1001": CustomerProfile(
        subject_id="CUST-1001",
        legal_name="Viktor Petrov",
        date_of_birth=date(1969, 4, 12),
        nationality="RU",
        residence_country="AE",
        risk_rating="high",
    ),
    "CUST-2002": CustomerProfile(
        subject_id="CUST-2002",
        legal_name="John A. Smith",
        date_of_birth=date(1990, 7, 22),
        nationality="US",
        residence_country="US",
        risk_rating="low",
    ),
}


# --- Providers -----------------------------------------------------------------


class MockWatchlistProvider:
    def fetch_entry(self, list_name: str, entry_id: str) -> WatchlistCandidate | None:
        entry = _WATCHLIST.get((list_name, entry_id))
        return entry.model_copy(deep=True) if entry else None


class MockCustomerProvider:
    def fetch_profile(self, subject_id: str) -> CustomerProfile | None:
        profile = _CUSTOMERS.get(subject_id)
        return profile.model_copy(deep=True) if profile else None


class MockAdverseMediaProvider:
    def search(self, alert: ScreeningAlert) -> list[str]:
        name = alert.screened_name.lower()
        if "petrov" in name:
            return [
                "2023 investigative report links Viktor Petrov to sanctioned energy trading network.",
            ]
        return []


def default_deps() -> ScreeningDeps:
    return ScreeningDeps(
        watchlist=MockWatchlistProvider(),
        customer=MockCustomerProvider(),
        adverse_media=MockAdverseMediaProvider(),
    )
