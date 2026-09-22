"""Tests for GROWTH's pillar-owned selection and transformation.

Four components on four clocks. That is the whole difficulty of this pillar and
it is why the component-level freshness discount was built: GDP is quarterly and
lands one to two months after the quarter closes, industrial production and
retail sales are monthly, and the survey is monthly for USD, EUR, GBP and CHF
and quarterly for JPY, CAD, AUD and NZD. A single pillar-level age reports
whichever series happened to update last and carries the rest at full weight, so
several tests here read the per-component factors rather than
``PillarScore.staleness_days``.

Two unit traps are tested directly because both produce a plausible ranking
rather than an exception. ``business_confidence_mfg`` is a percentage balance,
neutral at zero and routinely negative in a healthy economy; a diffusion index
is neutral at 50. Code written against distance from 50 and pointed at this
series shifts every currency the same way, the cross-sectional z-score absorbs
the offset, and the ranking still looks orderly. And a balance of ``-7.5`` is a
reading, so anything that treats a falsy or negative value as absent loses a
real print and renormalises the pillar around a gap that is not there.

Nothing here reaches the network. Every value that reaches an assertion is
written in this file, so each component is checkable by hand. The registry is
read for two things only, an indicator's unit and its frequency, so a fixture
carries the real ones rather than a plausible-looking stand-in, and for the
staleness allowances the freshness assertions are computed against.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

import pytest

from fbe.config import ScoringConfig
from fbe.datasources.registry import (
    CYCLE_DAYS,
    DEFAULT_PUBLICATION_LAG_DAYS,
    INDICATORS,
    full_weight_age,
    series_for,
    staleness_allowance,
)
from fbe.pillars.base import MIN_COMPONENT_WEIGHT, BasePillar
from fbe.pillars.growth import GrowthPillar
from fbe.scoring import freshness
from fbe.types import Frequency, Observation
from fbe.universe import G10

ASOF = date(2026, 9, 15)

EXPECTED_REQUIRES = (
    "gdp_yoy",
    "business_confidence_mfg",
    "indpro_yoy",
    "retail_sales_yoy",
)
"""Spelled out rather than read off the pillar, so a key silently dropped from
`requires` fails here rather than quietly shrinking what these tests cover."""

UNITS = {key: INDICATORS[key].unit for key in EXPECTED_REQUIRES}
FREQUENCIES = {key: INDICATORS[key].frequency for key in EXPECTED_REQUIRES}

MONTHLY_PERIOD = date(2026, 8, 1)
"""45 days before `ASOF`, which is inside every monthly series' own ramp."""

QUARTERLY_PERIOD = date(2026, 7, 1)
"""76 days before `ASOF`, which is inside ``gdp_yoy``'s 270-day ramp."""

QUARTERLY_SURVEY_PERIOD = date(2026, 4, 1)
"""167 days before `ASOF`.

A punctual quarterly survey print, and the case the global 45-day ceiling
expires on the day it publishes. It is well inside the 270 the registry derives
from the quarterly half of this series.
"""

FOUR = ("USD", "EUR", "GBP", "JPY")
"""Enough to clear `fbe.pillars.base.MIN_CROSS_SECTION`, small enough to read."""


@pytest.fixture
def pillar() -> GrowthPillar:
    return GrowthPillar()


def obs(
    indicator: str,
    currency: str,
    value: float,
    period: date,
    *,
    released_at: datetime | None = None,
    revision: int = 0,
    unit: str | None = None,
    frequency: Frequency | None = None,
) -> Observation:
    """Build one observation, defaulting to the registry's own unit and frequency."""
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=period,
        source="test",
        series_id=indicator,
        unit=unit if unit is not None else UNITS[indicator],
        frequency=frequency if frequency is not None else FREQUENCIES[indicator],
        released_at=released_at,
        revision=revision,
    )


def published(period: date, days_after: int = 20) -> datetime:
    """A release timestamp a given number of days after the period starts."""
    stamped = date.fromordinal(period.toordinal() + days_after)
    return datetime(stamped.year, stamped.month, stamped.day, 12, 0, tzinfo=UTC)


def activity(
    currency: str,
    gdp: float | None = None,
    survey: float | None = None,
    indpro: float | None = None,
    retail: float | None = None,
    *,
    survey_period: date = MONTHLY_PERIOD,
) -> list[Observation]:
    """One visible print per series for a currency, skipping the ``None`` ones.

    ``None`` means the currency published nothing for that series, which is the
    state every absence test here is built from. It is never a value.
    """
    built: list[Observation] = []
    for indicator, value, period in (
        ("gdp_yoy", gdp, QUARTERLY_PERIOD),
        ("business_confidence_mfg", survey, survey_period),
        ("indpro_yoy", indpro, MONTHLY_PERIOD),
        ("retail_sales_yoy", retail, MONTHLY_PERIOD),
    ):
        if value is None:
            continue
        built.append(
            obs(indicator, currency, value, period, released_at=published(period))
        )
    return built


UNIVERSE: dict[str, tuple[float | None, float | None, float | None, float | None]] = {
    "USD": (2.4, -1.5, 1.8, 3.1),
    "EUR": (1.2, -7.5, 0.4, 1.6),
    "GBP": (0.8, -12.0, -1.1, 0.9),
    "JPY": (1.6, 2.0, 0.7, 2.2),
}
"""Four currencies, no two sharing a value on any series.

Every series has to separate all four, because `cross_sectional_z` refuses a
cross-section whose readings are all equal and would turn a real assertion into
a comparison of two ``None``s.
"""


Readings = tuple[float | None, float | None, float | None, float | None]


def universe(overrides: Mapping[str, Readings] | None = None) -> list[Observation]:
    """Build the four-currency fixture, replacing any currency's readings."""
    replaced: dict[str, Readings] = dict(UNIVERSE) | dict(overrides or {})
    built: list[Observation] = []
    for currency, values in replaced.items():
        built.extend(activity(currency, *values))
    return built


def component_values(
    pillar: GrowthPillar,
    observations: Sequence[Observation],
    currencies: Sequence[str] = FOUR,
    asof: date = ASOF,
) -> Mapping[str, Mapping[str, float | None]]:
    """Run the two pillar-owned steps and return `_transform`'s output."""
    return pillar._transform(pillar._extract(observations, currencies, asof), asof)


# --- what the pillar declares ------------------------------------------------


def test_the_pillar_asks_for_the_four_series_it_blends(pillar: GrowthPillar) -> None:
    """Guards every test below, which would narrow silently with `requires`."""
    assert tuple(pillar.requires) == EXPECTED_REQUIRES


def test_the_sub_weights_match_the_published_table(pillar: GrowthPillar) -> None:
    """Section 3.3 of `docs/scoring-spec.md`, which the floor tests count against."""
    assert pillar.component_weights == {
        "gdp_yoy": 0.30,
        "business_confidence_mfg": 0.30,
        "indpro_yoy": 0.20,
        "retail_sales_yoy": 0.20,
    }


def test_the_pillar_defines_no_selection_logic_of_its_own() -> None:
    """The look-ahead rule lives in one place, which is what #122 bought.

    A copy of the visibility or vintage rule in this module would drift from
    `BasePillar`'s, and a drifted copy leaks look-ahead into one pillar only,
    which is the hardest version of the bug to find.
    """
    for helper in ("_visible", "_newest_vintages", "_extract"):
        assert helper not in vars(GrowthPillar), (
            f"GROWTH defines its own {helper}, so #121's fix would land on "
            "BasePillar and be missed here"
        )
    assert GrowthPillar._extract is BasePillar._extract
    assert GrowthPillar._visible is BasePillar._visible
    assert GrowthPillar._newest_vintages is BasePillar._newest_vintages


# --- what _extract selects ---------------------------------------------------


def test_every_required_key_is_present_for_every_currency(
    pillar: GrowthPillar,
) -> None:
    """A dropped key and an empty sequence read the same and are different facts."""
    extracted = pillar._extract(activity("USD", 2.4, -1.5, 1.8, 3.1), FOUR, ASOF)

    assert set(extracted) == set(FOUR)
    for currency in FOUR:
        assert set(extracted[currency]) == set(EXPECTED_REQUIRES)
    for indicator in EXPECTED_REQUIRES:
        assert extracted["EUR"][indicator] == ()


def test_observations_come_back_sorted_by_period(pillar: GrowthPillar) -> None:
    """`_transform` reads the last element as the newest, so the order matters."""
    periods = [date(2026, 6, 1), date(2026, 4, 1), date(2026, 8, 1), date(2026, 5, 1)]
    scrambled = [
        obs("retail_sales_yoy", "USD", 1.0, period, released_at=published(period))
        for period in periods
    ]

    found = pillar._extract(scrambled, ("USD",), ASOF)["USD"]["retail_sales_yoy"]

    assert [item.period for item in found] == sorted(periods)


def test_an_observation_not_yet_published_takes_no_part(pillar: GrowthPillar) -> None:
    """Period before the run date, release after it: the shape that flatters a backtest.

    A run dated 15 April that filtered on period would score the middle of April
    with a GDP figure that does not exist for another ten days.
    """
    unpublished = obs(
        "gdp_yoy",
        "USD",
        9.9,
        QUARTERLY_PERIOD,
        released_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
    )

    extracted = pillar._extract(
        [*activity("USD", 2.4, -1.5, 1.8, 3.1), unpublished], ("USD",), ASOF
    )
    components = pillar._transform(extracted, ASOF)

    assert 9.9 not in [item.value for item in extracted["USD"]["gdp_yoy"]]
    assert components["USD"]["gdp_yoy"] == 2.4


def test_the_newest_vintage_visible_at_asof_wins(pillar: GrowthPillar) -> None:
    """A revision the run could not see must not displace the estimate it could."""
    vintages = [
        obs(
            "gdp_yoy",
            "USD",
            1.0,
            QUARTERLY_PERIOD,
            released_at=published(QUARTERLY_PERIOD),
            revision=0,
        ),
        obs(
            "gdp_yoy",
            "USD",
            2.4,
            QUARTERLY_PERIOD,
            released_at=published(QUARTERLY_PERIOD, 40),
            revision=1,
        ),
        obs(
            "gdp_yoy",
            "USD",
            8.8,
            QUARTERLY_PERIOD,
            released_at=datetime(2026, 12, 1, 12, 0, tzinfo=UTC),
            revision=2,
        ),
    ]

    components = pillar._transform(pillar._extract(vintages, ("USD",), ASOF), ASOF)

    assert components["USD"]["gdp_yoy"] == 2.4


# --- units, and the two ways this pillar's units go wrong --------------------


def test_the_confidence_balance_is_carried_as_the_oecd_published_it(
    pillar: GrowthPillar,
) -> None:
    """A percentage balance is neutral at zero, not at 50.

    Rebasing a balance onto a diffusion index's scale, or scoring distance from
    50, shifts every currency the same way. The cross-sectional z-score then
    absorbs the offset and the ranking still looks orderly, so this is asserted
    on the value rather than on the ranking.
    """
    values = component_values(pillar, universe())

    for currency, (_, survey, _, _) in UNIVERSE.items():
        assert values[currency]["business_confidence_mfg"] == survey


def test_a_negative_balance_is_a_reading_and_not_an_absence(
    pillar: GrowthPillar,
) -> None:
    """A healthy economy routinely prints a negative balance.

    Anything that treats a falsy or negative value as missing loses a real print
    and renormalises the pillar around a gap that is not there.
    """
    values = component_values(pillar, universe({"USD": (2.4, -18.0, 1.8, 3.1)}))

    assert values["USD"]["business_confidence_mfg"] == -18.0


def test_the_three_year_on_year_series_are_carried_as_published(
    pillar: GrowthPillar,
) -> None:
    """Levels, in percent, nothing applied. Section 3.3 says "Level" for all four."""
    values = component_values(pillar, universe())

    for currency, (gdp, _, indpro, retail) in UNIVERSE.items():
        assert values[currency]["gdp_yoy"] == gdp
        assert values[currency]["indpro_yoy"] == indpro
        assert values[currency]["retail_sales_yoy"] == retail


def test_no_component_is_rebased_against_another(pillar: GrowthPillar) -> None:
    """Two series printing the same number come through as the same number.

    The balance and the year-on-year rates are on different scales and neither
    is converted to the other. If either carried a per-series offset, one of
    these two would move.
    """
    values = component_values(pillar, universe({"USD": (-7.5, -7.5, 1.8, 3.1)}))

    assert values["USD"]["gdp_yoy"] == -7.5
    assert values["USD"]["business_confidence_mfg"] == -7.5


def test_the_newest_print_wins_over_an_older_one(pillar: GrowthPillar) -> None:
    """Each component is the newest published value, not the first or the average."""
    older = date(2026, 5, 1)
    observations = [
        *activity("USD", 2.4, -1.5, 1.8, 3.1),
        obs("retail_sales_yoy", "USD", 0.1, older, released_at=published(older)),
    ]

    values = component_values(pillar, observations, ("USD",))

    assert values["USD"]["retail_sales_yoy"] == 3.1


# --- absence, and the zero that is a reading ---------------------------------


def test_a_missing_reading_is_none_and_never_zero(pillar: GrowthPillar) -> None:
    """Three currencies, three different gaps, and no substituted value anywhere.

    Zero is a reading: an economy growing at exactly 0.0% year on year is a
    finding. A missing print filed as 0.0 would drag that currency to the middle
    of the cross-section on the strength of an outage.
    """
    values = component_values(
        pillar,
        universe(
            {
                "USD": (2.4, -1.5, None, 3.1),
                "EUR": (None, -7.5, 0.4, 1.6),
                "GBP": (0.8, -12.0, -1.1, None),
            }
        ),
    )

    assert values["USD"]["indpro_yoy"] is None
    assert values["EUR"]["gdp_yoy"] is None
    assert values["GBP"]["retail_sales_yoy"] is None
    for currency in ("USD", "EUR", "GBP"):
        for component, value in values[currency].items():
            assert value != 0.0, f"{currency} {component} was filled with a zero"


def test_a_reading_of_zero_survives_as_a_reading(pillar: GrowthPillar) -> None:
    """The converse, and what stops the test above passing against a pillar that
    refuses every 0.0."""
    values = component_values(pillar, universe({"USD": (0.0, 0.0, 0.0, 0.0)}))

    for component in EXPECTED_REQUIRES:
        assert values["USD"][component] == 0.0


def test_a_currency_with_nothing_at_all_produces_four_absences(
    pillar: GrowthPillar,
) -> None:
    """Every component key is present, because a dropped key is a different fact."""
    values = component_values(pillar, universe({"EUR": (None, None, None, None)}))

    assert set(values["EUR"]) == set(EXPECTED_REQUIRES)
    assert all(value is None for value in values["EUR"].values())


# --- signs, one component at a time ------------------------------------------


@pytest.mark.parametrize(
    ("component", "index"),
    [
        ("gdp_yoy", 0),
        ("business_confidence_mfg", 1),
        ("indpro_yoy", 2),
        ("retail_sales_yoy", 3),
    ],
)
def test_faster_activity_is_a_higher_score(
    pillar: GrowthPillar, component: str, index: int
) -> None:
    """Higher is positive on all four, and none of them is inverted.

    Asserted one component at a time, because an inverted sign on one of the
    two 0.20 components is outvoted by the other three and leaves the ranking
    looking sensible.
    """
    readings = list(UNIVERSE["GBP"])
    highest = max(values[index] for values in UNIVERSE.values())
    assert highest is not None
    readings[index] = highest + 5.0
    raised: Readings = (readings[0], readings[1], readings[2], readings[3])

    before = pillar.compute(universe(), FOUR, ASOF)
    after = pillar.compute(universe({"GBP": raised}), FOUR, ASOF)

    assert after["GBP"].score > before["GBP"].score


def test_each_component_carries_its_sign_justification_in_the_docstring() -> None:
    """Criterion: one line per component, in the docstring, saying why.

    A sign rule stated nowhere is a sign rule nobody can check against the
    economics, and this pillar has four of them.

    The marker is asserted separately and the block is bounded by the next
    blank line. An earlier version split on a string that was not there yet,
    which handed the whole docstring back and passed against the component
    list at the top of it, so deleting every justification would not have
    failed it.
    """
    assert GrowthPillar.__doc__ is not None
    marker = "Sign rule, one line per component"
    assert marker in GrowthPillar.__doc__
    block = GrowthPillar.__doc__.split(marker, 1)[1].split("\n\n", 1)[0]
    for component in EXPECTED_REQUIRES:
        assert f"``{component}``" in block


def test_moving_one_currency_moves_the_others(pillar: GrowthPillar) -> None:
    """The score is a rank against the others, not a reading of one economy.

    A pillar that scored each currency in isolation passes every single-currency
    test above and fails this one.
    """
    before = pillar.compute(universe(), FOUR, ASOF)
    after = pillar.compute(universe({"GBP": (9.9, 15.0, 8.8, 9.1)}), FOUR, ASOF)

    for currency in ("USD", "EUR", "JPY"):
        assert after[currency].score != before[currency].score


# --- the blend, which this pillar delegates ----------------------------------


def test_a_component_only_some_currencies_have_is_scored_across_those_currencies(
    pillar: GrowthPillar,
) -> None:
    """The currencies that have it are ranked against each other, not against zero.

    Dropping the component for everyone throws away a series because one country
    is missing it; filling the gap with a zero ranks a missing print as average.
    """
    observations = universe({"JPY": (1.6, 2.0, None, 2.2)})

    complete = pillar.compute(universe(), FOUR, ASOF)
    partial = pillar.compute(observations, FOUR, ASOF)
    values = component_values(pillar, observations)

    assert values["JPY"]["indpro_yoy"] is None
    # JPY renormalises over the 0.80 it still holds, which clears the floor.
    assert partial["JPY"].z is not None
    # The three that still have industrial production are z-scored against each
    # other rather than against a filled-in zero, so dropping JPY's 0.7 from
    # that cross-section has to move them.
    for currency in ("USD", "EUR", "GBP"):
        assert partial[currency].z != complete[currency].z


def test_a_currency_holding_exactly_half_the_sub_weight_is_absent(
    pillar: GrowthPillar,
) -> None:
    """One 0.30 component plus one 0.20 component is exactly the floor.

    Section 3.3 names this case: the surviving components are being asked to
    speak for the ones that are absent, so the pillar is absent rather than
    scored on what is left.
    """
    held = {"gdp_yoy": 0.30, "indpro_yoy": 0.20}
    assert sum(held.values()) == MIN_COMPONENT_WEIGHT

    scores = pillar.compute(universe({"JPY": (1.6, None, 0.7, None)}), FOUR, ASOF)

    assert scores["JPY"].z is None
    assert scores["JPY"].score == 0.0
    assert "business_confidence_mfg" in scores["JPY"].notes


def test_a_currency_holding_more_than_half_the_sub_weight_still_scores(
    pillar: GrowthPillar,
) -> None:
    """The converse, which stops the test above passing against a pillar that refuses
    every incomplete currency."""
    scores = pillar.compute(universe({"JPY": (1.6, 2.0, None, None)}), FOUR, ASOF)

    assert scores["JPY"].z is not None


# --- what reaches the report -------------------------------------------------


def test_gdp_reaches_raw_in_percent(pillar: GrowthPillar) -> None:
    """`headline_component` is the level, so a reader sees the published rate."""
    scores = pillar.compute(universe(), FOUR, ASOF)

    assert pillar.headline_component == "gdp_yoy"
    for currency, (gdp, _, _, _) in UNIVERSE.items():
        assert scores[currency].raw == gdp


def test_raw_is_the_published_level_and_not_the_pillars_own_score(
    pillar: GrowthPillar,
) -> None:
    """A z-score leaking into `raw` reads as a growth rate and is not one."""
    scores = pillar.compute(universe(), FOUR, ASOF)

    assert scores["USD"].raw == 2.4
    assert scores["USD"].z != 2.4


def test_every_currency_in_the_universe_gets_a_score(pillar: GrowthPillar) -> None:
    """The scorer detects thin coverage by looking for `z is None`, not absent keys."""
    scores = pillar.compute(universe(), G10, ASOF)

    assert set(scores) == set(G10)
    assert scores["CHF"].z is None


# --- the four clocks ---------------------------------------------------------


def test_a_quarterly_survey_currency_scores_beside_a_monthly_one(
    pillar: GrowthPillar,
) -> None:
    """Both halves of the survey stay in the cross-section.

    USD, EUR, GBP and CHF survey monthly; JPY, CAD, AUD and NZD survey
    quarterly. Judged against the global 45-day ceiling the quarterly half
    expires on the day it publishes, and the slot would hold four currencies.
    """
    observations = [
        *activity("USD", *UNIVERSE["USD"]),
        *activity("EUR", *UNIVERSE["EUR"]),
        *activity("GBP", *UNIVERSE["GBP"]),
        *activity("JPY", *UNIVERSE["JPY"], survey_period=QUARTERLY_SURVEY_PERIOD),
    ]

    scores = pillar.compute(observations, FOUR, ASOF)
    values = component_values(pillar, observations)

    assert values["JPY"]["business_confidence_mfg"] == 2.0
    assert scores["JPY"].z is not None
    assert scores["USD"].z is not None


def test_the_survey_ages_against_its_own_allowance_and_not_the_global_ceiling(
    pillar: GrowthPillar,
) -> None:
    """The quarterly leg carries real weight. Under the global 45 it carries none."""
    observations = [
        *activity("USD", *UNIVERSE["USD"]),
        *activity("EUR", *UNIVERSE["EUR"]),
        *activity("GBP", *UNIVERSE["GBP"]),
        *activity("JPY", *UNIVERSE["JPY"], survey_period=QUARTERLY_SURVEY_PERIOD),
    ]
    age = (ASOF - QUARTERLY_SURVEY_PERIOD).days
    ref = series_for("business_confidence_mfg", "JPY")
    assert ref is not None
    full = full_weight_age(ref, ref.frequency)
    allowance = staleness_allowance(ref, ref.frequency)

    scores = pillar.compute(observations, FOUR, ASOF)
    factor = scores["JPY"].diagnostics["freshness.business_confidence_mfg"]

    # Japan's survey is the Tankan, quarterly, and the OECD publishes it 161
    # days after the quarter starts, so it is punctual to 253 days and worth
    # nothing at 345.
    assert (full, allowance) == (253, 345)
    assert factor == pytest.approx(freshness(age, full, allowance))
    assert factor > 0.0
    # Judged as a monthly leg, which is what the parent spec calls this
    # indicator, the same punctual print would be worth nothing.
    assert freshness(age, 76, 107) == 0.0


def test_each_component_ages_on_its_own_clock(pillar: GrowthPillar) -> None:
    """A stale GDP print beside a fresh retail print discounts one and not the other.

    The pillar-level age reports the freshest input, which is the retail print,
    so a single scalar would carry the stale GDP leg at full weight. That is the
    case `component_indicators` exists for.
    """
    late = date(2026, 2, 1)
    observations = [
        *activity("EUR", *UNIVERSE["EUR"]),
        *activity("GBP", *UNIVERSE["GBP"]),
        *activity("JPY", *UNIVERSE["JPY"]),
        *activity("USD", None, -1.5, 1.8, 3.1),
        obs("gdp_yoy", "USD", 2.4, late, released_at=published(late, 40)),
    ]
    age = (ASOF - late).days

    scores = pillar.compute(observations, FOUR, ASOF)
    diagnostics = scores["USD"].diagnostics

    # On the ramp rather than at either end of it, so a cliff cannot satisfy
    # this and neither can a factor pinned at 1.0.
    assert age == 226
    gdp_ref = series_for("gdp_yoy", "USD")
    assert gdp_ref is not None
    assert diagnostics["freshness.gdp_yoy"] == pytest.approx(
        freshness(
            age,
            full_weight_age(gdp_ref, gdp_ref.frequency),
            staleness_allowance(gdp_ref, gdp_ref.frequency),
        )
    )
    assert 0.0 < diagnostics["freshness.gdp_yoy"] < 1.0
    assert diagnostics["freshness.retail_sales_yoy"] == 1.0
    # The pillar-level age is the freshest input, which is why a single scalar
    # cannot do this job and `component_indicators` exists.
    assert scores["USD"].staleness_days == (ASOF - MONTHLY_PERIOD).days


def test_a_component_past_its_allowance_still_moves_the_score(
    pillar: GrowthPillar,
) -> None:
    """Pins today's behaviour, which is wrong, so it is not read as an accident.

    `component_freshness` computes the discount and records it, and
    `BasePillar._normalise` has nowhere to receive it from, so it is never
    applied: a print at a factor of ``0.0`` still enters the blend at its full
    sub-weight. #162 carries the fix and it is the aggregator's wiring rather
    than this pillar's.

    It is also why `_transform` returns the reading rather than ``None`` for a
    late print. Cutting it off here would take it out of the sub-weight the
    currency is judged to hold, which is `MIN_COMPONENT_WEIGHT`'s input, and
    that floor asks about substitution rather than about age.
    """
    expired = date(2025, 10, 1)

    def run(gdp: float) -> float:
        observations = [
            *activity("EUR", *UNIVERSE["EUR"]),
            *activity("GBP", *UNIVERSE["GBP"]),
            *activity("JPY", *UNIVERSE["JPY"]),
            *activity("USD", None, -1.5, 1.8, 3.1),
            obs("gdp_yoy", "USD", gdp, expired, released_at=published(expired, 40)),
        ]
        scores = pillar.compute(observations, FOUR, ASOF)
        assert scores["USD"].diagnostics["freshness.gdp_yoy"] == 0.0
        return scores["USD"].score

    gdp_ref = series_for("gdp_yoy", "USD")
    assert gdp_ref is not None
    assert (ASOF - expired).days > staleness_allowance(gdp_ref, gdp_ref.frequency)
    assert run(2.4) != run(-6.0)


def test_the_legs_own_ramp_is_read_from_the_registry(
    pillar: GrowthPillar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Testing the wire, not the value.

    The pillar reads each leg's own ramp out of the registry, so changing what
    the registry says about that leg has to move the component's factor. A
    pillar that had retyped the bounds, or that fell back to one figure for
    everything, does not move.

    The lever changed with #126: there is no allowance field to shorten any
    more, so this shortens both tables the bounds are built from. A quarterly
    lag of 30 days and a cycle of 30 puts the leg at ``30 + 30 = 60`` and
    ``30 + 60 = 90``, so the 76-day print that is punctual on the shipped
    tables lands halfway down the ramp at ``(90 - 76) / 30``.
    """
    monkeypatch.setattr(
        "fbe.datasources.registry.DEFAULT_PUBLICATION_LAG_DAYS",
        {**DEFAULT_PUBLICATION_LAG_DAYS, Frequency.QUARTERLY: 30},
    )
    monkeypatch.setattr(
        "fbe.datasources.registry.CYCLE_DAYS",
        {**CYCLE_DAYS, Frequency.QUARTERLY: 30},
    )
    scores = pillar.compute(universe(), FOUR, ASOF)
    factor = scores["USD"].diagnostics["freshness.gdp_yoy"]
    age = (ASOF - QUARTERLY_PERIOD).days

    assert age == 76
    assert factor == pytest.approx(14 / 30)
    assert factor < 1.0


def test_a_component_with_no_data_has_no_freshness_key(pillar: GrowthPillar) -> None:
    """An absent component and one present and expired are different answers.

    `component_freshness` keeps them apart, and flattening them here would make
    an outage read as a late release.
    """
    scores = pillar.compute(universe({"USD": (2.4, -1.5, None, 3.1)}), FOUR, ASOF)

    assert "freshness.indpro_yoy" not in scores["USD"].diagnostics
    assert "freshness.gdp_yoy" in scores["USD"].diagnostics


def test_every_component_has_an_entry_in_the_freshness_table(
    pillar: GrowthPillar,
) -> None:
    """A component missing from `component_indicators` is silently exempt from the
    discount, which is the failure that table exists to prevent."""
    assert set(pillar.component_indicators) == set(pillar.component_weights)


# --- the config is read, not retyped -----------------------------------------


def test_the_score_band_is_read_from_the_config(pillar: GrowthPillar) -> None:
    """A retyped clip would not move when the config does."""
    tight = GrowthPillar(ScoringConfig(score_clip=0.25))

    scores = tight.compute(universe(), FOUR, ASOF)

    assert max(abs(score.score) for score in scores.values()) <= 0.25
    assert any(abs(score.score) == 0.25 for score in scores.values())


def test_the_fixture_reproduces_a_hand_computed_blend(pillar: GrowthPillar) -> None:
    """Four numbers computed outside the pillar, to six places.

    Recomputed from the readings at the top of this file without any of the
    pillar's own helpers: a population z-score per component across the four
    currencies, weighted by section 3.3's sub-weights, then centred on the
    run's own blend mean and divided by its blend standard deviation, which is
    `blend_divisor`'s fallback while this pillar has no stored history. The
    fallback is asserted too, because a score computed under the rolling
    estimate is not on the same scale.

    This is the one assertion here that pins the whole pipeline rather than one
    step of it, and it is the assertion a reader can check with a calculator.
    """
    expected = {
        "USD": 1.249409,
        "EUR": -0.424124,
        "GBP": -1.392068,
        "JPY": 0.566784,
    }

    scores = pillar.compute(universe(), FOUR, ASOF)

    assert scores["USD"].blend_divisor_path == "run_local"
    for currency, z in expected.items():
        assert scores[currency].z == pytest.approx(z, abs=5e-7)
