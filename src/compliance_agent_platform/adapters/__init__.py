"""Provider adapters for the enrich node.

Week 1 ships mock implementations only; the Protocols in :mod:`.base` are the seam
where real watchlist / KYC / adverse-media integrations plug in later.
"""

from compliance_agent_platform.adapters.base import (
    AdverseMediaProvider,
    CustomerProvider,
    ScreeningDeps,
    WatchlistProvider,
)
from compliance_agent_platform.adapters.mock import default_deps

__all__ = [
    "AdverseMediaProvider",
    "CustomerProvider",
    "ScreeningDeps",
    "WatchlistProvider",
    "default_deps",
]
