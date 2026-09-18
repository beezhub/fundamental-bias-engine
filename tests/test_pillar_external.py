"""Tests for EXTERNAL's two pillar-owned methods and the registry entry they need.

Three components, three different traps, and none of the three fails loudly on
its own.

**The scale.** `trade_trend` divides a trade balance by a nominal GDP and both
arrive in actual US dollars, which is the whole reason #158 was ruled the way it
was: same unit, no conversion step. Nothing in the arithmetic would notice a GDP
expressed in millions. Every currency's number would come back a million times
larger, every currency's by the same factor, the cross-sectional z-score would
absorb it exactly, and the ranking would be unchanged and the published unit
wrong. So the dependency on the registry's unit contract is pinned here
deliberately rather than assumed.

**The window.** Both `trade_trend` and `terms_of_trade` are three-month
quantities, and `BasePillar.momentum` counts observations rather than months.
Crude oil is daily, so three observations of it is three days. The window here is
measured in calendar months for that reason, and a series with a hole too big to
cover returns an absence rather than a longer return wearing a three-month label.

**The zero.** `terms_of_trade` is `0.0` for the five currencies with no
`commodity_link` and that is a modelling statement, the one deliberate zero in
the engine. A currency that has a link and no visible price is a different fact
and comes back `None`. Several tests here exist only to keep those two apart.

Nothing reaches the network. Every observation is built in this file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

import pytest

from fbe.datasources.registry import INDICATORS, SOURCE_FRED, UNCONSUMED_INDICATORS
from fbe.pillars.base import (
    DEFAULT_PUBLICATION_LAG_DAYS,
    MIN_COMPONENT_WEIGHT,
    BasePillar,
)
from fbe.pillars.external import ExternalPillar
from fbe.types import Frequency, Observation, PillarName
from fbe.universe import G10, meta

ASOF = date(2026, 9, 18)

EXPECTED_REQUIRES = (
    "current_account_gdp",
    "trade_balance",
    "commodity_price",
    "gdp_nominal_usd",
)
"""Spelled out rather than read off the pillar, so a key silently dropped from
`requires` fails here rather than quietly shrinking what these tests cover."""

NEWEST = date(2026, 6, 1)
"""The newest monthly period in the fixture, three months after `BASELINE`."""

BASELINE = date(2026, 3, 1)
"""Exactly three calendar months before `NEWEST`."""

QUARTER = date(2026, 4, 1)
"""The newest current-account period. Quarterly across the whole G10."""

GDP_PERIOD = date(2025, 1, 1)
"""Annual periods are stamped on 1 January of the year they describe."""

# currency: (current account % of GDP, balance at BASELINE, balance at NEWEST,
#            nominal GDP in actual US dollars)
UNIVERSE: dict[str, tuple[float, float, float, float]] = {
    "USD": (-3.3, -100e9, -70e9, 30e12),
    "EUR": (2.6, 20e9, 30e9, 5e12),
    "GBP": (-2.1, -5e9, -17e9, 4e12),
    "JPY": (3.4, 8e9, 30e9, 4.4e12),
    "CHF": (6.2, 12e9, 8e9, 1e12),
    "CAD": (-0.8, -6e9, 7.8e9, 2.3e12),
    "AUD": (1.1, 4e9, -8.6e9, 1.8e12),
    "NZD": (-5.4, -1e9, 1.08e9, 260e9),
}
"""Eight currencies, no two sharing a value on any component.

`cross_sectional_z` refuses a cross-section whose readings are all equal, so a
degenerate series would turn a real assertion into a comparison of two ``None``s.
"""

EXPECTED_TRADE_TREND = {
    "USD": 0.1,
    "EUR": 0.2,
    "GBP": -0.3,
    "JPY": 0.5,
    "CHF": -0.4,
    "CAD": 0.6,
    "AUD": -0.7,
    "NZD": 0.8,
}
"""Hand-computed: ``(newest - baseline) / gdp * 100``, in percent of GDP.

USD is ``(-70e9 - -100e9) / 30e12 * 100``, which is ``30e9 / 30e12 * 100``,
which is 0.1. The other seven are worked the same way and every one of them is
a different number, so a currency reading another's GDP is visible.
"""

COMMODITY = {"CAD": ("crude_oil", 80.0, 88.0), "AUD": ("iron_ore", 100.0, 95.0)}
"""Linked currency: (complex, price at `BASELINE`, price at `NEWEST`).

NZD is deliberately left out of the shared fixture and given its own series in
the tests that need it, because its registry ref is manual and unverified.
"""


@pytest.fixture
def pillar() -> ExternalPillar:
    return ExternalPillar()


def published(period: date, days_after: int = 20) -> datetime:
    """A release timestamp a given number of days after the period starts."""
    stamped = date.fromordinal(period.toordinal() + days_after)
    return datetime(stamped.year, stamped.month, stamped.day, 12, 0, tzinfo=UTC)


def obs(
    indicator: str,
    currency: str,
    value: float,
    period: date,
    *,
    unit: str | None = None,
    frequency: Frequency | None = None,
    released_at: datetime | None = None,
    revision: int = 0,
) -> Observation:
    """Build one observation, defaulting to the registry's own unit and frequency."""
    spec = INDICATORS[indicator]
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=period,
        source="test",
        series_id=indicator,
        unit=unit if unit is not None else spec.unit,
        frequency=frequency if frequency is not None else spec.frequency,
        released_at=released_at if released_at is not None else published(period),
        revision=revision,
    )


def balances(currency: str, baseline: float, newest: float) -> list[Observation]:
    """Four monthly trade balances, so the three-month window has both ends."""
    return [
        obs("trade_balance", currency, value, period)
        for period, value in (
            (BASELINE, baseline),
            (date(2026, 4, 1), (baseline + newest) / 2),
            (date(2026, 5, 1), (baseline + newest) / 2),
            (NEWEST, newest),
        )
    ]


def commodity(currency: str, baseline: float, newest: float) -> list[Observation]:
    """A commodity price at each end of the three-month window."""
    return [
        obs("commodity_price", currency, value, period)
        for period, value in ((BASELINE, baseline), (NEWEST, newest))
    ]


def universe(
    drop: Mapping[str, tuple[str, ...]] | None = None,
    gdp_scale: float = 1.0,
) -> list[Observation]:
    """Build the eight-currency fixture, withholding the named indicators.

    Args:
        drop: ``{currency: indicators}`` to withhold, which is how a currency is
            put on the sub-weight floor.
        gdp_scale: Multiplier on every nominal GDP, used only by the test that
            pins the unit dependency.
    """
    withheld = drop or {}
    built: list[Observation] = []
    for currency, (account, baseline, newest, gdp) in UNIVERSE.items():
        skip = withheld.get(currency, ())
        if "current_account_gdp" not in skip:
            built.append(obs("current_account_gdp", currency, account, QUARTER))
        if "trade_balance" not in skip:
            built.extend(balances(currency, baseline, newest))
        if "gdp_nominal_usd" not in skip:
            built.append(obs("gdp_nominal_usd", currency, gdp * gdp_scale, GDP_PERIOD))
        if currency in COMMODITY and "commodity_price" not in skip:
            _, low, high = COMMODITY[currency]
            built.extend(commodity(currency, low, high))
    return built


def components(
    pillar: ExternalPillar,
    observations: Sequence[Observation],
    currencies: Sequence[str] = G10,
    asof: date = ASOF,
) -> Mapping[str, Mapping[str, float | None]]:
    """Run the two pillar-owned steps and return `_transform`'s output."""
    return pillar._transform(pillar._extract(observations, currencies, asof), asof)


# --- the registry entry the ruling authorised --------------------------------


def test_the_nominal_gdp_indicator_is_registered() -> None:
    """Without it `trade_trend` has no denominator, which is what #158 was about."""
    spec = INDICATORS["gdp_nominal_usd"]

    assert spec.pillar is PillarName.EXTERNAL
    assert spec.unit == "usd"
    assert spec.frequency is Frequency.ANNUAL
    assert set(spec.series) == set(G10)


def test_every_nominal_gdp_ref_is_in_dollars() -> None:
    """The unit is the contract `trade_trend` divides against.

    A national-currency series filed under this key gives a plausible small
    number for that currency and nothing raises, which is the single most likely
    way the ruling said this would go wrong.
    """
    spec = INDICATORS["gdp_nominal_usd"]

    for currency, ref in spec.series.items():
        assert ref.unit == "usd", f"{currency} is not in dollars"
        assert ref.source == SOURCE_FRED
        assert ref.verified
        assert ref.last_observed == GDP_PERIOD


def test_the_nominal_gdp_series_are_the_ones_that_were_measured() -> None:
    """The World Bank family, verified 8 of 8 through FRED on 2026-09-18."""
    measured = {
        "USD": "MKTGDPUSA646NWDB",
        "EUR": "MKTGDPDEA646NWDB",
        "GBP": "MKTGDPGBA646NWDB",
        "JPY": "MKTGDPJPA646NWDB",
        "CHF": "MKTGDPCHA646NWDB",
        "CAD": "MKTGDPCAA646NWDB",
        "AUD": "MKTGDPAUA646NWDB",
        "NZD": "MKTGDPNZA646NWDB",
    }

    spec = INDICATORS["gdp_nominal_usd"]

    assert {c: ref.series_id for c, ref in spec.series.items()} == measured


def test_the_allowance_follows_the_registrys_own_rule() -> None:
    """Derived, not chosen. `IndicatorSpec.max_staleness_days` states the rule.

    The rule is the age of the newest print on the day before the next one is
    due. Both inputs were measured rather than assumed: the period is a calendar
    year, and the publication lag is the gap from the period's end to the day
    FRED last updated the series, 2025-12-31 to 2026-07-07.
    """
    measured_lag_days = (date(2026, 7, 7) - date(2025, 12, 31)).days
    next_period_ends = date(2026, 12, 31)
    next_due = date.fromordinal(next_period_ends.toordinal() + measured_lag_days)
    day_before = date.fromordinal(next_due.toordinal() - 1)

    assert measured_lag_days == 188
    assert (day_before - GDP_PERIOD).days == 916
    assert INDICATORS["gdp_nominal_usd"].max_staleness_days == 916


def test_the_allowance_expires_the_day_after_the_next_print_is_due() -> None:
    """The behaviour the derivation exists for, asserted rather than the arithmetic.

    A current series must never read stale, and a series that has missed a whole
    release must. An allowance chosen to make a run pass would fail one of these
    two.
    """
    ref = INDICATORS["gdp_nominal_usd"].series["USD"]
    allowance = INDICATORS["gdp_nominal_usd"].max_staleness_days

    assert not ref.stale_on(date(2027, 7, 6), allowance)
    assert ref.stale_on(date(2027, 7, 7), allowance)


def test_the_annual_frequency_has_a_publication_lag() -> None:
    """A member with no entry is a `KeyError` waiting for the first unstamped print.

    `BasePillar._visible` indexes this mapping with a bare subscript, and
    `tests/test_staleness_ramp.py` indexes it while walking every registry spec.
    """
    assert Frequency.ANNUAL in DEFAULT_PUBLICATION_LAG_DAYS
    assert DEFAULT_PUBLICATION_LAG_DAYS[Frequency.ANNUAL] == 552


def test_an_unstamped_annual_print_is_invisible_until_its_lag_has_run(
    pillar: ExternalPillar,
) -> None:
    """Erring long costs weight. Erring short admits data before it existed."""
    lag = DEFAULT_PUBLICATION_LAG_DAYS[Frequency.ANNUAL]
    unstamped = Observation(
        indicator="gdp_nominal_usd",
        currency="USD",
        value=30e12,
        period=GDP_PERIOD,
        source="test",
        series_id="gdp_nominal_usd",
        unit="usd",
        frequency=Frequency.ANNUAL,
        released_at=None,
    )
    due = date.fromordinal(GDP_PERIOD.toordinal() + lag)

    assert not pillar._visible(unstamped, date.fromordinal(due.toordinal() - 1))
    assert pillar._visible(unstamped, due)


def test_the_nominal_gdp_key_is_consumed_rather_than_declared_unconsumed(
    pillar: ExternalPillar,
) -> None:
    """The agreement test walks `requires`; this pins both ends of it."""
    assert "gdp_nominal_usd" in pillar.requires
    assert "gdp_nominal_usd" not in UNCONSUMED_INDICATORS


# --- what the pillar declares ------------------------------------------------


def test_the_pillar_asks_for_the_four_series_it_needs(pillar: ExternalPillar) -> None:
    """Guards every test below, which would narrow silently with `requires`."""
    assert tuple(pillar.requires) == EXPECTED_REQUIRES


def test_the_sub_weights_match_the_published_table(pillar: ExternalPillar) -> None:
    """Section 3.5 of `docs/scoring-spec.md`, which the floor tests count against."""
    assert pillar.component_weights == {
        "current_account_gdp": 0.40,
        "trade_trend": 0.30,
        "terms_of_trade": 0.30,
    }


def test_the_trade_trend_component_ages_on_both_of_its_inputs(
    pillar: ExternalPillar,
) -> None:
    """A component is as stale as its stalest input, and this one has two.

    The denominator is the older by years. The ruling recorded that discounting
    a scaling constant for its age is arguable and left it with #126 rather than
    building a mechanism for one case.
    """
    assert pillar.component_indicators["trade_trend"] == (
        "trade_balance",
        "gdp_nominal_usd",
    )


def test_the_pillar_defines_no_selection_logic_of_its_own() -> None:
    """The look-ahead rule lives in one place, which is what #122 bought.

    The stub docstring described routing `commodity_price` by a commodity
    complex held in `Observation.meta`. Nothing populates `meta` except
    `ManualSource`, and the registry keys that indicator by currency, so the
    described routing would find nothing and take terms of trade away from
    exactly the three currencies it exists for.
    """
    for helper in ("_visible", "_newest_vintages", "_extract"):
        assert helper not in vars(ExternalPillar)
    assert ExternalPillar._extract is BasePillar._extract


# --- what _extract selects ---------------------------------------------------


def test_every_required_key_is_present_for_every_currency(
    pillar: ExternalPillar,
) -> None:
    """A dropped key and an empty sequence read the same and are different facts."""
    extracted = pillar._extract(universe(), G10, ASOF)

    assert set(extracted) == set(G10)
    for currency in G10:
        assert set(extracted[currency]) == set(EXPECTED_REQUIRES)
    assert extracted["USD"]["commodity_price"] == ()


def test_an_observation_not_yet_published_takes_no_part(pillar: ExternalPillar) -> None:
    """Period before the run date, release after it: the shape that flatters."""
    unpublished = obs(
        "current_account_gdp",
        "USD",
        99.9,
        QUARTER,
        released_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )

    values = components(pillar, [*universe(), unpublished])

    assert values["USD"]["current_account_gdp"] == -3.3


# --- the current account, carried as published -------------------------------


def test_the_current_account_is_the_published_level(pillar: ExternalPillar) -> None:
    """Percent of GDP as the source published it, with nothing applied."""
    values = components(pillar, universe())

    for currency, (account, _, _, _) in UNIVERSE.items():
        assert values[currency]["current_account_gdp"] == account


def test_the_current_account_reaches_raw(pillar: ExternalPillar) -> None:
    """`headline_component`, so a reader sees a number they can recognise."""
    scores = pillar.compute(universe(), G10, ASOF)

    assert pillar.headline_component == "current_account_gdp"
    for currency, (account, _, _, _) in UNIVERSE.items():
        assert scores[currency].raw == account


def test_a_surplus_scores_above_a_deficit(pillar: ExternalPillar) -> None:
    """The sign rule for this component, asserted on its own.

    A surplus is a standing bid for the currency and a deficit a standing offer
    that has to be financed.
    """
    values = components(pillar, universe())

    assert values["CHF"]["current_account_gdp"] == 6.2
    assert values["NZD"]["current_account_gdp"] == -5.4
    assert values["CHF"]["current_account_gdp"] > values["NZD"]["current_account_gdp"]


# --- the trade trend, and the unit it depends on -----------------------------


def test_the_trade_trend_is_the_three_month_change_in_percent_of_gdp(
    pillar: ExternalPillar,
) -> None:
    """Hand-computed for all eight, each a different number.

    USD is ``(-70e9 - -100e9) / 30e12 * 100 = 0.1``. A currency reading another
    currency's GDP lands on one of the other seven values and is visible.
    """
    values = components(pillar, universe())

    for currency, expected in EXPECTED_TRADE_TREND.items():
        assert values[currency]["trade_trend"] == pytest.approx(expected)


def test_an_improving_balance_is_positive_and_a_worsening_one_negative(
    pillar: ExternalPillar,
) -> None:
    """The sign rule for this component, asserted on its own.

    USD's balance is a deficit at both ends and still scores positive, because
    the component is the direction of travel and not the level.
    """
    values = components(pillar, universe())

    assert values["USD"]["trade_trend"] > 0
    assert UNIVERSE["USD"][2] > UNIVERSE["USD"][1]
    assert values["AUD"]["trade_trend"] < 0
    assert UNIVERSE["AUD"][2] < UNIVERSE["AUD"][1]


def test_the_ratio_depends_on_both_sides_being_in_the_same_unit(
    pillar: ExternalPillar,
) -> None:
    """Nothing here rescales, so the registry's unit contract is load-bearing.

    A nominal GDP filed in millions rather than in actual dollars makes every
    currency's `trade_trend` a million times smaller, by the same factor for all
    eight. The cross-sectional z-score absorbs a common factor exactly, so the
    ranking would be unchanged, every published number would be wrong and
    nothing would raise. This test is why `test_every_nominal_gdp_ref_is_in_dollars`
    exists rather than being assumed.
    """
    scaled = components(pillar, universe(gdp_scale=1e6))

    for currency, expected in EXPECTED_TRADE_TREND.items():
        assert scaled[currency]["trade_trend"] == pytest.approx(expected / 1e6)


def test_a_currency_with_no_nominal_gdp_has_no_trade_trend(
    pillar: ExternalPillar,
) -> None:
    """No denominator is an absence, not a zero change.

    The ruling was explicit: invent no fallback for a currency whose GDP cannot
    be verified. The component is absent and the blend renormalises over what is
    left.
    """
    values = components(pillar, universe(drop={"CHF": ("gdp_nominal_usd",)}))

    assert values["CHF"]["trade_trend"] is None
    assert values["CHF"]["current_account_gdp"] == 6.2


def test_a_nominal_gdp_of_zero_raises_rather_than_dividing(
    pillar: ExternalPillar,
) -> None:
    """A zero or negative GDP is corrupt input, not a currency with no data.

    Returning an absence would hide a broken registry entry behind the same
    answer a missing series gives.
    """
    broken = [
        item
        for item in universe()
        if not (item.indicator == "gdp_nominal_usd" and item.currency == "CHF")
    ]
    broken.append(obs("gdp_nominal_usd", "CHF", 0.0, GDP_PERIOD))

    with pytest.raises(ValueError, match="CHF"):
        components(pillar, broken)


# --- terms of trade, and the one deliberate zero -----------------------------


def test_terms_of_trade_is_zero_for_the_five_without_a_link(
    pillar: ExternalPillar,
) -> None:
    """A modelling statement, not missing data.

    Marking it absent instead would leave the z-score to be computed across
    three currencies, which on three points is not a z-score.
    """
    values = components(pillar, universe())

    for currency in ("USD", "EUR", "GBP", "JPY", "CHF"):
        assert meta(currency).commodity_link is None
        assert values[currency]["terms_of_trade"] == 0.0


def test_terms_of_trade_is_the_three_month_return_for_a_linked_currency(
    pillar: ExternalPillar,
) -> None:
    """Percent return, hand-computed: crude 80 to 88 is +10, iron 100 to 95 is -5."""
    values = components(pillar, universe())

    assert values["CAD"]["terms_of_trade"] == pytest.approx(10.0)
    assert values["AUD"]["terms_of_trade"] == pytest.approx(-5.0)


def test_a_rising_export_price_is_positive(pillar: ExternalPillar) -> None:
    """The sign rule for this component, asserted on its own.

    A rise in the export price raises national income and, through it, the
    currency, usually before it reaches any macro series.
    """
    values = components(pillar, universe())

    assert values["CAD"]["terms_of_trade"] > 0
    assert COMMODITY["CAD"][2] > COMMODITY["CAD"][1]
    assert values["AUD"]["terms_of_trade"] < 0


def test_a_linked_currency_with_no_price_is_absent_rather_than_zero(
    pillar: ExternalPillar,
) -> None:
    """The distinction the whole component turns on.

    A zero says the commodity impulse is neutral. For CAD that would be a
    confident claim about crude, made on a feed outage.
    """
    values = components(pillar, universe(drop={"CAD": ("commodity_price",)}))

    assert meta("CAD").commodity_link == "crude_oil"
    assert values["CAD"]["terms_of_trade"] is None
    assert values["USD"]["terms_of_trade"] == 0.0


def test_each_linked_currency_reads_its_own_commodity(pillar: ExternalPillar) -> None:
    """CAD takes crude, AUD iron ore, NZD dairy.

    A mis-routed link gives a plausible number under the wrong currency, which
    no later check would catch, so this asserts per currency and then swaps two
    series to prove both move.
    """
    with_nzd = [*universe(), *commodity("NZD", 50.0, 56.0)]

    values = components(pillar, with_nzd)

    assert meta("NZD").commodity_link == "dairy"
    assert values["CAD"]["terms_of_trade"] == pytest.approx(10.0)
    assert values["AUD"]["terms_of_trade"] == pytest.approx(-5.0)
    assert values["NZD"]["terms_of_trade"] == pytest.approx(12.0)

    swapped = [
        *[item for item in universe() if item.indicator != "commodity_price"],
        *commodity("CAD", 100.0, 95.0),
        *commodity("AUD", 80.0, 88.0),
    ]
    crossed = components(pillar, swapped)

    assert crossed["CAD"]["terms_of_trade"] == pytest.approx(-5.0)
    assert crossed["AUD"]["terms_of_trade"] == pytest.approx(10.0)


# --- the window is months, not observations ----------------------------------


def test_the_window_is_three_months_and_not_three_observations(
    pillar: ExternalPillar,
) -> None:
    """Crude is daily. Three observations of it is three days.

    `BasePillar.momentum` counts observations and says so in its own docstring,
    which is why this pillar does not use it. A three-day return on oil is a
    real number in the right unit and is not what the pillar is for.

    This case has four points and the oldest is exactly three months back, so
    the two rules coincide here. `test_three_observations_back_is_not_three_months_back`
    is the one that separates them.
    """
    daily = [
        obs(
            "commodity_price",
            "CAD",
            value,
            period,
            frequency=Frequency.DAILY,
            released_at=published(period, 1),
        )
        for period, value in (
            (BASELINE, 80.0),
            (date(2026, 5, 29), 87.0),
            (date(2026, 5, 30), 87.5),
            (NEWEST, 88.0),
        )
    ]
    observations = [
        *[item for item in universe() if item.currency != "CAD"],
        *[
            item
            for item in universe()
            if item.currency == "CAD" and item.indicator != "commodity_price"
        ],
        *daily,
    ]

    values = components(pillar, observations)

    # Three months back is 80.0, giving +10.0. Three observations back is 87.0,
    # which would give about +1.15.
    assert values["CAD"]["terms_of_trade"] == pytest.approx(10.0)


def test_a_baseline_older_than_the_window_allows_is_an_absence(
    pillar: ExternalPillar,
) -> None:
    """A hole in the series must not become a longer return wearing a short label.

    The window is three months by construction and at most six where the series
    has a gap. Beyond that the component is absent, because a nine-month return
    reported as a three-month one is the wrong number in the right unit.
    """
    stale_baseline = [
        obs("commodity_price", "CAD", 80.0, date(2025, 8, 1)),
        obs("commodity_price", "CAD", 88.0, NEWEST),
    ]
    observations = [
        *[
            item
            for item in universe()
            if not (item.currency == "CAD" and item.indicator == "commodity_price")
        ],
        *stale_baseline,
    ]

    values = components(pillar, observations)

    assert values["CAD"]["terms_of_trade"] is None


def test_a_baseline_inside_the_tolerance_still_answers(
    pillar: ExternalPillar,
) -> None:
    """The converse, which stops the test above passing against a pillar that
    refuses every inexact baseline."""
    late_baseline = [
        obs("commodity_price", "CAD", 80.0, date(2026, 2, 10)),
        obs("commodity_price", "CAD", 88.0, NEWEST),
    ]
    observations = [
        *[
            item
            for item in universe()
            if not (item.currency == "CAD" and item.indicator == "commodity_price")
        ],
        *late_baseline,
    ]

    values = components(pillar, observations)

    assert values["CAD"]["terms_of_trade"] == pytest.approx(10.0)


def test_a_single_observation_gives_no_change(pillar: ExternalPillar) -> None:
    """One point is not a change. It is not a change of zero either."""
    observations = [
        *[item for item in universe() if item.indicator != "trade_balance"],
        *[
            obs("trade_balance", currency, values[2], NEWEST)
            for currency, values in UNIVERSE.items()
        ],
    ]

    values = components(pillar, observations)

    assert values["USD"]["trade_trend"] is None


# --- the floor, and what a missing balance costs -----------------------------


def test_a_currency_missing_both_balances_is_absent(pillar: ExternalPillar) -> None:
    """0.30 of terms of trade alone is at the floor, so the pillar is absent.

    Terms of trade is never missing for a currency with no link, so this is the
    only route to an absent EXTERNAL.
    """
    held = {"terms_of_trade": 0.30}
    assert sum(held.values()) / 1.0 <= MIN_COMPONENT_WEIGHT

    scores = pillar.compute(
        universe(drop={"JPY": ("current_account_gdp", "trade_balance")}), G10, ASOF
    )

    assert scores["JPY"].z is None
    assert scores["JPY"].score == 0.0


def test_a_currency_holding_one_balance_still_scores(pillar: ExternalPillar) -> None:
    """The converse. 0.70 or 0.60 clears the floor and the blend renormalises.

    Without this the test above passes against a pillar that refuses every
    incomplete currency.
    """
    scores = pillar.compute(universe(drop={"JPY": ("trade_balance",)}), G10, ASOF)

    assert scores["JPY"].z is not None


def test_nothing_is_substituted_for_a_missing_balance(pillar: ExternalPillar) -> None:
    """Absence is `None`, never a zero. A zero current account is a reading."""
    values = components(
        pillar, universe(drop={"GBP": ("current_account_gdp", "trade_balance")})
    )

    assert values["GBP"]["current_account_gdp"] is None
    assert values["GBP"]["trade_trend"] is None
    assert values["GBP"]["terms_of_trade"] == 0.0


def test_a_reading_of_zero_survives_as_a_reading(pillar: ExternalPillar) -> None:
    """A current account of exactly 0.0 is a balanced economy, which is a finding."""
    flat = [
        item
        for item in universe()
        if not (item.indicator == "current_account_gdp" and item.currency == "GBP")
    ]
    flat.append(obs("current_account_gdp", "GBP", 0.0, QUARTER))

    values = components(pillar, flat)

    assert values["GBP"]["current_account_gdp"] == 0.0


# --- the cross-section -------------------------------------------------------


def test_moving_one_currency_moves_the_others(pillar: ExternalPillar) -> None:
    """The score is a rank against the others, not a reading of one economy."""
    before = pillar.compute(universe(), G10, ASOF)
    shifted = [
        item
        for item in universe()
        if not (item.indicator == "current_account_gdp" and item.currency == "USD")
    ]
    shifted.append(obs("current_account_gdp", "USD", 25.0, QUARTER))

    after = pillar.compute(shifted, G10, ASOF)

    for currency in ("EUR", "GBP", "JPY"):
        assert after[currency].score != before[currency].score


def test_every_currency_in_the_universe_gets_a_score(pillar: ExternalPillar) -> None:
    """The scorer detects thin coverage by looking for `z is None`, not absent keys."""
    scores = pillar.compute(universe(), G10, ASOF)

    assert set(scores) == set(G10)
    assert all(score.z is not None for score in scores.values())


def test_a_currency_outside_the_universe_raises(pillar: ExternalPillar) -> None:
    """`commodity_link` is read from `universe.meta`, which has no default.

    A currency the universe does not carry must not quietly take the five
    currencies' deliberate `0.0`, which is a statement about a known export
    basket rather than a fallback for an unknown one.
    """
    with pytest.raises(KeyError, match="XYZ"):
        components(pillar, universe(), ("USD", "EUR", "GBP", "XYZ"))


def test_a_non_finite_nominal_gdp_raises(pillar: ExternalPillar) -> None:
    """NaN passes every comparison, so `<= 0.0` would let it through.

    A NaN denominator gives a NaN component, a NaN z-score and a NaN score, and
    nothing on the way raises.
    """
    broken = [
        item
        for item in universe()
        if not (item.indicator == "gdp_nominal_usd" and item.currency == "CHF")
    ]
    broken.append(obs("gdp_nominal_usd", "CHF", float("nan"), GDP_PERIOD))

    with pytest.raises(ValueError, match="CHF"):
        components(pillar, broken)


def test_the_fixture_reproduces_a_hand_computed_blend(pillar: ExternalPillar) -> None:
    """Eight scores computed outside the pillar, to six places.

    Recomputed from the readings at the top of this file without any of the
    pillar's own helpers: a population z-score per component across the
    currencies that have it, weighted by section 3.5's sub-weights, renormalised
    per currency over the weight present, then centred on the run's blend mean
    and divided by its blend standard deviation.

    NZD is the interesting row. It carries a `commodity_link` and the fixture
    gives it no price, so its terms of trade is absent rather than zero, it
    renormalises over the 0.70 it holds, and it takes no part in that
    component's cross-section. A pillar that gave it the five currencies' `0.0`
    would move every other currency's terms-of-trade z-score as well as its own.

    This is the one assertion here that covers the whole pipeline rather than
    one step of it.
    """
    expected = {
        "USD": -0.784983,
        "EUR": 0.519763,
        "GBP": -0.981758,
        "JPY": 1.011608,
        "CHF": 0.589116,
        "CAD": 1.577646,
        "AUD": -1.426829,
        "NZD": -0.504564,
    }

    scores = pillar.compute(universe(), G10, ASOF)

    assert scores["USD"].blend_divisor_path == "run_local"
    assert scores["NZD"].z is not None
    for currency, z in expected.items():
        assert scores[currency].z == pytest.approx(z, abs=5e-7)


def test_a_linked_currency_with_no_price_leaves_that_cross_section(
    pillar: ExternalPillar,
) -> None:
    """The converse of giving NZD a zero, which is what makes the row above pin
    something.

    Handing an absent reading the five currencies' deliberate zero would put a
    ninth point into the terms-of-trade cross-section and move CAD and AUD,
    which have real commodity moves and nothing wrong with their data.
    """
    absent = pillar.compute(universe(), G10, ASOF)
    with_zero = pillar.compute([*universe(), *commodity("NZD", 50.0, 50.0)], G10, ASOF)

    assert absent["CAD"].z != with_zero["CAD"].z
    assert absent["AUD"].z != with_zero["AUD"].z


def test_the_newest_level_wins_over_an_older_one(pillar: ExternalPillar) -> None:
    """Every level in the fixture had one period, so nothing pinned which one is read.

    A mutation reading the oldest observation rather than the newest survived
    the whole file until this was added, because one observation is both.
    """
    older_account = obs("current_account_gdp", "USD", -9.9, date(2026, 1, 1))
    older_gdp = obs("gdp_nominal_usd", "USD", 15e12, date(2024, 1, 1))

    values = components(pillar, [*universe(), older_account, older_gdp])

    assert values["USD"]["current_account_gdp"] == -3.3
    # The older GDP is half the newer one, so reading it would double the trend.
    assert values["USD"]["trade_trend"] == pytest.approx(0.1)


def test_three_observations_back_is_not_three_months_back(
    pillar: ExternalPillar,
) -> None:
    """The daily case, with enough points that the two answers differ.

    An earlier version of this test gave crude four observations with the oldest
    exactly three months back, so counting observations and counting months
    landed on the same reading and a pillar doing either passed. The series now
    has six points clustered at the recent end: three observations back is
    2026-05-28 and three months back is 2026-03-01, and only one of them gives
    the +10.0 this asserts.
    """
    daily = [
        obs(
            "commodity_price",
            "CAD",
            value,
            period,
            frequency=Frequency.DAILY,
            released_at=published(period, 1),
        )
        for period, value in (
            (BASELINE, 80.0),
            (date(2026, 5, 27), 86.0),
            (date(2026, 5, 28), 86.5),
            (date(2026, 5, 29), 87.0),
            (date(2026, 5, 30), 87.5),
            (NEWEST, 88.0),
        )
    ]
    observations = [
        *[
            item
            for item in universe()
            if not (item.currency == "CAD" and item.indicator == "commodity_price")
        ],
        *daily,
    ]

    values = components(pillar, observations)

    # Three months back is 80.0, giving +10.0. Three observations back is 86.5,
    # which would give about +1.73.
    assert values["CAD"]["terms_of_trade"] == pytest.approx(10.0)


def test_the_baseline_is_the_newest_reading_the_window_admits(
    pillar: ExternalPillar,
) -> None:
    """Two readings inside the window, and the later one is the baseline.

    Taking the first qualifying reading instead would widen the window silently
    by however far back the series happens to start, which is the same defect as
    the unbounded tolerance and is not caught by a series with one candidate.
    """
    candidates = [
        obs("commodity_price", "CAD", value, period)
        for period, value in (
            (date(2026, 2, 20), 79.0),
            (BASELINE, 80.0),
            (NEWEST, 88.0),
        )
    ]
    observations = [
        *[
            item
            for item in universe()
            if not (item.currency == "CAD" and item.indicator == "commodity_price")
        ],
        *candidates,
    ]

    values = components(pillar, observations)

    # 88 against 80 is +10.0. Against the earlier 79.0 it would be about +11.39.
    assert values["CAD"]["terms_of_trade"] == pytest.approx(10.0)
