"""Every canonical key a pillar asks for must resolve in the registry.

The bug this guards against does not raise. A pillar declares a key in
`requires`, the registry holds the same concept under a different spelling,
and `fbe.datasources.registry.series_for` is never even called with a key that
would tell it apart from a key nobody ever supplies: the indicator is simply
absent from every observation set the runner builds, and the pillar quietly
renormalises around it or drops the whole currency, exactly the outcome
`docs/data-sources.md` and the engineering-standards skill call a silently
reduced pillar rather than an error.

On the commit before this test was added, `default_pillars()` asked for nine
keys with no entry in `fbe.datasources.registry.INDICATORS` at all:
``yield_2y_chg_1m``, ``yield_2y_chg_3m`` (MONETARY), ``pmi_composite``,
``indpro_yoy`` (GROWTH), ``employment_chg`` (EMPLOYMENT),
``current_account_gdp``, ``commodity_price`` (EXTERNAL), ``cot_net_pct_oi``
(POSITIONING) and ``vol_index`` (RISK). Every one of the seven that named an
existing concept was a registry spelled differently
(``employment_change``, ``industrial_production_yoy``, ``pmi_manufacturing``,
``current_account``, ``commodity_index``, ``cot_net_position``, ``vix``); the
two two-year-yield momentum keys had no registry entry under any spelling and
were added as their own `IndicatorSpec` rows, derived from `yield_2y`'s own
identifiers. See issue #7.
"""

from __future__ import annotations

import pytest

from fbe.datasources.registry import INDICATORS
from fbe.pillars import default_pillars

REQUIRED_KEYS: tuple[tuple[str, str], ...] = tuple(
    (pillar.name.name, key) for pillar in default_pillars() for key in pillar.requires
)
"""``(pillar name, indicator key)`` for every key every default pillar
declares, computed once at collection time so a new pillar or a new required
key is covered automatically without editing this file."""


@pytest.mark.parametrize(
    "pillar_name, key",
    REQUIRED_KEYS,
    ids=[f"{p}:{k}" for p, k in REQUIRED_KEYS],
)
def test_every_required_indicator_resolves_in_the_registry(
    pillar_name: str, key: str
) -> None:
    """A key a pillar declares in `requires` must be a real registry entry.

    Failing here means one of two things, and the assertion message names
    which key and which pillar so the fix is not a guessing game: either the
    pillar's spelling drifted from the registry's, or the indicator was never
    added to the registry at all. Neither is something a pillar should
    discover at run time by silently seeing no observations.
    """
    assert key in INDICATORS, (
        f"{pillar_name} pillar requires {key!r}, which is not a key in "
        "fbe.datasources.registry.INDICATORS. Either the registry spells "
        "this indicator differently, or it has never been added."
    )


def test_required_keys_cover_every_default_pillar() -> None:
    """Guard the guard: a pillar with an empty `requires` would pass silently.

    `REQUIRED_KEYS` is built from `default_pillars()`, so a pillar that failed
    to declare any `requires` at all would contribute nothing to
    `test_every_required_indicator_resolves_in_the_registry` and the suite
    would report green while checking nothing for it. Pinning the pillar
    names this run actually covers makes that omission visible.
    """
    covered = {pillar_name for pillar_name, _ in REQUIRED_KEYS}
    expected = {pillar.name.name for pillar in default_pillars()}
    assert covered == expected
