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

from fbe.datasources.registry import INDICATORS, UNCONSUMED_INDICATORS
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


# ----------------------------------------------------------------------
# The other direction: an indicator attributed to a pillar nothing asks for
# ----------------------------------------------------------------------
#
# The check above runs from the pillars to the registry and catches a key a
# pillar declares that no registry entry answers. This one runs the other way
# and catches the mirror image: a registered `IndicatorSpec` that names a
# pillar in its ``pillar`` field which that pillar never asks for.
#
# That orphan is quieter than the first case, because it produces no missing
# data. It produces a coverage figure. `yield_10y` sat at a verified 8/8 with
# `pillar=PillarName.MONETARY`, and both its own description and
# `docs/data-sources.md` read that attribution as consumption and said the
# 10-year carried MONETARY for the two currencies with no 2-year. It does not:
# MONETARY has no 10-year term at any sub-weight, so those two currencies lose
# the pillar outright. An operator reading the coverage table saw a healthy
# input where there was a series nothing reads. See issue #26.

ATTRIBUTED_KEYS: tuple[tuple[str, str], ...] = tuple(
    (spec.pillar.name, key)
    for key, spec in sorted(INDICATORS.items())
    if spec.pillar is not None
)
"""``(pillar name, indicator key)`` for every registry entry naming a pillar.

Computed from `INDICATORS` at collection time for the same reason
`REQUIRED_KEYS` is: a new indicator is covered without editing this file, and
nothing here restates a key name that a rename would leave stale.
"""


def _required_by(pillar_name: str) -> frozenset[str]:
    """Keys the named pillar declares, read from the pillar itself."""
    return frozenset(
        key
        for pillar in default_pillars()
        if pillar.name.name == pillar_name
        for key in pillar.requires
    )


@pytest.mark.parametrize(
    "pillar_name, key",
    ATTRIBUTED_KEYS,
    ids=[f"{p}:{k}" for p, k in ATTRIBUTED_KEYS],
)
def test_every_attributed_indicator_is_consumed_or_declared_unconsumed(
    pillar_name: str, key: str
) -> None:
    """An indicator naming a pillar is either asked for or listed as held.

    Both sides are read from their real sources, the pillar's own ``requires``
    and `UNCONSUMED_INDICATORS`, so a rename moves both at once and this stays
    silent about it. There is no expected-key list here to go stale.

    If this fails, the two honest fixes are to add the key to the pillar's
    ``requires``, which is a scoring decision and not a lane's to take, or to
    record it in `UNCONSUMED_INDICATORS` with its description saying what it is
    held for. Removing the ``pillar`` attribution is a third option and a worse
    one: it loses the record of where the series would belong.
    """
    consumed = key in _required_by(pillar_name)
    declared = key in UNCONSUMED_INDICATORS

    assert consumed or declared, (
        f"{key!r} is registered with pillar={pillar_name} but that pillar's "
        f"requires does not name it, and it is not in "
        f"UNCONSUMED_INDICATORS. Its coverage figure will read as a live "
        "input for a series nothing consumes."
    )


def test_nothing_declared_unconsumed_is_actually_consumed() -> None:
    """The other way round, so the declaration cannot quietly go stale.

    If a pillar later starts asking for a key listed here, the list is wrong
    and every reader it was written for is now being misled in the opposite
    direction. Cheaper to fail here than to have the document say a live input
    is held in reserve.
    """
    wrongly_listed = {
        key
        for key in UNCONSUMED_INDICATORS
        for pillar in default_pillars()
        if key in pillar.requires
    }
    assert not wrongly_listed, (
        f"{sorted(wrongly_listed)} are in UNCONSUMED_INDICATORS but a pillar "
        "requires them. Remove them from the set."
    )


def test_every_declared_unconsumed_key_is_registered() -> None:
    """A name in the set that is not a registry key protects nothing.

    A typo, or a key removed from the registry without the set being updated,
    would leave an entry that can never match and a real orphan that the
    parametrised check would then have to catch on its own.
    """
    unknown = UNCONSUMED_INDICATORS - set(INDICATORS)
    assert not unknown, (
        f"{sorted(unknown)} are in UNCONSUMED_INDICATORS but are not keys in "
        "INDICATORS."
    )
