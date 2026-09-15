"""Tests for the three normalisation primitives on `BasePillar`.

These are the transforms every pillar's score is eventually built from, so the
properties worth testing are the ones whose failure would produce a plausible
number rather than an exception: the wrong estimator, a z-score computed against
a cross-section too thin to mean anything, a currency with no data quietly
counted as sitting at the mean, and a per-currency implementation that passes
every test except the one that makes the transform cross-sectional at all.

Two estimators are in play and they are deliberately opposite.
`cross_sectional_z` uses the population form, because the eight currencies are
the whole scored universe. `time_series_z` uses the sample form, because an
observed history is a sample of the process that generated it. Every expected
figure below was computed by hand at 30 significant figures and is pinned to a
precision that tells the two estimators apart.

Nothing here reaches the network, and nothing here builds an `Observation` from
a real source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest

from fbe.pillars.base import MIN_CROSS_SECTION, BasePillar
from fbe.types import Frequency, Observation

# --- hand-computed expectations ---------------------------------------------

FIVE: Mapping[str, float | None] = {
    "USD": 5.0,
    "EUR": 3.0,
    "GBP": 2.0,
    "JPY": 1.0,
    "CHF": 4.0,
}
"""A five-currency cross-section with mean 3.0 and population sd sqrt(2)."""

FIVE_POPULATION_Z: Mapping[str, float] = {
    "USD": 1.41421356237309505,
    "EUR": 0.0,
    "GBP": -0.70710678118654752,
    "JPY": -1.41421356237309505,
    "CHF": 0.70710678118654752,
}
"""``(value - 3.0) / sqrt(10 / 5)``, computed by hand. Under the sample form the
divisor would be ``sqrt(10 / 4)`` and USD would read 1.26491106406735173, so
these figures tell the two estimators apart on their own."""

FIVE_SAMPLE_Z_USD = 1.26491106406735173
"""What USD would read if `cross_sectional_z` used ``ddof=1``. Named so a test
can assert the answer is not this, rather than only that it is the other one."""

SERIES_SAMPLE_Z = 1.52542553961938009
"""The z-score of 12 against the history 1..12 under ``ddof=1``: mean 6.5,
sample sd sqrt(13). Hand-computed."""

SERIES_POPULATION_Z = 1.59325501363138301
"""The same newest value under ``ddof=0``. `time_series_z` must not return it."""


def _observation(value: float, period: date, *, unit: str = "percent") -> Observation:
    """Build one observation. Only ``value`` and ``period`` are read here."""
    return Observation(
        indicator="cot_net_pct_oi",
        currency="AUD",
        value=value,
        period=period,
        source="test",
        series_id="X",
        unit=unit,
        frequency=Frequency.MONTHLY,
    )


def _monthly(values: Sequence[float], *, start_year: int = 2024) -> list[Observation]:
    """Build a monthly series ascending by period, one observation per month.

    Periods are stamped on the first of the month purely so the sequence is
    ordered and distinct. Nothing in these tests depends on which day of a month
    stamps a monthly period, which is the question #27 has not settled.
    """
    return [
        _observation(value, date(start_year + index // 12, index % 12 + 1, 1))
        for index, value in enumerate(values)
    ]


# --- criterion 1: the population estimator ----------------------------------


def test_the_cross_section_is_standardised_by_the_population_estimator() -> None:
    computed = BasePillar.cross_sectional_z(FIVE)

    for currency, expected in FIVE_POPULATION_Z.items():
        assert computed[currency] == pytest.approx(expected, abs=1e-12), currency


def test_the_sample_estimator_would_give_a_different_answer() -> None:
    """The guard on criterion 1. The two estimators must not be confusable."""
    computed = BasePillar.cross_sectional_z(FIVE)

    assert computed["USD"] != pytest.approx(FIVE_SAMPLE_Z_USD, abs=1e-6)


def test_every_currency_asked_about_comes_back() -> None:
    computed = BasePillar.cross_sectional_z(FIVE)

    assert set(computed) == set(FIVE)


# --- criterion 2: too thin a cross-section ----------------------------------


def test_fewer_than_the_minimum_returns_none_for_every_currency() -> None:
    """Including the currencies that had data, which is the point of the rule."""
    values: Mapping[str, float | None] = {
        "USD": 5.0,
        "EUR": 3.0,
        "GBP": None,
        "JPY": None,
    }

    computed = BasePillar.cross_sectional_z(values)

    assert computed == {"USD": None, "EUR": None, "GBP": None, "JPY": None}


def test_exactly_the_minimum_is_enough_to_score() -> None:
    values: Mapping[str, float | None] = {
        "USD": 5.0,
        "EUR": 3.0,
        "GBP": 1.0,
        "JPY": None,
    }

    computed = BasePillar.cross_sectional_z(values)

    assert computed["JPY"] is None
    assert [computed[c] for c in ("USD", "EUR", "GBP")] == pytest.approx(
        [1.224744871391589, 0.0, -1.224744871391589], abs=1e-12
    )


@pytest.mark.parametrize("usable", [0, 1, 2])
def test_no_count_below_the_minimum_scores_anything(usable: int) -> None:
    values: dict[str, float | None] = {
        code: (float(index) if index < usable else None)
        for index, code in enumerate(("USD", "EUR", "GBP", "JPY"))
    }

    computed = BasePillar.cross_sectional_z(values)

    assert set(computed.values()) == {None}


def test_the_minimum_is_read_from_the_module_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserting the constant equals 3 would not catch a hardcoded 3.

    Raising it and watching a cross-section that was scoring stop scoring is
    what shows the function reads the constant rather than a literal of its own.
    """
    values: Mapping[str, float | None] = {"USD": 5.0, "EUR": 3.0, "GBP": 1.0}
    assert set(BasePillar.cross_sectional_z(values).values()) != {None}

    monkeypatch.setattr("fbe.pillars.base.MIN_CROSS_SECTION", MIN_CROSS_SECTION + 1)

    assert set(BasePillar.cross_sectional_z(values).values()) == {None}


# --- criterion 3: a cross-section that agrees -------------------------------


def test_identical_values_score_zero_rather_than_missing() -> None:
    """Every currency reporting the same number is a finding, not a gap."""
    values: Mapping[str, float | None] = {"USD": 2.5, "EUR": 2.5, "GBP": 2.5}

    computed = BasePillar.cross_sectional_z(values)

    assert computed == {"USD": 0.0, "EUR": 0.0, "GBP": 0.0}


@pytest.mark.parametrize("value", [0.1, 0.3, 1.1, 2.9, 0.07])
def test_identical_values_score_zero_even_when_the_value_is_not_exact(
    value: float,
) -> None:
    """2.5 is exactly representable in binary and 0.1 is not.

    The mean of three values of 0.1 is 0.10000000000000002, so each deviation
    is about -1.4e-17 and so is the standard deviation, and dividing one by the
    other returns -1.0 for all three: every currency confidently below a mean
    that all three of them equal. Testing only round numbers would never show
    it, which is how it survived the first version of this file.
    """
    values: Mapping[str, float | None] = {"USD": value, "EUR": value, "GBP": value}

    computed = BasePillar.cross_sectional_z(values)

    assert computed == {"USD": 0.0, "EUR": 0.0, "GBP": 0.0}


@pytest.mark.parametrize("value", [0.1, 0.3, 1.1])
def test_a_flat_history_of_an_inexact_value_is_still_none(value: float) -> None:
    """The same hazard on the time-series side."""
    assert BasePillar.time_series_z(_monthly([value] * 12), lookback_years=5) is None


def test_identical_values_beside_a_missing_one_still_score_zero() -> None:
    values: Mapping[str, float | None] = {
        "USD": 2.5,
        "EUR": 2.5,
        "GBP": 2.5,
        "JPY": None,
    }

    computed = BasePillar.cross_sectional_z(values)

    assert computed == {"USD": 0.0, "EUR": 0.0, "GBP": 0.0, "JPY": None}


# --- criterion 4: a missing currency takes no part --------------------------


def test_a_currency_with_no_reading_stays_none() -> None:
    values: Mapping[str, float | None] = {**FIVE, "NZD": None}

    computed = BasePillar.cross_sectional_z(values)

    assert computed["NZD"] is None


def test_adding_a_missing_currency_moves_nobody() -> None:
    """A None counted as a zero would drag every other currency's z-score."""
    before = BasePillar.cross_sectional_z(FIVE)

    after = BasePillar.cross_sectional_z({**FIVE, "NZD": None})

    for currency in FIVE:
        assert after[currency] == pytest.approx(before[currency], abs=1e-12), currency


def test_a_missing_currency_does_not_shift_the_mean() -> None:
    """The sharper form of the same rule, asserted on a set whose mean is not 0.

    If a ``None`` were read as 0.0 the mean would fall and every z would rise.
    """
    values: Mapping[str, float | None] = {"USD": 10.0, "EUR": 10.0, "GBP": 4.0}
    before = BasePillar.cross_sectional_z(values)

    after = BasePillar.cross_sectional_z({**values, "JPY": None})

    assert after["JPY"] is None
    assert {currency: after[currency] for currency in values} == before


# --- criterion 5: the transform is cross-sectional --------------------------


def test_moving_one_currency_moves_another_that_did_not_move() -> None:
    """The property a per-currency implementation cannot have.

    GBP's own reading is identical in both runs. Its z-score must still change,
    because the question is never what GBP did, it is what GBP did compared with
    everyone else.
    """
    before = BasePillar.cross_sectional_z(FIVE)

    after = BasePillar.cross_sectional_z({**FIVE, "USD": 25.0})

    assert after["GBP"] != pytest.approx(before["GBP"], abs=1e-9)


def test_the_currency_that_moved_is_not_the_only_one_that_changed() -> None:
    before = BasePillar.cross_sectional_z(FIVE)

    after = BasePillar.cross_sectional_z({**FIVE, "USD": 25.0})

    moved = [
        currency
        for currency in FIVE
        if after[currency] != pytest.approx(before[currency], abs=1e-9)
    ]
    assert sorted(moved) == sorted(FIVE)


# --- criterion 6: the sample estimator on a history -------------------------


def test_a_history_is_standardised_by_the_sample_estimator() -> None:
    series = _monthly([float(value) for value in range(1, 13)])

    assert BasePillar.time_series_z(series, lookback_years=5) == pytest.approx(
        SERIES_SAMPLE_Z, abs=1e-12
    )


def test_the_population_estimator_would_give_a_different_answer() -> None:
    """The guard on criterion 6, mirroring the one on criterion 1."""
    series = _monthly([float(value) for value in range(1, 13)])

    assert BasePillar.time_series_z(series, lookback_years=5) != pytest.approx(
        SERIES_POPULATION_Z, abs=1e-6
    )


def test_the_newest_value_is_the_one_scored() -> None:
    """Not the mean of the window, and not the oldest."""
    rising = _monthly([float(value) for value in range(1, 13)])
    falling = _monthly([float(value) for value in range(12, 0, -1)])

    assert BasePillar.time_series_z(rising, lookback_years=5) == pytest.approx(
        -BasePillar.time_series_z(falling, lookback_years=5), abs=1e-12
    )


# --- criterion 7: too short a history, and a flat one -----------------------


def test_eleven_observations_are_too_few() -> None:
    series = _monthly([float(value) for value in range(1, 12)])

    assert len(series) == 11
    assert BasePillar.time_series_z(series, lookback_years=5) is None


def test_twelve_observations_are_enough() -> None:
    series = _monthly([float(value) for value in range(1, 13)])

    assert len(series) == 12
    assert BasePillar.time_series_z(series, lookback_years=5) is not None


def test_a_flat_history_returns_none_rather_than_zero() -> None:
    """Unlike the cross-section, where every currency agreeing is a finding.

    A series that has not moved in five years says nothing about whether its
    newest print is high or low, so there is no z-score to report.
    """
    series = _monthly([4.0] * 12)

    assert BasePillar.time_series_z(series, lookback_years=5) is None


def test_an_empty_series_returns_none() -> None:
    assert BasePillar.time_series_z([], lookback_years=5) is None


# --- criterion 8: the window bounds -----------------------------------------


def test_an_observation_older_than_the_lookback_is_excluded() -> None:
    """The old reading sits well outside the window, not on its edge.

    Placed clearly outside rather than exactly on the cutoff, so this asserts
    the window is applied at all without depending on which day stamps a
    period, which #27 has not settled.
    """
    inside = _monthly([float(value) for value in range(1, 13)], start_year=2024)
    ancient = [_observation(-500.0, date(2005, 1, 1))]

    with_ancient = BasePillar.time_series_z(ancient + inside, lookback_years=5)

    assert with_ancient == pytest.approx(
        BasePillar.time_series_z(inside, lookback_years=5), abs=1e-12
    )


def test_an_observation_after_the_asof_is_excluded() -> None:
    series = _monthly([float(value) for value in range(1, 13)], start_year=2024)
    future = _observation(900.0, date(2026, 6, 1))
    asof = date(2025, 6, 1)

    with_future = BasePillar.time_series_z(
        [*series, future], lookback_years=5, asof=asof
    )

    assert with_future == pytest.approx(
        BasePillar.time_series_z(series, lookback_years=5, asof=asof), abs=1e-12
    )


def test_the_future_observation_leaves_the_spread_alone_too() -> None:
    """A filter applied only when choosing the newest value is not enough.

    The excluded observation would still widen the window's standard deviation
    and pull its mean, which is look-ahead bias wearing a smaller hat. Asserted
    against the hand-computed figure rather than against another call, so this
    holds even if both calls were wrong in the same way.
    """
    series = _monthly([float(value) for value in range(1, 13)], start_year=2024)

    polluted = BasePillar.time_series_z(
        [*series, _observation(900.0, date(2026, 6, 1))],
        lookback_years=5,
        asof=date(2025, 6, 1),
    )

    assert polluted == pytest.approx(SERIES_SAMPLE_Z, abs=1e-12)


def test_asof_defaults_to_the_newest_period_in_the_series() -> None:
    series = _monthly([float(value) for value in range(1, 13)], start_year=2024)
    newest = series[-1].period

    assert BasePillar.time_series_z(series, lookback_years=5) == pytest.approx(
        BasePillar.time_series_z(series, lookback_years=5, asof=newest), abs=1e-12
    )


def test_the_lookback_length_changes_the_answer() -> None:
    """Otherwise the window could be ignored and every test above still pass.

    Two years of monthly data, scored against one year of it and against all of
    it. The newest value is the same; what it is being compared with is not.
    """
    series = _monthly([float(value) for value in range(1, 25)], start_year=2020)
    asof = date(2021, 12, 1)

    one_year = BasePillar.time_series_z(series, lookback_years=1, asof=asof)
    five_years = BasePillar.time_series_z(series, lookback_years=5, asof=asof)

    assert one_year is not None
    assert five_years is not None
    assert one_year != pytest.approx(five_years, abs=1e-9)


def test_the_cutoff_boundary_keeps_an_observation_of_the_same_day() -> None:
    """Asserts the comparison, not a period convention.

    Every date here is constructed outright, so nothing depends on which day of
    a month stamps a monthly period. The docstring says observations *older
    than* the cutoff are excluded, which makes one landing on it a keeper.
    """
    asof = date(2025, 6, 1)
    on_the_cutoff = _observation(-40.0, date(2020, 6, 1))
    inside = _monthly([float(value) for value in range(1, 13)], start_year=2024)

    with_boundary = BasePillar.time_series_z(
        [on_the_cutoff, *inside], lookback_years=5, asof=asof
    )

    assert with_boundary != pytest.approx(
        BasePillar.time_series_z(inside, lookback_years=5, asof=asof), abs=1e-9
    )


def test_a_leap_day_asof_steps_back_to_a_real_date() -> None:
    """29 February has no counterpart in a non-leap year.

    Rolling forward to 1 March would move the cutoff a day later and drop an
    observation the configured lookback asked for. Asserted through the public
    function: the observation on 28 February 2023 is inside a one-year window
    ending on 29 February 2024, and excluding it changes the answer.
    """
    asof = date(2024, 2, 29)
    # Twelve months from March 2023 to February 2024, so the one-year window is
    # exactly full without the boundary observation and the comparison is not
    # None against None.
    inside = [
        _observation(
            float(month + 1), date(2023 + (month + 2) // 12, (month + 2) % 12 + 1, 1)
        )
        for month in range(12)
    ]
    assert inside[0].period == date(2023, 3, 1)
    assert inside[-1].period == date(2024, 2, 1)
    boundary = _observation(-40.0, date(2023, 2, 28))

    without = BasePillar.time_series_z(inside, lookback_years=1, asof=asof)
    with_boundary = BasePillar.time_series_z(
        [boundary, *inside], lookback_years=1, asof=asof
    )

    assert without is not None
    assert with_boundary != pytest.approx(without, abs=1e-9)


def test_a_leap_day_asof_four_years_back_keeps_the_leap_day() -> None:
    """2020 is a leap year, so no fallback applies and the cutoff is 29 February."""
    asof = date(2024, 2, 29)
    inside = _monthly([float(value) for value in range(1, 13)], start_year=2023)
    boundary = _observation(-40.0, date(2020, 2, 29))

    with_boundary = BasePillar.time_series_z(
        [boundary, *inside], lookback_years=4, asof=asof
    )

    assert with_boundary != pytest.approx(
        BasePillar.time_series_z(inside, lookback_years=4, asof=asof), abs=1e-9
    )


@pytest.mark.parametrize("years", [-1, -5])
def test_a_negative_lookback_is_refused_rather_than_answered(years: int) -> None:
    """It would put the window's start after its end and answer None.

    None means the history is too short, and a misconfigured window is not that.
    """
    series = _monthly([float(value) for value in range(1, 13)])

    with pytest.raises(ValueError, match="lookback"):
        BasePillar.time_series_z(series, lookback_years=years)


# --- criterion 9: the change over N periods ---------------------------------


def test_momentum_is_the_difference_between_two_observations() -> None:
    series = _monthly([1.0, 2.0, 4.0, 10.0])

    assert BasePillar.momentum(series, periods=3) == pytest.approx(9.0, abs=1e-12)


def test_momentum_over_more_periods_than_the_series_holds_is_none() -> None:
    series = _monthly([1.0, 2.0, 4.0, 10.0])

    assert BasePillar.momentum(series, periods=4) is None


@pytest.mark.parametrize(
    ("periods", "expected"),
    [(1, 6.0), (2, 8.0), (3, 9.0)],
)
def test_momentum_counts_back_from_the_newest(periods: int, expected: float) -> None:
    series = _monthly([1.0, 2.0, 4.0, 10.0])

    assert BasePillar.momentum(series, periods=periods) == pytest.approx(
        expected, abs=1e-12
    )


def test_momentum_is_signed() -> None:
    """A falling series gives a negative change, not its magnitude."""
    series = _monthly([10.0, 4.0, 2.0, 1.0])

    assert BasePillar.momentum(series, periods=3) == pytest.approx(-9.0, abs=1e-12)


def test_momentum_is_not_rebased_to_a_percentage() -> None:
    """2.0 to 3.0 is a change of 1.0 in the series' unit, not 50."""
    series = _monthly([2.0, 3.0])

    assert BasePillar.momentum(series, periods=1) == pytest.approx(1.0, abs=1e-12)


def test_momentum_ignores_the_observations_in_between() -> None:
    """It is a difference of two endpoints, not an average of the path."""
    straight = _monthly([0.0, 1.0, 2.0, 3.0])
    jagged = _monthly([0.0, 99.0, -99.0, 3.0])

    assert BasePillar.momentum(straight, periods=3) == BasePillar.momentum(
        jagged, periods=3
    )


def test_momentum_of_an_empty_series_is_none() -> None:
    assert BasePillar.momentum([], periods=1) is None


@pytest.mark.parametrize("periods", [-1, -3])
def test_a_negative_horizon_is_refused_rather_than_answered(periods: int) -> None:
    """``series[-1 - -1]`` is ``series[0]``, so this would return a real-looking
    number differenced against the wrong end of the series, and the length check
    would not catch it either.
    """
    series = _monthly([1.0, 2.0, 4.0, 10.0])

    with pytest.raises(ValueError, match="periods"):
        BasePillar.momentum(series, periods=periods)


def test_momentum_at_zero_periods_is_zero_not_none() -> None:
    """A change over no periods is a real answer, and it is no change."""
    series = _monthly([1.0, 2.0, 4.0])

    assert BasePillar.momentum(series, periods=0) == pytest.approx(0.0, abs=1e-12)


# --- criterion 10: nothing is substituted for absent data -------------------


def test_no_path_substitutes_a_number_for_absent_data() -> None:
    """Every degenerate case answers None, and never a plausible-looking zero."""
    assert BasePillar.cross_sectional_z({"USD": 1.0, "EUR": None})["USD"] is None
    assert BasePillar.time_series_z(_monthly([1.0] * 11), lookback_years=5) is None
    assert BasePillar.time_series_z(_monthly([4.0] * 12), lookback_years=5) is None
    assert BasePillar.momentum(_monthly([1.0, 2.0]), periods=5) is None


def test_the_input_mapping_is_not_mutated() -> None:
    """A caller's mapping is theirs. Filling it in would be a silent write."""
    values: dict[str, float | None] = {**FIVE, "NZD": None}
    before = dict(values)

    BasePillar.cross_sectional_z(values)

    assert values == before


def test_the_input_series_is_not_reordered_in_place() -> None:
    series = _monthly([3.0, 1.0, 2.0, 10.0])
    before = list(series)

    BasePillar.time_series_z(series, lookback_years=5)
    BasePillar.momentum(series, periods=2)

    assert series == before
