"""Tests for MONETARY's two pillar-owned methods.

`_extract` answers "what was this currency's rate picture on a given day", and
the word that carries the weight is *day*. Its whole difficulty is the
visibility rule: an observation counts only once it had been published, not
once the period it describes has begun. Filtering on period instead is
invisible until Phase 6 runs a backtest, and it always flatters, so most of the
`_extract` cases here are about a number the run must not be allowed to see.

`_transform` turns those observations into five components. Its difficulty is
absence. Every component a currency cannot build is `None`, never `0.0`: for
`real_policy_rate` a zero reads as a central bank exactly at target, which is a
real and specific claim about a currency the model has no data for.

Nothing here reaches the network or the registry. Every observation is built in
the test that uses it, so the arithmetic is checkable by hand against numbers
written in the file.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest

from fbe.datasources.registry import INDICATORS
from fbe.pillars.base import DEFAULT_PUBLICATION_LAG_DAYS
from fbe.pillars.monetary import MonetaryPillar
from fbe.types import Frequency, Observation

ASOF = date(2026, 9, 15)

# The five keys the pillar declares, spelled out rather than read from the
# pillar, so a key silently dropped from `requires` fails here rather than
# quietly shrinking what the tests cover.
EXPECTED_REQUIRES = (
    "policy_rate",
    "yield_2y",
    "yield_2y_chg_1m",
    "yield_2y_chg_3m",
    "cpi_yoy",
)

# Read from the registry rather than written out. An earlier version hardcoded
# both tables and then asserted them back off observations the tests themselves
# had built with them, which proved only that the file agreed with itself.
UNITS = {key: INDICATORS[key].unit for key in EXPECTED_REQUIRES}

FREQUENCIES = {key: INDICATORS[key].frequency for key in EXPECTED_REQUIRES}


@pytest.fixture
def pillar() -> MonetaryPillar:
    return MonetaryPillar()


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
    """Build one observation.

    Defaults to the unit and frequency the registry carries for the indicator,
    so a test that says nothing about either gets the real ones rather than a
    plausible-looking stand-in.
    """
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


def published(period: date, days_after: int = 1) -> datetime:
    """A release timestamp a given number of days after the period starts."""
    stamped = date.fromordinal(period.toordinal() + days_after)
    return datetime(stamped.year, stamped.month, stamped.day, 12, 0, tzinfo=UTC)


def full_set(currency: str, *, period: date = date(2026, 9, 10)) -> list[Observation]:
    """One visible observation of each of the five indicators."""
    return [
        obs("policy_rate", currency, 4.25, period, released_at=published(period)),
        obs("yield_2y", currency, 3.80, period, released_at=published(period)),
        obs("yield_2y_chg_1m", currency, 12.0, period, released_at=published(period)),
        obs("yield_2y_chg_3m", currency, -8.0, period, released_at=published(period)),
        obs(
            "cpi_yoy",
            currency,
            2.40,
            date(2026, 8, 1),
            released_at=published(date(2026, 8, 1), 20),
        ),
    ]


# --- what _extract selects ---------------------------------------------------


def test_the_pillar_asks_for_the_five_series_it_blends(pillar: MonetaryPillar) -> None:
    """Guards every test below, which would narrow silently with `requires`."""
    assert tuple(pillar.requires) == EXPECTED_REQUIRES


def test_every_required_key_is_present_for_every_currency(
    pillar: MonetaryPillar,
) -> None:
    extracted = pillar._extract(full_set("USD"), ["USD", "EUR"], ASOF)

    assert set(extracted) == {"USD", "EUR"}
    for currency in ("USD", "EUR"):
        assert set(extracted[currency]) == set(EXPECTED_REQUIRES)


def test_an_indicator_with_nothing_is_an_empty_sequence_not_a_missing_key(
    pillar: MonetaryPillar,
) -> None:
    """A dropped key and an empty sequence read the same at a glance and are
    different facts. `component_freshness` already distinguishes them, skipping
    an indicator whose sequence is falsy, so the shape has a consumer."""
    only_policy = [obs("policy_rate", "USD", 4.25, date(2026, 9, 10))]

    extracted = pillar._extract(only_policy, ["USD"], ASOF)

    assert list(extracted["USD"]["yield_2y"]) == []
    assert "yield_2y" in extracted["USD"]


def test_a_currency_with_nothing_at_all_still_appears(
    pillar: MonetaryPillar,
) -> None:
    """Omitting it would make an unscored currency indistinguishable from one
    that was never asked for."""
    extracted = pillar._extract(full_set("USD"), ["USD", "JPY"], ASOF)

    assert set(extracted["JPY"]) == set(EXPECTED_REQUIRES)
    assert all(list(series) == [] for series in extracted["JPY"].values())


def test_a_currency_not_asked_for_is_not_returned(pillar: MonetaryPillar) -> None:
    extracted = pillar._extract(full_set("USD") + full_set("EUR"), ["USD"], ASOF)

    assert set(extracted) == {"USD"}


def test_an_indicator_this_pillar_does_not_use_is_ignored(
    pillar: MonetaryPillar,
) -> None:
    """GDP belongs to GROWTH. A pillar that hoovered up everything would blend
    another pillar's series the day a key is added to the registry."""
    intruder = Observation(
        indicator="gdp_yoy",
        currency="USD",
        value=2.1,
        period=date(2026, 6, 1),
        source="test",
        series_id="gdp_yoy",
        unit="percent",
        frequency=Frequency.QUARTERLY,
        released_at=published(date(2026, 6, 1), 40),
    )

    extracted = pillar._extract([*full_set("USD"), intruder], ["USD"], ASOF)

    assert "gdp_yoy" not in extracted["USD"]


def test_each_series_comes_back_oldest_first(pillar: MonetaryPillar) -> None:
    """Served out of order, because `_transform` reads the last element and a
    caller sorting its own input would prove nothing about this method."""
    periods = [date(2026, 9, 1), date(2026, 9, 11), date(2026, 9, 4)]
    served = [
        obs("yield_2y", "USD", 3.0 + index, period, released_at=published(period))
        for index, period in enumerate(periods)
    ]

    extracted = pillar._extract(served, ["USD"], ASOF)

    assert [o.period for o in extracted["USD"]["yield_2y"]] == sorted(periods)


# --- the visibility rule -----------------------------------------------------


def test_a_figure_not_yet_published_is_excluded(pillar: MonetaryPillar) -> None:
    """The rule this method exists for. The period is well before the run date
    and the number did not exist yet, so a run dated here could not have read
    it. Filtering on period keeps it and the backtest reads the answer sheet.
    """
    unpublished = obs(
        "cpi_yoy",
        "USD",
        2.4,
        date(2026, 9, 1),
        released_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )

    extracted = pillar._extract([unpublished], ["USD"], ASOF)

    assert unpublished.period < ASOF
    assert list(extracted["USD"]["cpi_yoy"]) == []


def test_a_figure_published_on_the_run_date_is_included(
    pillar: MonetaryPillar,
) -> None:
    """The boundary is inclusive, so a print released this morning counts."""
    same_day = obs(
        "policy_rate",
        "USD",
        4.25,
        date(2026, 9, 1),
        released_at=datetime(ASOF.year, ASOF.month, ASOF.day, 9, 0, tzinfo=UTC),
    )

    extracted = pillar._extract([same_day], ["USD"], ASOF)

    assert list(extracted["USD"]["policy_rate"]) == [same_day]


def test_without_a_release_stamp_the_assumed_lag_decides(
    pillar: MonetaryPillar,
) -> None:
    """The fallback, and the reason a run should record how often it was used.
    A monthly period is assumed publishable 45 days after it starts."""
    lag = DEFAULT_PUBLICATION_LAG_DAYS[Frequency.MONTHLY]
    assert lag == 45
    too_recent = obs("cpi_yoy", "USD", 2.4, date(ASOF.year, ASOF.month, 1))
    ready = obs("cpi_yoy", "USD", 2.1, date.fromordinal(ASOF.toordinal() - lag))

    extracted = pillar._extract([too_recent, ready], ["USD"], ASOF)

    assert list(extracted["USD"]["cpi_yoy"]) == [ready]


def test_the_assumed_lag_boundary_is_inclusive(pillar: MonetaryPillar) -> None:
    """At the allowance and one day short of it, so an off-by-one shows."""
    lag = DEFAULT_PUBLICATION_LAG_DAYS[Frequency.MONTHLY]
    on_the_day = obs("cpi_yoy", "USD", 2.1, date.fromordinal(ASOF.toordinal() - lag))
    one_short = obs("cpi_yoy", "EUR", 2.2, date.fromordinal(ASOF.toordinal() - lag + 1))

    extracted = pillar._extract([on_the_day, one_short], ["USD", "EUR"], ASOF)

    assert list(extracted["USD"]["cpi_yoy"]) == [on_the_day]
    assert list(extracted["EUR"]["cpi_yoy"]) == []


def test_the_assumed_lag_is_the_one_for_that_series_frequency(
    pillar: MonetaryPillar,
) -> None:
    """A daily yield is publishable the next day and a monthly CPI is not. One
    lag for everything would hide six weeks of a CPI print's unavailability."""
    assert DEFAULT_PUBLICATION_LAG_DAYS[Frequency.DAILY] == 1
    period = date.fromordinal(ASOF.toordinal() - 2)
    daily = obs("yield_2y", "USD", 3.8, period)
    monthly = obs("cpi_yoy", "USD", 2.4, period)

    extracted = pillar._extract([daily, monthly], ["USD"], ASOF)

    assert list(extracted["USD"]["yield_2y"]) == [daily]
    assert list(extracted["USD"]["cpi_yoy"]) == []


# --- the vintage rule --------------------------------------------------------


def test_one_observation_per_period_survives(pillar: MonetaryPillar) -> None:
    period = date(2026, 8, 1)
    first = obs(
        "cpi_yoy", "USD", 2.4, period, released_at=published(period, 20), revision=0
    )
    revised = obs(
        "cpi_yoy", "USD", 2.6, period, released_at=published(period, 40), revision=1
    )

    extracted = pillar._extract([first, revised], ["USD"], ASOF)

    assert list(extracted["USD"]["cpi_yoy"]) == [revised]


def test_a_revision_not_yet_published_does_not_win(pillar: MonetaryPillar) -> None:
    """The same mistake as the visibility rule wearing different clothes. A run
    in September must see August's print as first estimated, not as it will be
    revised in November."""
    period = date(2026, 8, 1)
    first = obs(
        "cpi_yoy", "USD", 2.4, period, released_at=published(period, 20), revision=0
    )
    future = obs(
        "cpi_yoy",
        "USD",
        2.6,
        period,
        released_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        revision=1,
    )

    extracted = pillar._extract([first, future], ["USD"], ASOF)

    assert list(extracted["USD"]["cpi_yoy"]) == [first]


def test_the_highest_visible_revision_wins_not_the_newest_stamp(
    pillar: MonetaryPillar,
) -> None:
    """Revision first, release time only as a tie-break. A correction issued
    later with a lower revision number is not the current vintage."""
    period = date(2026, 8, 1)
    higher_revision = obs(
        "cpi_yoy", "USD", 2.6, period, released_at=published(period, 20), revision=2
    )
    later_stamp = obs(
        "cpi_yoy", "USD", 2.4, period, released_at=published(period, 40), revision=1
    )

    extracted = pillar._extract([higher_revision, later_stamp], ["USD"], ASOF)

    assert list(extracted["USD"]["cpi_yoy"]) == [higher_revision]


@pytest.mark.parametrize("winner_first", [True, False])
def test_the_later_release_breaks_a_revision_tie(
    pillar: MonetaryPillar, winner_first: bool
) -> None:
    """Served both ways round, because with the tie-break gone the winner is
    whichever arrived first and one of the two orders would pass anyway.
    """
    period = date(2026, 8, 1)
    earlier = obs(
        "cpi_yoy", "USD", 2.4, period, released_at=published(period, 20), revision=1
    )
    later = obs(
        "cpi_yoy", "USD", 2.6, period, released_at=published(period, 30), revision=1
    )
    served = [later, earlier] if winner_first else [earlier, later]

    extracted = pillar._extract(served, ["USD"], ASOF)

    assert list(extracted["USD"]["cpi_yoy"]) == [later]


def test_the_vintage_rule_does_not_collapse_different_periods(
    pillar: MonetaryPillar,
) -> None:
    """Deduplication is per period. Collapsing the series to one row would lose
    the history a change component is computed from upstream."""
    july = obs(
        "cpi_yoy",
        "USD",
        2.2,
        date(2026, 7, 1),
        released_at=published(date(2026, 7, 1), 20),
    )
    august = obs(
        "cpi_yoy",
        "USD",
        2.4,
        date(2026, 8, 1),
        released_at=published(date(2026, 8, 1), 20),
    )

    extracted = pillar._extract([july, august], ["USD"], ASOF)

    assert list(extracted["USD"]["cpi_yoy"]) == [july, august]


# --- what _transform builds --------------------------------------------------


def test_the_levels_are_the_newest_published_values(pillar: MonetaryPillar) -> None:
    """Newest, not first and not an average. The pillar reads a rate path as it
    stands today."""
    older = date(2026, 9, 1)
    newer = date(2026, 9, 11)
    served = [
        obs("policy_rate", "USD", 4.00, older, released_at=published(older)),
        obs("policy_rate", "USD", 4.25, newer, released_at=published(newer)),
        obs("yield_2y", "USD", 3.60, older, released_at=published(older)),
        obs("yield_2y", "USD", 3.80, newer, released_at=published(newer)),
    ]

    components = pillar._transform(pillar._extract(served, ["USD"], ASOF), ASOF)

    assert components["USD"]["policy_rate"] == 4.25
    assert components["USD"]["yield_2y"] == 3.80


def test_the_changes_are_the_newest_published_changes(
    pillar: MonetaryPillar,
) -> None:
    extracted = pillar._extract(full_set("USD"), ["USD"], ASOF)

    components = pillar._transform(extracted, ASOF)

    assert components["USD"]["yield_2y_chg_1m"] == 12.0
    assert components["USD"]["yield_2y_chg_3m"] == -8.0


def test_a_currency_holding_only_the_level_gets_no_change_components(
    pillar: MonetaryPillar,
) -> None:
    """Nothing here differences a daily yield locally. "One month back" would
    otherwise mean a different span per currency's holiday calendar, which is
    not comparable across a cross-section that is then z-scored."""
    level_only = [
        obs(
            "yield_2y",
            "USD",
            3.60,
            date(2026, 6, 15),
            released_at=published(date(2026, 6, 15)),
        ),
        obs(
            "yield_2y",
            "USD",
            3.80,
            date(2026, 9, 11),
            released_at=published(date(2026, 9, 11)),
        ),
    ]

    components = pillar._transform(pillar._extract(level_only, ["USD"], ASOF), ASOF)

    assert components["USD"]["yield_2y"] == 3.80
    assert components["USD"]["yield_2y_chg_1m"] is None
    assert components["USD"]["yield_2y_chg_3m"] is None


# --- the real policy rate ----------------------------------------------------


def test_the_real_policy_rate_is_the_nominal_less_headline_inflation(
    pillar: MonetaryPillar,
) -> None:
    """Hand-computed: 4.25 less 2.40 is 1.85 percentage points."""
    extracted = pillar._extract(full_set("USD"), ["USD"], ASOF)

    components = pillar._transform(extracted, ASOF)

    assert components["USD"]["real_policy_rate"] == pytest.approx(1.85)


def test_the_real_policy_rate_can_be_negative(pillar: MonetaryPillar) -> None:
    """A central bank behind the curve, which is the case the component exists
    to catch. A clamp at zero would read as one exactly at neutral."""
    period = date(2026, 9, 10)
    served = [
        obs("policy_rate", "USD", 2.00, period, released_at=published(period)),
        obs(
            "cpi_yoy",
            "USD",
            5.50,
            date(2026, 8, 1),
            released_at=published(date(2026, 8, 1), 20),
        ),
    ]

    components = pillar._transform(pillar._extract(served, ["USD"], ASOF), ASOF)

    assert components["USD"]["real_policy_rate"] == pytest.approx(-3.50)


def test_the_real_policy_rate_uses_the_newest_visible_cpi(
    pillar: MonetaryPillar,
) -> None:
    """Not the newest that exists. A CPI print released after the run date is
    excluded upstream, so the component is built from July's number."""
    period = date(2026, 9, 10)
    served = [
        obs("policy_rate", "USD", 4.00, period, released_at=published(period)),
        obs(
            "cpi_yoy",
            "USD",
            2.20,
            date(2026, 7, 1),
            released_at=published(date(2026, 7, 1), 20),
        ),
        obs(
            "cpi_yoy",
            "USD",
            3.00,
            date(2026, 8, 1),
            released_at=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
        ),
    ]

    components = pillar._transform(pillar._extract(served, ["USD"], ASOF), ASOF)

    assert components["USD"]["real_policy_rate"] == pytest.approx(1.80)


def test_a_currency_with_no_cpi_loses_only_the_real_rate(
    pillar: MonetaryPillar,
) -> None:
    """The criterion this issue is named for. Four components survive, and the
    blend renormalises over 0.85 of the sub-weight."""
    without_cpi = [o for o in full_set("USD") if o.indicator != "cpi_yoy"]

    components = pillar._transform(pillar._extract(without_cpi, ["USD"], ASOF), ASOF)

    assert components["USD"]["real_policy_rate"] is None
    assert components["USD"]["policy_rate"] == 4.25
    assert components["USD"]["yield_2y"] == 3.80
    assert components["USD"]["yield_2y_chg_1m"] == 12.0
    assert components["USD"]["yield_2y_chg_3m"] == -8.0


def test_a_missing_real_rate_is_none_and_never_zero(pillar: MonetaryPillar) -> None:
    """Stated separately because `0.0` here is not a missing value, it is the
    claim that a central bank is sitting exactly at target.

    Contrasted against a currency whose policy rate and CPI happen to match, so
    the test holds a real `0.0` and a real absence side by side. An
    implementation that substituted zero for the gap would make these two equal
    and would pass every other test in this file.
    """
    period = date(2026, 9, 10)
    at_target = [
        obs("policy_rate", "EUR", 2.40, period, released_at=published(period)),
        obs(
            "cpi_yoy",
            "EUR",
            2.40,
            date(2026, 8, 1),
            released_at=published(date(2026, 8, 1), 20),
        ),
    ]
    without_cpi = [o for o in full_set("USD") if o.indicator != "cpi_yoy"]

    components = pillar._transform(
        pillar._extract(without_cpi + at_target, ["USD", "EUR"], ASOF), ASOF
    )

    assert components["EUR"]["real_policy_rate"] == pytest.approx(0.0)
    assert components["USD"]["real_policy_rate"] is None


def test_no_policy_rate_also_costs_the_real_rate(pillar: MonetaryPillar) -> None:
    """The other half of the component. Either input missing loses it."""
    without_policy = [o for o in full_set("USD") if o.indicator != "policy_rate"]

    components = pillar._transform(pillar._extract(without_policy, ["USD"], ASOF), ASOF)

    assert components["USD"]["policy_rate"] is None
    assert components["USD"]["real_policy_rate"] is None
    assert components["USD"]["yield_2y"] == 3.80


# --- absence, across a cross-section -----------------------------------------


def test_three_currencies_with_different_gaps_keep_what_they_have(
    pillar: MonetaryPillar,
) -> None:
    """One complete, one without a front end, one with nothing at all. Every
    component a currency cannot build is `None`, and no currency's gap changes
    another's numbers."""
    served = [
        *full_set("USD"),
        *[o for o in full_set("EUR") if not o.indicator.startswith("yield_2y")],
    ]

    components = pillar._transform(
        pillar._extract(served, ["USD", "EUR", "JPY"], ASOF), ASOF
    )

    assert all(value is not None for value in components["USD"].values())
    assert components["EUR"]["policy_rate"] == 4.25
    assert components["EUR"]["real_policy_rate"] == pytest.approx(1.85)
    assert components["EUR"]["yield_2y"] is None
    assert components["EUR"]["yield_2y_chg_1m"] is None
    assert components["EUR"]["yield_2y_chg_3m"] is None
    assert all(value is None for value in components["JPY"].values())


def test_a_currency_with_no_yield_at_all_still_returns_its_other_components(
    pillar: MonetaryPillar,
) -> None:
    """So `blend_components` has something to renormalise over rather than the
    currency vanishing before it gets there."""
    no_front_end = [
        o for o in full_set("EUR") if not o.indicator.startswith("yield_2y")
    ]

    components = pillar._transform(pillar._extract(no_front_end, ["EUR"], ASOF), ASOF)

    assert set(components["EUR"]) == set(pillar.component_weights)
    assert components["EUR"]["policy_rate"] == 4.25


def test_every_currency_asked_for_gets_every_component_key(
    pillar: MonetaryPillar,
) -> None:
    """Even one with nothing. A missing key and a `None` are different answers
    and only one of them survives a blend."""
    components = pillar._transform(pillar._extract([], ["USD", "EUR"], ASOF), ASOF)

    for currency in ("USD", "EUR"):
        assert set(components[currency]) == set(pillar.component_weights)
        assert all(value is None for value in components[currency].values())


# --- sign convention ---------------------------------------------------------


# The four components built from a single series. `real_policy_rate` takes two
# and has its own sign test below, because the input that moves it is the one
# that is subtracted.
DIRECT_COMPONENTS = (
    "policy_rate",
    "yield_2y",
    "yield_2y_chg_1m",
    "yield_2y_chg_3m",
)


@pytest.mark.parametrize("component", DIRECT_COMPONENTS)
def test_no_component_is_inverted(pillar: MonetaryPillar, component: str) -> None:
    """Positive means a higher or rising rate path, which is currency-positive.
    A sign flip anywhere here would make the pillar confidently backwards,
    which is the first row of the prime directive table.
    """
    period = date(2026, 9, 10)
    low = obs(component, "USD", 1.0, period, released_at=published(period))
    high = obs(component, "USD", 2.0, period, released_at=published(period))

    lower = pillar._transform(pillar._extract([low], ["USD"], ASOF), ASOF)
    higher = pillar._transform(pillar._extract([high], ["USD"], ASOF), ASOF)

    assert higher["USD"][component] > lower["USD"][component]


def test_a_higher_inflation_print_lowers_the_real_rate(
    pillar: MonetaryPillar,
) -> None:
    """The one place a rising input is currency-negative, and it is negative
    because it is subtracted rather than because a sign was flipped."""
    period = date(2026, 9, 10)
    rate = obs("policy_rate", "USD", 4.00, period, released_at=published(period))

    def real_rate(cpi: float) -> float | None:
        served: Sequence[Observation] = [
            rate,
            obs(
                "cpi_yoy",
                "USD",
                cpi,
                date(2026, 8, 1),
                released_at=published(date(2026, 8, 1), 20),
            ),
        ]
        return pillar._transform(pillar._extract(served, ["USD"], ASOF), ASOF)["USD"][
            "real_policy_rate"
        ]

    assert real_rate(2.0) == pytest.approx(2.0)
    assert real_rate(3.0) == pytest.approx(1.0)


# --- gaps the review passes found -------------------------------------------


def test_the_sort_key_is_period_and_not_release_time(pillar: MonetaryPillar) -> None:
    """A late-arriving revision of an older period must not become the newest.

    Every other fixture in this file stamps releases monotonically in period, so
    the two sort keys are indistinguishable there and sorting on `released_at`
    passed the whole file. Here July is revised and re-released in September,
    after August printed, which is the ordinary shape of a statistical revision.
    """
    july = obs(
        "cpi_yoy",
        "USD",
        2.2,
        date(2026, 7, 1),
        released_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        revision=1,
    )
    august = obs(
        "cpi_yoy",
        "USD",
        2.4,
        date(2026, 8, 1),
        released_at=published(date(2026, 8, 1), 20),
    )
    assert august.released_at is not None and july.released_at is not None
    assert august.released_at < july.released_at

    extracted = pillar._extract([july, august], ["USD"], ASOF)

    assert list(extracted["USD"]["cpi_yoy"]) == [july, august]


def test_the_newest_period_and_not_the_latest_release_sets_the_component(
    pillar: MonetaryPillar,
) -> None:
    """The consequence of the test above, pinned as a number. Sorting on release
    time gives 4.00 - 2.2 = 1.80 where the right answer is 4.00 - 2.4 = 1.60."""
    period = date(2026, 9, 10)
    served = [
        obs("policy_rate", "USD", 4.00, period, released_at=published(period)),
        obs(
            "cpi_yoy",
            "USD",
            2.2,
            date(2026, 7, 1),
            released_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            revision=1,
        ),
        obs(
            "cpi_yoy",
            "USD",
            2.4,
            date(2026, 8, 1),
            released_at=published(date(2026, 8, 1), 20),
        ),
    ]

    components = pillar._transform(pillar._extract(served, ["USD"], ASOF), ASOF)

    assert components["USD"]["real_policy_rate"] == pytest.approx(1.60)


def test_a_period_after_the_run_date_is_dropped(pillar: MonetaryPillar) -> None:
    """`_extract`'s own Args line promises this, and `_transform` reads the
    newest period, so a forward-dated print would otherwise become the current
    level. Published early for a period still ahead is a real shape: a survey
    can print before the month it covers begins.

    This is not the period-filtering mistake the visibility rule is about.
    Dropping a period that starts after `asof` and dropping one published after
    `asof` are different questions, and both answers are no.
    """
    ahead = obs(
        "policy_rate",
        "USD",
        9.99,
        date(2026, 12, 1),
        released_at=published(date(2026, 9, 1)),
    )
    current = obs(
        "policy_rate",
        "USD",
        4.25,
        date(2026, 9, 10),
        released_at=published(date(2026, 9, 10)),
    )

    extracted = pillar._extract([ahead, current], ["USD"], ASOF)
    components = pillar._transform(extracted, ASOF)

    assert list(extracted["USD"]["policy_rate"]) == [current]
    assert components["USD"]["policy_rate"] == 4.25


def test_a_period_on_the_run_date_is_kept(pillar: MonetaryPillar) -> None:
    """The boundary of the rule above, so it cannot be off by a day and discard
    a rate set this morning."""
    today = obs("policy_rate", "USD", 4.25, ASOF, released_at=published(ASOF, 0))

    extracted = pillar._extract([today], ["USD"], ASOF)

    assert list(extracted["USD"]["policy_rate"]) == [today]


def test_a_release_stamp_without_a_timezone_does_not_raise(
    pillar: MonetaryPillar,
) -> None:
    """`_vintage_key` normalises a zoneless stamp to UTC and says so. Nothing
    exercised it: every other fixture here builds an aware datetime, so the
    branch could be deleted and the file stayed green, while a real naive stamp
    raises `TypeError` comparing against an aware one.
    """
    period = date(2026, 8, 1)
    aware = obs(
        "cpi_yoy", "USD", 2.4, period, released_at=published(period, 20), revision=1
    )
    naive = obs(
        "cpi_yoy",
        "USD",
        2.6,
        period,
        released_at=datetime(2026, 8, 25, 12, 0),
        revision=1,
    )

    extracted = pillar._extract([aware, naive], ["USD"], ASOF)

    assert [o.value for o in extracted["USD"]["cpi_yoy"]] == [2.6]


@pytest.mark.parametrize("first_wins", [True, False])
def test_two_identical_vintages_resolve_on_arrival_order(
    pillar: MonetaryPillar, first_wins: bool
) -> None:
    """Pinned rather than left to chance. The docstring calls this arbitrary,
    and arbitrary is not the same as unpinned: with the comparison widened to
    `>=` the last row would win instead, silently, and a row duplicated in a
    manual file would change the model's number by where it sits.
    """
    period = date(2026, 8, 1)
    stamp = published(period, 20)
    one = obs("cpi_yoy", "USD", 2.4, period, released_at=stamp, revision=1)
    two = obs("cpi_yoy", "USD", 2.6, period, released_at=stamp, revision=1)
    served = [one, two] if first_wins else [two, one]

    extracted = pillar._extract(served, ["USD"], ASOF)

    assert [o.value for o in extracted["USD"]["cpi_yoy"]] == [served[0].value]


def test_moving_one_currency_leaves_the_others_untouched(
    pillar: MonetaryPillar,
) -> None:
    """The house technique, and the invariant that matters here: these two
    methods are per-currency, and the cross-sectional step belongs to
    `_normalise`. A shared mutable inner mapping would break this and nothing
    else in the file would notice."""

    def run(usd_cpi: float) -> dict[str, dict[str, float | None]]:
        served = [
            *[o for o in full_set("USD") if o.indicator != "cpi_yoy"],
            obs(
                "cpi_yoy",
                "USD",
                usd_cpi,
                date(2026, 8, 1),
                released_at=published(date(2026, 8, 1), 20),
            ),
            *full_set("EUR"),
            *full_set("JPY"),
        ]
        return dict(
            pillar._transform(
                pillar._extract(served, ["USD", "EUR", "JPY"], ASOF), ASOF
            )
        )

    quiet = run(2.9)
    shocked = run(9.9)

    assert quiet["USD"]["real_policy_rate"] != shocked["USD"]["real_policy_rate"]
    assert quiet["EUR"] == shocked["EUR"]
    assert quiet["JPY"] == shocked["JPY"]


def test_every_component_indicator_is_one_extract_returns(
    pillar: MonetaryPillar,
) -> None:
    """`component_indicators` names the inputs each component ages against, and
    `component_freshness` looks them up in `_extract`'s output. A name outside
    `requires` would never be carried there, the lookup would skip it, and the
    component would take the freshness of its surviving inputs alone: a quiet
    under-ageing rather than a visible gap."""
    named = {
        indicator
        for indicators in pillar.component_indicators.values()
        for indicator in indicators
    }

    assert named <= set(pillar.requires)


# --- the published worked example -------------------------------------------

# `docs/scoring-spec.md` section 7.1, the raw-input table, transcribed. The spec
# says of section 7: "It is a fixture, not an illustration." The z-score half
# waits on `cross_sectional_z`; the real policy rate needs nothing but these two
# methods, so it is pinned here.
SPEC_7_1 = {
    "USD": (4.50, 2.9, 1.60),
    "EUR": (2.50, 2.1, 0.40),
    "GBP": (4.25, 3.2, 1.05),
    "JPY": (0.50, 2.8, -2.30),
    "CHF": (0.25, 0.6, -0.35),
    "CAD": (3.00, 2.2, 0.80),
    "AUD": (4.10, 3.4, 0.70),
    "NZD": (2.70, 1.9, 0.80),
}


def test_the_worked_example_reproduces_its_real_policy_rate_column(
    pillar: MonetaryPillar,
) -> None:
    """All eight currencies, to the precision the spec publishes.

    A worked example that does not reproduce is worse than no worked example,
    and this is the one column of section 7.1 that these two methods alone
    decide. It also runs a real eight-currency cross-section rather than the one
    or two hand-built currencies the rest of the file uses.
    """
    period = date(2026, 9, 10)
    cpi_period = date(2026, 8, 1)
    served = [
        item
        for currency, (policy, cpi, _) in SPEC_7_1.items()
        for item in (
            obs("policy_rate", currency, policy, period, released_at=published(period)),
            obs(
                "cpi_yoy",
                currency,
                cpi,
                cpi_period,
                released_at=published(cpi_period, 20),
            ),
        )
    ]

    components = pillar._transform(pillar._extract(served, list(SPEC_7_1), ASOF), ASOF)

    for currency, (_, _, expected) in SPEC_7_1.items():
        assert components[currency]["real_policy_rate"] == pytest.approx(expected), (
            currency
        )
