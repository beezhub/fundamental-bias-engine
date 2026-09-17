"""`MIN_COMPONENT_WEIGHT` and the pillars that sit exactly on it.

The floor says how much of a pillar's sub-weight a currency must hold before the
pillar is scored for it at all. Two halves of that rule are tested here, and they
run at different times.

The arithmetic half runs today. `component_weights` is implemented on every
pillar, so which subsets of components land exactly on the floor is a fact about
the committed sub-weights and can be pinned now. That is the half that went
wrong: EMPLOYMENT's two components are 0.50 each, so a currency missing one holds
exactly 0.50, and under a strict ``<`` the floor could never fire for the one
pillar whose own docstring says a single component is not safe on its own.

The behavioural half is still guarded, on one callable rather than three.
`blend_components` and `missing_score` have landed and enforce the floor;
`BasePillar.compute` is scaffolded pending #51, so a test that asks a pillar to
score a currency still raises ``NotImplementedError`` whatever the rule says.
Those tests skip while the scaffold marker is present and start asserting the
moment `compute` lands, in the pattern ``tests/test_smoke.py`` uses for modules
that have not arrived.

The blend's own half of the rule is covered meanwhile by
``tests/test_pillar_blend.py``, which drives `blend_components` directly and
does not need `compute`.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time
from itertools import combinations

import pytest

from fbe.config import ScoringConfig
from fbe.pillars.base import MIN_COMPONENT_WEIGHT, BasePillar
from fbe.pillars.employment import EmploymentPillar
from fbe.pillars.external import ExternalPillar
from fbe.pillars.growth import GrowthPillar
from fbe.pillars.inflation import InflationPillar
from fbe.pillars.monetary import MonetaryPillar
from fbe.types import Frequency, Observation

SCAFFOLD = re.compile(r"is scaffolded;")
"""The stub marker ``tests/test_stubs.py`` requires on every scaffolded callable.

Matching the message rather than calling the function and catching
``NotImplementedError`` keeps the guard honest: a real ``NotImplementedError``
raised from somewhere deeper once the pillar lands fails the test instead of
silently skipping it.

Only the first half of the message is matched. Every stub here writes it as two
adjacent string literals split over two lines, so the phrase and the roadmap
reference are never contiguous in the source that `inspect.getsource` returns.
``tests/test_stubs.py`` is what holds the full message to its form.
"""

MULTI_COMPONENT_PILLARS: tuple[BasePillar, ...] = (
    MonetaryPillar(),
    InflationPillar(),
    GrowthPillar(),
    EmploymentPillar(),
    ExternalPillar(),
)
"""The five pillars that blend sub-indicators. POSITIONING and RISK do not."""


def _skip_if_scaffolded(*functions: Callable[..., object]) -> None:
    """Skip the calling test while any of these callables is still a stub."""
    for function in functions:
        if SCAFFOLD.search(inspect.getsource(function)):
            pytest.skip(f"{function.__qualname__} is still scaffolded")


def _present(weights: Mapping[str, float], missing: tuple[str, ...]) -> float:
    """Sub-weight a currency holds when `missing` components are absent."""
    return round(sum(w for key, w in weights.items() if key not in missing), 10)


def _subsets_exactly_on_the_floor(weights: Mapping[str, float]) -> set[frozenset[str]]:
    """Every set of present components summing to exactly `MIN_COMPONENT_WEIGHT`."""
    keys = tuple(weights)
    found: set[frozenset[str]] = set()
    for size in range(1, len(keys) + 1):
        for present in combinations(keys, size):
            if round(sum(weights[key] for key in present), 10) == MIN_COMPONENT_WEIGHT:
                found.add(frozenset(present))
    return found


# --- the arithmetic, which is testable today --------------------------------


def test_employment_holds_exactly_the_floor_when_either_component_is_missing() -> None:
    """The case the rule was wrong about.

    Both sub-weights are 0.50, so losing either one leaves exactly the floor, not
    less than it. This is why the comparison has to be "at or below": under a
    strict ``<`` there is no missing-data state in which EMPLOYMENT is ever
    marked absent.
    """
    weights = EmploymentPillar().component_weights

    assert _present(weights, ("employment_trend",)) == MIN_COMPONENT_WEIGHT
    assert _present(weights, ("unemployment_6m",)) == MIN_COMPONENT_WEIGHT


def test_growth_holds_exactly_the_floor_on_gdp_and_retail_sales() -> None:
    """The arithmetic from the ruling on issue #15, which the shape still allows.

    A currency holding only the leading survey and industrial production sits at
    0.30 + 0.20, exactly the floor, and GROWTH is absent for it.

    This was the live instance until issue #23: ``indpro_yoy`` is manual-only for
    CHF, AUD and NZD and the manual PMI file held no values, so those three held
    GDP plus retail sales and lost the pillar. Substituting
    ``business_confidence_mfg``, which is verified 8 of 8, is what rescued them.
    The combination is still reachable, so the boundary still needs pinning.
    """
    weights = GrowthPillar().component_weights

    assert (
        _present(weights, ("business_confidence_mfg", "indpro_yoy"))
        == MIN_COMPONENT_WEIGHT
    )


def test_monetary_missing_only_cpi_clears_the_floor() -> None:
    """A currency without ``cpi_yoy`` loses ``real_policy_rate`` and nothing else.

    0.85 is above the floor under either comparison, so this currency still
    scores. The point of the test is that widening the comparison to "at or
    below" did not sweep up the ordinary missing-series case.
    """
    weights = MonetaryPillar().component_weights

    assert _present(weights, ("real_policy_rate",)) == 0.85
    assert _present(weights, ("real_policy_rate",)) > MIN_COMPONENT_WEIGHT


def test_the_subsets_sitting_exactly_on_the_floor_are_the_recorded_ones() -> None:
    """Pin which pillars can reach the boundary, so a sub-weight edit is visible.

    A change to any pillar's sub-weights that moves a combination onto or off the
    floor changes which currencies are scored as absent, and that is not
    something to discover from a report. The MONETARY entries are worth stating
    explicitly: the ruling on issue #15 said no combination of MONETARY's
    sub-weights reaches exactly 0.50, and two do. Both need the two-year yield
    series and its change keys to disagree about availability, which is possible
    because they arrive from the registry as separate keys rather than being
    differenced from one series.
    """
    expected = {
        "MONETARY": {
            frozenset({"yield_2y", "yield_2y_chg_3m"}),
            frozenset({"policy_rate", "yield_2y_chg_1m", "real_policy_rate"}),
        },
        "INFLATION": set(),
        "GROWTH": {
            frozenset({"gdp_yoy", "indpro_yoy"}),
            frozenset({"gdp_yoy", "retail_sales_yoy"}),
            frozenset({"business_confidence_mfg", "indpro_yoy"}),
            frozenset({"business_confidence_mfg", "retail_sales_yoy"}),
        },
        "EMPLOYMENT": {
            frozenset({"unemployment_6m"}),
            frozenset({"employment_trend"}),
        },
        "EXTERNAL": set(),
    }

    actual = {
        pillar.name.name: _subsets_exactly_on_the_floor(pillar.component_weights)
        for pillar in MULTI_COMPONENT_PILLARS
    }

    assert actual == expected


def test_every_pillar_sub_weight_set_still_sums_to_one() -> None:
    """The floor is a fraction of 1.0, so the premise has to hold."""
    for pillar in MULTI_COMPONENT_PILLARS:
        assert round(sum(pillar.component_weights.values()), 10) == 1.0


# --- the behaviour, which asserts once the pillar layer lands ---------------

UNIVERSE: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD")


def _full_coverage(
    pillar: BasePillar, asof: date, drop: Mapping[str, tuple[str, ...]]
) -> tuple[Observation, ...]:
    """Every indicator the pillar requires, for all eight currencies, minus `drop`.

    Deliberately not the ``sample_observations`` fixture. That set carries four
    indicators, and none of these three pillars can be brought to its sub-weight
    floor with it: EMPLOYMENT would have no ``employment_chg`` for any currency,
    so the whole cross-section would be absent and a per-currency assertion would
    pass for the wrong reason, and MONETARY would sit at 0.55 before anything was
    removed and below the floor after.

    Values vary by currency so the cross-section is not degenerate. They are
    arbitrary and mean nothing beyond being different from each other.

    Args:
        pillar: The pillar whose `requires` list defines the indicator set.
        asof: Period stamped on every observation, so nothing is stale.
        drop: ``{currency: indicators}`` to withhold, which is how a currency is
            put on the floor.
    """
    required = set(pillar.requires)
    for currency, indicators in drop.items():
        unknown = [name for name in indicators if name not in required]
        # A drop naming a key the pillar does not require withholds nothing, so
        # the currency keeps full coverage and the assertion about its floor
        # passes against a fixture that never put it on one. That is how the
        # two GROWTH cases below went on naming `pmi_composite` after #23
        # substituted it out of `requires`.
        assert not unknown, (
            f"{pillar.name} does not require {unknown} for {currency}, so "
            "dropping it withholds nothing"
        )

    observations: list[Observation] = []
    for offset, currency in enumerate(UNIVERSE):
        for indicator in pillar.requires:
            if indicator in drop.get(currency, ()):
                continue
            observations.append(
                Observation(
                    indicator=indicator,
                    currency=currency,
                    value=1.0 + offset * 0.25,
                    period=asof,
                    source="fixture",
                    series_id=f"{indicator.upper()}_{currency}",
                    unit="percent",
                    frequency=Frequency.MONTHLY,
                    released_at=datetime.combine(asof, time(12, 0), tzinfo=UTC),
                )
            )
    return tuple(observations)


def test_employment_is_absent_for_a_currency_holding_only_unemployment(
    asof: date,
) -> None:
    """Half of a two-component pillar is not enough to score it.

    The unemployment rate falls both when hiring is strong and when people leave
    the labour force, and the hiring series is what tells those apart. A currency
    with only the first is exactly the state `EmploymentPillar` documents as
    unsafe, so it must come back as `missing_score` with ``z`` set to ``None``
    rather than as a confident score on one series. The other seven currencies
    keep both series, so AUD's absence is the floor firing and not a thin
    cross-section.
    """
    _skip_if_scaffolded(
        BasePillar.compute, EmploymentPillar._extract, EmploymentPillar._transform
    )

    pillar = EmploymentPillar()
    scores = pillar.compute(
        _full_coverage(pillar, asof, drop={"AUD": ("employment_chg",)}),
        UNIVERSE,
        asof,
    )

    assert scores["AUD"].z is None
    assert scores["AUD"].raw is None
    assert scores["AUD"].score == 0.0
    assert scores["USD"].z is not None


def test_growth_is_absent_for_a_currency_holding_gdp_and_retail_sales_only(
    asof: date,
) -> None:
    """0.30 plus 0.20 is exactly the floor, so GROWTH is absent, not scored.

    The leading slot dropped here is ``business_confidence_mfg``. It was
    ``pmi_composite`` until #23 ruled the substitution, and this test went on
    naming the old key, which GROWTH no longer requires: nothing was withheld,
    CHF kept all four components and the assertion passed only while GROWTH
    could not run at all. `_full_coverage` now refuses an unknown drop.

    The combination is still reachable, although the three currencies it used
    to describe are not on it any more: ``indpro_yoy`` is manual-only for CHF,
    AUD and NZD, and the survey is verified 8 of 8, so it is the survey going
    dark rather than the PMI being absent that puts one of them here.
    """
    _skip_if_scaffolded(
        BasePillar.compute, GrowthPillar._extract, GrowthPillar._transform
    )

    pillar = GrowthPillar()
    scores = pillar.compute(
        _full_coverage(
            pillar, asof, drop={"CHF": ("business_confidence_mfg", "indpro_yoy")}
        ),
        UNIVERSE,
        asof,
    )

    assert scores["CHF"].z is None
    assert scores["USD"].z is not None


def test_growth_still_scores_a_currency_missing_only_the_survey(asof: date) -> None:
    """0.70 clears the floor, so one missing component of four still scores.

    The pair with the test above is the point. One missing component of four is a
    repair the renormalisation can make; two is not. This one also named
    ``pmi_composite``, so it dropped nothing and asserted that a currency with
    complete coverage scores, which is true of every currency in the fixture.
    """
    _skip_if_scaffolded(
        BasePillar.compute, GrowthPillar._extract, GrowthPillar._transform
    )

    pillar = GrowthPillar()
    scores = pillar.compute(
        _full_coverage(pillar, asof, drop={"CHF": ("business_confidence_mfg",)}),
        UNIVERSE,
        asof,
    )

    assert scores["CHF"].z is not None


def test_monetary_still_scores_a_currency_missing_only_cpi(asof: date) -> None:
    """0.85 clears the floor, so the ordinary missing-series case still scores.

    ``cpi_yoy`` feeds ``real_policy_rate`` and nothing else in this pillar, so
    losing it costs 0.15 of the sub-weight. The widened comparison must not
    sweep this up.
    """
    _skip_if_scaffolded(
        BasePillar.compute, MonetaryPillar._extract, MonetaryPillar._transform
    )

    pillar = MonetaryPillar()
    scores = pillar.compute(
        _full_coverage(pillar, asof, drop={"NZD": ("cpi_yoy",)}),
        UNIVERSE,
        asof,
    )

    assert scores["NZD"].z is not None


def test_the_floor_is_not_read_from_scoring_config() -> None:
    """`MIN_COMPONENT_WEIGHT` is a pillar-layer constant, not a configured value.

    Stated as a test because the two are easy to confuse: every other threshold
    in the scoring pipeline does live on `ScoringConfig`, and a future change
    that moves this one there has to move the comparison with it.
    """
    assert not hasattr(ScoringConfig(), "min_component_weight")
    assert MIN_COMPONENT_WEIGHT == 0.5
