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

from collections.abc import Mapping, Sequence

from fbe.config import ScoringConfig
from fbe.pillars.base import BasePillar
from fbe.pillars.employment import EmploymentPillar
from fbe.pillars.external import ExternalPillar
from fbe.pillars.growth import GrowthPillar
from fbe.pillars.inflation import InflationPillar
from fbe.pillars.monetary import MonetaryPillar
from fbe.pillars.positioning import PositioningPillar
from fbe.pillars.risk import RiskPillar
from fbe.types import PillarName

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


def default_pillars(
    config: ScoringConfig | None = None,
    blend_sd_history: Mapping[PillarName, Sequence[float]] | None = None,
) -> tuple[BasePillar, ...]:
    """Build the seven pillars in the order they are reported.

    Order is presentation only, running from the fastest and heaviest driver to
    the slowest and lightest. The scorer weights by `ScoringConfig`, so the
    sequence has no effect on any number.

    Args:
        config: Scoring configuration handed to every pillar. Defaults to
            `ScoringConfig()`.
        blend_sd_history: Per-pillar history of blend standard deviations from
            earlier runs, for the re-standardisation divisor in
            `BasePillar.blend_divisor`. The runner loads it from the stored
            reports under ``DataConfig.reports_dir``. A pillar missing from the
            mapping, or the whole argument being ``None``, means that pillar
            falls back to the current run's own standard deviation and records
            that it did.

    Returns:
        One instance of each pillar class.

    """
    cfg = config or ScoringConfig()
    hist = blend_sd_history or {}
    return tuple(
        cls(cfg, hist.get(cls.name))
        for cls in (
            MonetaryPillar,
            InflationPillar,
            GrowthPillar,
            EmploymentPillar,
            ExternalPillar,
            PositioningPillar,
            RiskPillar,
        )
    )


def required_indicators(
    pillars: tuple[BasePillar, ...] | None = None,
) -> tuple[str, ...]:
    """Collect every canonical indicator key the given pillars need.

    This does not decide what gets fetched, and nothing calls it.
    `fbe.datasources.collect.collect` defaults its ``indicators`` argument to
    every key in `fbe.datasources.registry.INDICATORS`, and ``fbe refresh``
    passes nothing, so a refresh fetches the whole registry including the keys
    no pillar reads. An earlier version of this docstring said the runner used
    this to decide what to fetch, and warned that an indicator a pillar forgot
    to declare would be absent at compute time. Neither was true, and the
    second was wrong in the direction that matters: the registry-wide fetch
    supplies the key anyway, so the failure it described cannot happen by this
    route. What does catch a pillar requiring a key the registry does not route
    is ``tests/test_registry_pillar_agreement.py``.

    Wiring `collect` to this list is the obvious-looking change, and it is the
    one to resist. Three reasons, each checked against the code:

    * ``docs/interfaces.md`` gives ``fbe refresh`` the job of filling the cache
      from every available source. Its one narrowing option, ``--source``,
      selects providers rather than indicators.
    * `registry.UNCONSUMED_INDICATORS` holds ``yield_10y``, ``pmi_composite``
      and ``equity_index`` registered but unread, so each stays cheap to
      re-adopt. ``yield_10y`` and ``equity_index`` carry eight fetchable legs
      apiece, and the registry-wide default is the only thing keeping those
      sixteen series arriving. Narrowing it would stop exercising their fetch
      path, so a source that broke for one of them would be found by whoever
      re-adopts it rather than on the morning it broke, and that docstring puts
      the cost of losing ``yield_10y`` at re-verifying eight sources.
      ``pmi_composite`` is unaffected either way: all eight of its legs are
      manual, so no fetch reaches it.
    * `fbe.datasources.collect.CollectionResult.gaps` is filled from
      `registry.stale_refs`, which iterates ``INDICATORS`` whole and answers
      from registry metadata rather than from what the run fetched. Narrowing
      the fetch without narrowing that leaves one result describing two
      different universes.

    What this is for is the argument to ``collect(indicators=...)`` when a
    caller does want a narrower fetch, and a direct answer to what a given
    pillar set needs. The seam is worth keeping. Only the description of it was
    wrong. Ruled on #301.

    Args:
        pillars: Pillars to inspect. ``None`` means `default_pillars`. An empty
            tuple is a caller asking about no pillars and returns no keys,
            which is deliberately not the same answer.

    Returns:
        Sorted, de-duplicated indicator keys: twenty of the registry's
        twenty-three today, the three left out being exactly
        `registry.UNCONSUMED_INDICATORS`.

    """
    chosen = pillars if pillars is not None else default_pillars()
    keys: set[str] = set()
    for pillar in chosen:
        keys.update(pillar.requires)
    return tuple(sorted(keys))
