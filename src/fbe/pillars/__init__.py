"""The seven fundamental pillars, and the default set the engine runs.

Each pillar answers one question about all eight currencies at once and returns
one `PillarScore` per currency on the shared ``-3..+3`` band, where positive
means fundamentally strong relative to the rest of the universe. `BasePillar`
holds the machinery they share; the modules beside it hold the economics.

Weights live in `ScoringConfig`, not here. A pillar does not know how much it
matters, which is what lets the weights be re-tuned without touching any of the
seven modules.
"""

from __future__ import annotations

from fbe.config import ScoringConfig
from fbe.pillars.base import BasePillar
from fbe.pillars.employment import EmploymentPillar
from fbe.pillars.external import ExternalPillar
from fbe.pillars.growth import GrowthPillar
from fbe.pillars.inflation import InflationPillar
from fbe.pillars.monetary import MonetaryPillar
from fbe.pillars.positioning import PositioningPillar
from fbe.pillars.risk import RiskPillar

__all__ = [
    "BasePillar",
    "MonetaryPillar",
    "InflationPillar",
    "GrowthPillar",
    "EmploymentPillar",
    "ExternalPillar",
    "PositioningPillar",
    "RiskPillar",
    "default_pillars",
    "required_indicators",
]


def default_pillars(config: ScoringConfig | None = None) -> tuple[BasePillar, ...]:
    """Build the seven pillars in the order they are reported.

    Order is presentation only, running from the fastest and heaviest driver to
    the slowest and lightest. The scorer weights by `ScoringConfig`, so the
    sequence has no effect on any number.

    Args:
        config: Scoring configuration handed to every pillar. Defaults to
            `ScoringConfig()`.

    Returns:
        One instance of each pillar class.
    """
    cfg = config or ScoringConfig()
    return (
        MonetaryPillar(cfg),
        InflationPillar(cfg),
        GrowthPillar(cfg),
        EmploymentPillar(cfg),
        ExternalPillar(cfg),
        PositioningPillar(cfg),
        RiskPillar(cfg),
    )


def required_indicators(
    pillars: tuple[BasePillar, ...] | None = None,
) -> tuple[str, ...]:
    """Collect every canonical indicator key the given pillars need.

    The runner uses this to decide what to fetch, so an indicator a pillar
    forgets to declare will not be there at compute time even if a source could
    have supplied it.

    Args:
        pillars: Pillars to inspect. Defaults to `default_pillars`.

    Returns:
        Sorted, de-duplicated indicator keys.
    """
    chosen = pillars if pillars is not None else default_pillars()
    keys: set[str] = set()
    for pillar in chosen:
        keys.update(pillar.requires)
    return tuple(sorted(keys))
