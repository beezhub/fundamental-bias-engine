"""Tests for EMPLOYMENT, the pillar with one sign flip and no partial state.

Three properties carry most of the risk here:

- The flip. `unemployment_6m` inverts the published change so that a falling
  unemployment rate scores positive, and `unemployment_chg_6m` carries the same
  number with its published sign for the reader. Inverting the wrong one, or
  inverting twice, reads a deteriorating labour market as a tightening one.
- The denominator. ``employment_trend`` is a count divided by a level, and the
  count and the level come from the same series so their units cancel. Dividing
  by the wrong currency's level, or scoring the raw count, produces a ranking of
  country size that looks like a ranking of hiring.
- The floor. Both components carry 0.50, and `MIN_COMPONENT_WEIGHT` is "at or
  below", so a currency holding one of the two is scored absent rather than on
  half the evidence. There is no partial state for this pillar and the tests
  assert that there is none.

The windows are in months rather than in observations, which is the sixth
acceptance criterion on issue #157. The rate is quarterly for CHF and NZD and
employment is quarterly for EUR, GBP, CHF and NZD, so a window counted in
observations would reach eighteen months back for those and six for the rest.
GBP is mixed, a monthly rate against a quarterly hiring series, and has a
fixture of its own because it is the only currency whose two components age on
different cadences.

Every observation is built in the test that uses it and every expected number is
worked by hand in the test's own docstring. Nothing reaches the network.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fbe.datasources.registry import INDICATORS, UNCONSUMED_INDICATORS
from fbe.pillars.base import MIN_COMPONENT_WEIGHT
from fbe.pillars.employment import (
    HIRING_WINDOW_MONTHS,
    MONTHS_PER_YEAR,
    UNEMPLOYMENT_WINDOW_MONTHS,
    EmploymentPillar,
)
from fbe.types import Frequency, Observation
from fbe.universe import G10

ASOF = date(2026, 9, 15)

RATE = "unemployment_rate"
CHG = "employment_chg"
LEVEL = "employment_level"

SPEC = Path(__file__).resolve().parents[1] / "docs" / "scoring-spec.md"

EXPECTED_REQUIRES = (RATE, CHG, LEVEL)
"""Spelled out rather than read off the pillar, so a key silently dropped from
`requires` fails here rather than quietly shrinking what these tests cover."""


@pytest.fixture
def pillar() -> EmploymentPillar:
    return EmploymentPillar()


def months_before(anchor: date, months: int) -> date:
    """``anchor`` shifted back by whole months, keeping the day of the month."""
    total = (anchor.year * 12 + anchor.month - 1) - months
    return date(total // 12, total % 12 + 1, anchor.day)


def obs(
    indicator: str,
    value: float,
    period: date,
    *,
    currency: str = "USD",
    frequency: Frequency = Frequency.MONTHLY,
    released_at: datetime | None = None,
) -> Observation:
    """One observation, released the day after the period it describes.

    A test that wants the visibility rule exercised passes its own
    ``released_at``. The default is deliberately early rather than realistic, so
    that a test which is not about publication lag is not accidentally about it.
    """
    stamped = (
        released_at
        if released_at is not None
        else datetime(period.year, period.month, period.day, 12, 0, tzinfo=UTC)
    )
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=period,
        source="test",
        series_id=f"test-{indicator}",
        unit=INDICATORS[indicator].unit,
        frequency=frequency,
        released_at=stamped,
    )


def series(
    indicator: str,
    values: Sequence[float],
    *,
    currency: str = "USD",
    step_months: int = 1,
    end: date = date(2026, 9, 1),
) -> list[Observation]:
    """A series ending at ``end``, oldest first, stepping back by whole months.

    ``step_months`` is the series' own cadence: 1 for a monthly publisher and 3
    for a quarterly one. It is a parameter rather than a constant because the
    difference between six months and six observations is exactly what several
    of these tests are about, and because the cadence is per series rather than
    per currency: GBP publishes the rate monthly and employment quarterly.
    """
    frequency = Frequency.MONTHLY if step_months == 1 else Frequency.QUARTERLY
    periods = [
        months_before(end, step_months * offset)
        for offset in reversed(range(len(values)))
    ]
    return [
        obs(indicator, value, period, currency=currency, frequency=frequency)
        for value, period in zip(values, periods, strict=True)
    ]


def run(
    pillar: EmploymentPillar,
    observations: Sequence[Observation],
    *,
    currencies: Sequence[str] | None = None,
) -> dict[str, dict[str, float | None]]:
    """Extract and transform one run, returning the components per currency."""
    wanted = tuple(currencies if currencies is not None else sorted(G10))
    extracted = pillar._extract(list(observations), wanted, ASOF)
    return {
        currency: dict(components)
        for currency, components in pillar._transform(extracted, ASOF).items()
    }


def healthy(
    currency: str = "USD",
    *,
    step_months: int = 1,
    rate_values: Sequence[float] = (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4),
    chg_value: float = 120.0,
    level_value: float = 150_000.0,
) -> list[Observation]:
    """One currency with all three series present and usable.

    The defaults are a labour market tightening steadily, which gives both
    components a non-zero value of a known sign, so a test asserting that
    something else went wrong is not passing on a pillar that returned zero.
    """
    count = UNEMPLOYMENT_WINDOW_MONTHS // step_months + 1
    return [
        *series(RATE, rate_values, currency=currency, step_months=step_months),
        *series(
            CHG,
            [chg_value] * count,
            currency=currency,
            step_months=step_months,
        ),
        *series(
            LEVEL,
            [level_value] * count,
            currency=currency,
            step_months=step_months,
        ),
    ]


# --- the registry entry the ruling asked for ---------------------------------


def test_the_employment_level_is_registered_in_persons() -> None:
    """Criterion: unit ``persons``, attributed to EMPLOYMENT, allowance derived.

    The unit is the assertion that matters. `employment_trend` divides a count
    by this level, so a level filed in a different unit from the flow it is
    divided by gives a number that is wrong by three orders of magnitude and
    still lands inside the clip band.
    """
    spec = INDICATORS[LEVEL]

    assert spec.unit == "persons"
    assert spec.pillar is not None and spec.pillar.name == "EMPLOYMENT"
    assert spec.max_staleness_days == INDICATORS[CHG].max_staleness_days


def test_the_level_reuses_the_flows_own_refs() -> None:
    """The ruling: take the stock from the same source the flow is differenced from.

    Seven of the eight `employment_chg` refs already point at a published level
    and reach the flow through ``diff``. The stock is those same refs under
    ``level``. Asserted field by field rather than by identity, because the
    thing that would go wrong is one currency's identifier drifting between the
    two keys, which is exactly what `employment_trend` cannot survive: the count
    and the level would then describe different populations and their units
    would no longer cancel.
    """
    flow = INDICATORS[CHG].series
    stock = INDICATORS[LEVEL].series

    assert set(stock) <= set(flow)
    for currency, ref in stock.items():
        origin = flow[currency]
        assert ref.source == origin.source, currency
        assert ref.series_id == origin.series_id, currency
        assert ref.unit == origin.unit, currency
        assert ref.frequency == origin.frequency, currency
        assert ref.last_observed == origin.last_observed, currency
        assert ref.verified == origin.verified, currency
        assert ref.transform == "level", currency
        assert origin.transform == "diff", currency


def test_the_euro_has_no_employment_level_and_is_given_no_substitute() -> None:
    """Criterion: a currency with a flow and no verified level gets no stand-in.

    EUR's `employment_chg` is a manual entry with no live source behind it: the
    euro-area level stopped publishing and the registry records that. There is
    no level to derive, so there is no EUR ref here. A German series is current
    and is deliberately not used, because EUR's flow is a euro-area figure and
    dividing it by a German level would compare a bloc's hiring with one
    member's workforce, which is roughly a factor of four and entirely
    plausible-looking.
    """
    assert "EUR" not in INDICATORS[LEVEL].series
    assert "EUR" in INDICATORS[CHG].series
    assert INDICATORS[CHG].series["EUR"].verified is False


def test_the_level_is_consumed_rather_than_declared_unconsumed() -> None:
    """Criterion: it appears in `requires`, so the agreement test passes.

    `tests/test_registry_pillar_agreement.py` fails an indicator attributed to a
    pillar that neither asks for it nor lists it unconsumed. Registering the key
    and forgetting the `requires` entry is the way this lands half-done.
    """
    assert tuple(EmploymentPillar.requires) == EXPECTED_REQUIRES
    assert LEVEL not in UNCONSUMED_INDICATORS


# --- what _extract selects ---------------------------------------------------


def test_the_three_series_come_back_sorted_oldest_first(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 1, the ordering half.

    Every window in this pillar indexes from the newest end, so a series handed
    over in the wrong order would measure the change backwards and flip the sign
    of the whole pillar without raising.
    """
    extracted = pillar._extract(healthy(), ("USD",), ASOF)

    assert set(extracted["USD"]) == set(EXPECTED_REQUIRES)
    for indicator in EXPECTED_REQUIRES:
        periods = [entry.period for entry in extracted["USD"][indicator]]
        assert periods == sorted(periods), indicator
        assert len(periods) == len(set(periods)), indicator


def test_an_observation_not_yet_published_takes_no_part(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 1, the visibility half, which is what makes a backtest honest.

    Filtering on the period a figure describes rather than on the date it was
    released is the defect that lets a run see numbers that did not exist yet.
    This pillar does not override `_extract`, so the rule it must obey is the
    shared one, and this asserts it arrives rather than that it exists.
    """
    unpublished = obs(
        RATE,
        99.0,
        date(2026, 9, 1),
        released_at=datetime(2026, 12, 1, 12, tzinfo=UTC),
    )

    extracted = pillar._extract([*healthy(), unpublished], ("USD",), ASOF)

    assert 99.0 not in [entry.value for entry in extracted["USD"][RATE]]


def test_the_pillar_does_not_carry_its_own_copy_of_the_shared_rules(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 1: shared helpers rather than a copy.

    #122 exists because a second pillar copying `_visible` and `_newest_vintages`
    is how the two drift apart. `BasePillar._extract` is concrete, this pillar
    reads three per-currency series, which is exactly the shape the base serves,
    so there is nothing here to override.
    """
    import fbe.pillars.base as base_module

    assert "_extract" not in vars(EmploymentPillar)
    assert "_visible" not in vars(EmploymentPillar)
    assert "_newest_vintages" not in vars(EmploymentPillar)
    assert type(pillar)._extract is base_module.BasePillar._extract


# --- the unemployment component and its single sign flip ---------------------


def test_a_falling_unemployment_rate_scores_positive(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 2, worked by hand.

    The rate runs 5.0 down to 4.4 in seven monthly steps ending 2026-09-01, so
    the oldest print is 2026-03-01 at 5.0 and that is exactly six months back.
    The published change is ``4.4 - 5.0 = -0.6`` percentage points and the
    scored component is ``+0.6``.

    This is the only sign flip in the pillar. A falling unemployment rate is a
    tightening labour market, which is currency-positive through the expected
    policy path.
    """
    built = run(pillar, healthy(), currencies=("USD",))

    assert built["USD"]["unemployment_6m"] == pytest.approx(0.6)
    assert built["USD"]["unemployment_chg_6m"] == pytest.approx(-0.6)


def test_a_rising_unemployment_rate_scores_negative(
    pillar: EmploymentPillar,
) -> None:
    """The same rule from the other side, so neither test passes on a constant.

    The rate runs 4.4 up to 5.0. Six months before 2026-09-01 is 2026-03-01,
    where the rate was 4.4, so the published change is ``5.0 - 4.4 = +0.6`` and
    the scored component is ``-0.6``.
    """
    rising = healthy(rate_values=(4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 5.0))

    built = run(pillar, rising, currencies=("USD",))

    assert built["USD"]["unemployment_6m"] == pytest.approx(-0.6)
    assert built["USD"]["unemployment_chg_6m"] == pytest.approx(0.6)


def test_the_two_unemployment_values_are_each_others_negation(
    pillar: EmploymentPillar,
) -> None:
    """The flip is applied once, to one of the two, and never downstream.

    Asserted as a relationship rather than as two numbers, so a change that
    flipped both, or neither, fails here even if the magnitudes stay right.
    """
    built = run(pillar, healthy(), currencies=("USD",))

    scored = built["USD"]["unemployment_6m"]
    published = built["USD"]["unemployment_chg_6m"]
    assert scored is not None and published is not None
    assert scored == pytest.approx(-published)
    assert scored != pytest.approx(0.0)


def test_the_published_change_reaches_raw_and_takes_no_sub_weight(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 3. ``raw`` shows what the statistics office published.

    Putting the flipped value on `PillarScore.raw` would print ``+0.5`` beside a
    currency whose unemployment rate fell 0.5 points, which reads as good news
    about a number that is good news, and ``+0.3`` beside one whose rate rose,
    which reads as good news about a bad number. The flip belongs to the score.
    """
    assert EmploymentPillar.headline_component == "unemployment_chg_6m"
    assert "unemployment_chg_6m" not in EmploymentPillar().component_weights

    # `raw` is only populated on a currency the pillar actually scored, and
    # `MIN_CROSS_SECTION` refuses a cross-section of one, so a single-currency
    # run would take the `missing_score` path and assert nothing about `raw`.
    observations = [
        *healthy("USD", rate_values=(5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *healthy("JPY", rate_values=(3.0, 3.0, 2.9, 2.9, 2.8, 2.8, 2.7)),
        *healthy("CAD", rate_values=(6.0, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6)),
    ]

    scores = pillar.compute(observations, ("USD", "JPY", "CAD"), ASOF)

    assert scores["USD"].raw == pytest.approx(-0.6)
    assert scores["CAD"].raw == pytest.approx(0.6)


def test_the_six_month_window_is_measured_in_months_not_observations(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 6, on a quarterly publisher, which is the case that separates them.

    CHF publishes the unemployment rate quarterly. The series here steps three
    months at a time and runs 5.0, 4.9, 4.8, 4.7 over the periods 2025-12-01,
    2026-03-01, 2026-06-01 and 2026-09-01. Six months back from the newest print
    is 2026-03-01 at 4.9, so the published change is ``4.7 - 4.9 = -0.2`` and the
    scored component is ``+0.2``.

    Counting six observations instead would reach 2024-03-01, which is eighteen
    months back and not in this series at all. Counting six observations on a
    longer series would measure eighteen months of a slow-moving rate and report
    a move three times this size with the same sign, which would pass every sign
    test in this file.
    """
    quarterly = healthy(
        "CHF",
        step_months=3,
        rate_values=(5.0, 4.9, 4.8, 4.7),
    )

    built = run(pillar, quarterly, currencies=("CHF",))

    assert built["CHF"]["unemployment_6m"] == pytest.approx(0.2)
    assert built["CHF"]["unemployment_chg_6m"] == pytest.approx(-0.2)


def test_a_series_too_short_for_the_window_is_absent_not_zero(
    pillar: EmploymentPillar,
) -> None:
    """A window that cannot be measured is refused rather than shortened.

    Three monthly prints cannot answer a six-month change. Measuring it over
    whatever is there would report a smaller move than the pillar claims to be
    reporting, and the error is one-sided: a short window always reads calmer.
    Zero is a real reading here, an unemployment rate that did not move, so it
    cannot double as the absence marker.
    """
    short = [
        *series(RATE, (5.0, 4.9, 4.8)),
        *series(CHG, (120.0,) * 7),
        *series(LEVEL, (150_000.0,) * 7),
    ]

    built = run(pillar, short, currencies=("USD",))

    assert built["USD"]["unemployment_6m"] is None
    assert built["USD"]["unemployment_chg_6m"] is None


# --- the hiring component and its denominator --------------------------------


def test_hiring_is_an_annualised_percent_of_the_level(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 4, worked by hand.

    Monthly, so the three months ending 2026-09-01 are three prints of 120
    thousand each, a 360 thousand total. Annualised that is ``360 * 12 / 3 =
    1440`` thousand against a level of 150,000 thousand, so
    ``1440 / 150000 * 100 = 0.96`` percent a year.

    The count and the level are both in thousands because both come from the
    same published series, so the scale cancels and the component is immune to
    the thousands-against-persons mismatch that the US ref carries.
    """
    built = run(pillar, healthy(), currencies=("USD",))

    assert built["USD"]["employment_trend"] == pytest.approx(0.96)


def test_shedding_jobs_scores_negative(pillar: EmploymentPillar) -> None:
    """The sign rule on the second component, asserted in its own right.

    The same arithmetic with the flow negative: three prints of -120 thousand
    give ``-1440 / 150000 * 100 = -0.96``. No flip is applied here. A shrinking
    workforce is currency-negative directly, which is why only the unemployment
    component needs inverting.
    """
    shedding = healthy(chg_value=-120.0)

    built = run(pillar, shedding, currencies=("USD",))

    assert built["USD"]["employment_trend"] == pytest.approx(-0.96)


def test_a_quarterly_flow_is_annualised_on_its_own_cadence(
    pillar: EmploymentPillar,
) -> None:
    """The same three-month window, on a publisher that prints once a quarter.

    One quarterly print of 30,000 already covers three months, so the total over
    the window is 30,000 and the annualised figure is ``30000 * 12 / 3 = 120000``
    against a level of 2,905,000, which is ``4.13`` percent a year.

    Summing three quarterly prints instead, which is what a window counted in
    observations would do, would report three times that. Dividing the quarterly
    print by three first and then annualising over one month would report a
    third of it.
    """
    quarterly = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7), currency="NZD", step_months=3),
        *series(CHG, (30_000.0,) * 4, currency="NZD", step_months=3),
        *series(LEVEL, (2_905_000.0,) * 4, currency="NZD", step_months=3),
    ]

    built = run(pillar, quarterly, currencies=("NZD",))

    expected = 30_000.0 * MONTHS_PER_YEAR / HIRING_WINDOW_MONTHS / 2_905_000.0 * 100.0
    assert expected == pytest.approx(4.1308, abs=5e-5)
    assert built["NZD"]["employment_trend"] == pytest.approx(expected)


def test_each_currency_is_divided_by_its_own_level(
    pillar: EmploymentPillar,
) -> None:
    """The denominator is per currency, which is the whole point of having one.

    Two currencies hire the same number of people against workforces that differ
    by a factor of ten. The pillar must say the smaller one is hiring ten times
    as fast. Reading one currency's level for everybody, which is the mistake a
    shared lookup makes, would rank them equal and would do it silently.
    """
    both = [
        *healthy("USD", level_value=150_000.0),
        *healthy("NZD", level_value=15_000.0),
    ]

    built = run(pillar, both, currencies=("USD", "NZD"))

    small = built["NZD"]["employment_trend"]
    large = built["USD"]["employment_trend"]
    assert small is not None and large is not None
    assert small == pytest.approx(large * 10.0)


def test_a_missing_level_leaves_hiring_absent_rather_than_a_raw_count(
    pillar: EmploymentPillar,
) -> None:
    """Criterion: no substitute for a missing denominator.

    This is EUR's case in the live registry. Scoring the raw count instead would
    rank the cross-section by country size, which section 3.4 rejects and which
    would look entirely reasonable in a report.
    """
    without_level = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *series(CHG, (120.0,) * 7),
    ]

    built = run(pillar, without_level, currencies=("USD",))

    assert built["USD"]["employment_trend"] is None
    assert built["USD"]["unemployment_6m"] == pytest.approx(0.6)


def test_a_level_of_zero_is_refused_rather_than_divided_by(
    pillar: EmploymentPillar,
) -> None:
    """A workforce cannot be zero, so reaching it means the series is not what it says.

    Dividing by it would raise, and guarding it with a fallback level would
    answer a number built on nonsense. Refusing is the only honest answer.
    """
    broken = healthy(level_value=0.0)

    built = run(pillar, broken, currencies=("USD",))

    assert built["USD"]["employment_trend"] is None


def test_an_incomplete_hiring_window_is_refused(pillar: EmploymentPillar) -> None:
    """Two monthly prints cannot answer a three-month total.

    Summing them anyway would report two thirds of the hiring that happened and
    would do it as a confident number. The shortfall is always in the same
    direction, so it would not look like noise.
    """
    gappy = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *series(CHG, (120.0, 120.0)),
        *series(LEVEL, (150_000.0, 150_000.0)),
    ]

    built = run(pillar, gappy, currencies=("USD",))

    assert built["USD"]["employment_trend"] is None


# --- the floor, which for this pillar has no partial state -------------------


def test_a_currency_with_only_the_rate_is_scored_absent(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 5, and the case #15 exists for.

    Both components carry 0.50, so one missing leaves exactly
    `MIN_COMPONENT_WEIGHT`, and the comparison is "at or below". The currency is
    absent rather than scored on the unemployment rate alone, which is the state
    the pillar's own docstring calls unsafe: a rate can fall because people
    stopped looking for work, and the hiring series is what tells the two apart.
    """
    assert pillar.component_weights["unemployment_6m"] == MIN_COMPONENT_WEIGHT

    # A full cross-section, with only GBP deficient. Scoring one currency on its
    # own would come back absent whatever the floor did, because
    # `MIN_CROSS_SECTION` refuses a cross-section of one, and this test would
    # then pass against a pillar with no floor at all.
    observations = [
        *healthy("USD", rate_values=(5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *healthy("JPY", rate_values=(3.0, 3.0, 2.9, 2.9, 2.8, 2.8, 2.7)),
        *healthy("CAD", rate_values=(6.0, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6)),
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4), currency="GBP"),
    ]

    scores = pillar.compute(observations, ("USD", "JPY", "CAD", "GBP"), ASOF)

    assert scores["GBP"].z is None
    assert scores["GBP"].score == pytest.approx(0.0)
    assert scores["USD"].z is not None


def test_a_currency_with_only_hiring_is_scored_absent(
    pillar: EmploymentPillar,
) -> None:
    """The other half of the same floor, so neither side is assumed.

    Symmetry is not obvious here: the two components reach the blend by
    different routes, one through a subtraction and one through a division.
    """
    observations = [
        *healthy("USD", rate_values=(5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *healthy("JPY", rate_values=(3.0, 3.0, 2.9, 2.9, 2.8, 2.8, 2.7)),
        *healthy("CAD", rate_values=(6.0, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6)),
        *series(CHG, (120.0,) * 7, currency="GBP"),
        *series(LEVEL, (150_000.0,) * 7, currency="GBP"),
    ]

    scores = pillar.compute(observations, ("USD", "JPY", "CAD", "GBP"), ASOF)

    assert scores["GBP"].z is None
    assert scores["USD"].z is not None


def test_a_currency_with_neither_series_substitutes_nothing(
    pillar: EmploymentPillar,
) -> None:
    """Criterion 7. Both components ``None``, and the pillar absent.

    Zero is a reading for both of these: an unemployment rate that did not move
    and a workforce that neither grew nor shrank. Neither may stand in for an
    outage.
    """
    built = run(pillar, [], currencies=("USD",))

    assert built["USD"]["unemployment_6m"] is None
    assert built["USD"]["employment_trend"] is None
    assert built["USD"]["unemployment_chg_6m"] is None

    scores = pillar.compute([], sorted(G10), ASOF)

    assert set(scores) == set(G10)
    for currency in G10:
        assert scores[currency].z is None, currency


def test_a_currency_with_both_components_scores(pillar: EmploymentPillar) -> None:
    """The floor must not be so eager that nothing ever scores.

    Without this the absence tests above would all pass on a pillar that never
    returns anything, which is the shape a vacuous suite takes here.
    """
    both = [
        *healthy("USD", chg_value=120.0),
        *healthy("JPY", chg_value=20.0),
        *healthy("CAD", chg_value=-40.0),
    ]

    scores = pillar.compute(both, ("USD", "JPY", "CAD"), ASOF)

    for currency in ("USD", "JPY", "CAD"):
        assert scores[currency].z is not None, currency


def test_the_cross_section_ranks_hiring_and_not_workforce_size(
    pillar: EmploymentPillar,
) -> None:
    """The failure section 3.4 names, asserted on the pillar's output.

    A large economy adding 0.4% a year and a small one adding 1.2% a year. The
    small one must score higher. Under a raw count the large one wins by two
    orders of magnitude, which is a stable, plausible and entirely wrong
    ranking.
    """
    mixed = [
        *healthy("USD", chg_value=50.0, level_value=150_000.0),
        *healthy("NZD", chg_value=30.0, level_value=30_000.0),
        *healthy("JPY", chg_value=10.0, level_value=68_000.0),
    ]

    scores = pillar.compute(mixed, ("USD", "NZD", "JPY"), ASOF)

    small = scores["NZD"].score
    large = scores["USD"].score
    assert small > large


def test_the_window_constants_are_read_and_not_retyped(
    pillar: EmploymentPillar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override the horizon and the answer has to move, or the constant is decoration.

    The rate series steps down 0.1 a month, so a three-month window reads -0.3
    and a six-month window reads -0.6. A hardcoded 6 passes every other test in
    this file.
    """
    before = run(pillar, healthy(), currencies=("USD",))

    monkeypatch.setattr("fbe.pillars.employment.UNEMPLOYMENT_WINDOW_MONTHS", 3)
    after = run(pillar, healthy(), currencies=("USD",))

    assert before["USD"]["unemployment_6m"] == pytest.approx(0.6)
    assert after["USD"]["unemployment_6m"] == pytest.approx(0.3)


def test_no_component_is_ever_a_substituted_zero(pillar: EmploymentPillar) -> None:
    """The pillar-wide version of the rule, swept rather than spot-checked.

    Every absence route in this file returns ``None``. A future change that
    answers ``0.0`` on any of them would put the affected currency in the middle
    of the cross-section on the strength of having no data, and would pass a
    test that only checked the value was not wrong.
    """
    routes = {
        "nothing at all": [],
        "rate only": list(series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4))),
        "flow with no level": [
            *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
            *series(CHG, (120.0,) * 7),
        ],
        "rate too short": [
            *series(RATE, (5.0, 4.9)),
            *series(CHG, (120.0,) * 7),
            *series(LEVEL, (150_000.0,) * 7),
        ],
    }

    for name, observations in routes.items():
        built = run(pillar, observations, currencies=("USD",))
        for component, value in built["USD"].items():
            assert value is None or value != 0.0, f"{name}: {component}"


# --- what the review pass found these could not see --------------------------


def test_a_hole_where_the_window_opens_is_refused(pillar: EmploymentPillar) -> None:
    """The six-month match is exact, never the nearest older print.

    2026-03-01 is missing here and 2026-02-01 is present. A nearest match would
    difference 2026-09-01 against 2026-02-01, report a seven-month move as a
    six-month one, and do it with the same sign and a plausible magnitude.
    `_change_over_months` argues for the exact match in its docstring and every
    other fixture in this file is contiguous, so ``==`` could become ``>=`` and
    nothing would notice.
    """
    holed = [
        obs(RATE, 5.4, date(2026, 2, 1)),
        *[
            obs(RATE, value, date(2026, month, 1))
            for value, month in ((4.9, 4), (4.8, 5), (4.7, 6), (4.6, 7), (4.5, 8))
        ],
        obs(RATE, 4.4, date(2026, 9, 1)),
        *series(CHG, (120.0,) * 7),
        *series(LEVEL, (150_000.0,) * 7),
    ]

    built = run(pillar, holed, currencies=("USD",))

    assert built["USD"]["unemployment_6m"] is None
    assert built["USD"]["unemployment_chg_6m"] is None


def test_hiring_is_divided_by_the_newest_level(pillar: EmploymentPillar) -> None:
    """The workforce moves, so which level print is read changes the answer.

    Every other fixture here holds the level flat, which makes the newest print,
    the oldest and everything between the same number. This workforce grows
    100,000 to 125,000 to 150,000 and the three readings are 0.96, 1.152 and
    1.44. Reading the oldest overstates hiring by half, in the same direction
    for every growing economy, and looks entirely reasonable in a report.
    """
    moving = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *series(CHG, (120.0,) * 4),
        *series(LEVEL, (90_000.0, 100_000.0, 125_000.0, 150_000.0)),
    ]

    built = run(pillar, moving, currencies=("USD",))

    assert built["USD"]["employment_trend"] == pytest.approx(0.96)


def test_the_window_is_summed_rather_than_scaled_from_one_print(
    pillar: EmploymentPillar,
) -> None:
    """Every other flow fixture is constant, so a sum and a scaled print agree.

    The three months in the window are 60, 120 and 180 thousand, totalling the
    same 360 the constant fixture produces, so the published 0.96 still holds.
    But ``newest * 3`` gives 1.44 and ``oldest * 3`` gives 0.48, and summing all
    seven prints, including the 999s that sit outside the window, gives 11.62.
    Real payrolls prints are never equal three months running, so a constant
    fixture cannot tell summing the window apart from reading one print of it.
    """
    varying = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *series(CHG, (999.0, 999.0, 999.0, 999.0, 60.0, 120.0, 180.0)),
        *series(LEVEL, (150_000.0,) * 7),
    ]

    built = run(pillar, varying, currencies=("USD",))

    assert built["USD"]["employment_trend"] == pytest.approx(0.96)


def test_the_hiring_window_is_read_and_not_retyped(
    pillar: EmploymentPillar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override the hiring horizon and the answer has to move.

    It only moves on a flow that varies. With a constant flow the annualised
    rate is invariant to the window width by construction, three prints of 120
    and six prints of 120 both annualising to the same number, so the obvious
    version of this test would itself be vacuous.

    Six months of 60, 60, 60, 60, 120, 180 total 540, so the six-month reading
    is ``540 * 12 / 6 / 150000 * 100 = 0.72``. The three-month reading over 60,
    120, 180 is ``360 * 12 / 3 / 150000 * 100 = 0.96``.
    """
    varying = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *series(CHG, (0.0, 60.0, 60.0, 60.0, 60.0, 120.0, 180.0)),
        *series(LEVEL, (150_000.0,) * 7),
    ]

    before = run(pillar, varying, currencies=("USD",))
    monkeypatch.setattr("fbe.pillars.employment.HIRING_WINDOW_MONTHS", 6)
    after = run(pillar, varying, currencies=("USD",))

    assert before["USD"]["employment_trend"] == pytest.approx(0.96)
    assert after["USD"]["employment_trend"] == pytest.approx(0.72)


def test_the_annualisation_is_read_and_not_retyped(
    pillar: EmploymentPillar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A literal ``4`` equals ``MONTHS_PER_YEAR / HIRING_WINDOW_MONTHS`` today.

    It would pass every other test in this file and would part company with the
    constants the day anyone changes the hiring horizon, which is the
    config-drift shape the standards call out. Halving the year halves the
    answer.
    """
    before = run(pillar, healthy(), currencies=("USD",))
    monkeypatch.setattr("fbe.pillars.employment.MONTHS_PER_YEAR", 6)
    after = run(pillar, healthy(), currencies=("USD",))

    assert before["USD"]["employment_trend"] == pytest.approx(0.96)
    assert after["USD"]["employment_trend"] == pytest.approx(0.48)


def test_a_negative_level_is_refused_rather_than_divided_by(
    pillar: EmploymentPillar,
) -> None:
    """Zero was covered and a negative level is the more dangerous half.

    Dividing by zero raises, so even an unguarded implementation fails loudly
    there. A negative level divides cleanly and returns a confident negative
    hiring rate, so narrowing the guard from ``<= 0.0`` to ``== 0.0`` would pass
    the zero test and ship the plausible wrong number.
    """
    broken = healthy(level_value=-150_000.0)

    built = run(pillar, broken, currencies=("USD",))

    assert built["USD"]["employment_trend"] is None


def test_both_windows_hold_on_a_series_spanning_several_years(
    pillar: EmploymentPillar,
) -> None:
    """Every other fixture here is seven prints long and real series are not.

    `_extract` returns the whole visible history, so a live run hands these
    windows decades of monthly payrolls. A `_months_between` that lost its year
    term would agree with every short fixture in this file and would sweep the
    twelve- and twenty-four-month-old prints into a three-month window here.

    The rate falls 0.05 a month over 33 prints, so the six-month change is
    ``-0.30``. The flow is 999 for the first thirty months and 120 for the last
    three, which still total 360 and still annualise to 0.96.
    """
    rate = tuple(5.0 - 0.05 * step for step in range(33))
    flow = (999.0,) * 30 + (120.0, 120.0, 120.0)
    long_run = [
        *series(RATE, rate),
        *series(CHG, flow),
        *series(LEVEL, (150_000.0,) * 33),
    ]

    built = run(pillar, long_run, currencies=("USD",))

    assert built["USD"]["unemployment_6m"] == pytest.approx(0.30)
    assert built["USD"]["employment_trend"] == pytest.approx(0.96)


def test_a_mixed_cadence_currency_reads_each_series_on_its_own(
    pillar: EmploymentPillar,
) -> None:
    """GBP's real shape: a monthly rate against a quarterly hiring series.

    Every other quarterly fixture here makes all three series quarterly, which
    is a shape no currency in the registry has. GBP is the one where the two
    components age on different cadences, so the step has to be measured per
    series rather than assumed per currency.

    The rate is monthly and falls 0.1 a month over seven prints, so six months
    back is 5.0 and the change is ``-0.6``, scoring ``+0.6``. The flow is one
    quarterly print of 30,000 against a workforce of 34,469,000, which is
    ``30000 * 12 / 3 / 34469000 * 100``.
    """
    mixed = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4), currency="GBP"),
        *series(CHG, (30_000.0,) * 3, currency="GBP", step_months=3),
        *series(LEVEL, (34_469_000.0,) * 3, currency="GBP", step_months=3),
    ]

    built = run(pillar, mixed, currencies=("GBP",))

    expected = 30_000.0 * MONTHS_PER_YEAR / HIRING_WINDOW_MONTHS / 34_469_000.0 * 100.0
    assert expected == pytest.approx(0.348139, abs=1e-6)
    assert built["GBP"]["unemployment_6m"] == pytest.approx(0.6)
    assert built["GBP"]["employment_trend"] == pytest.approx(expected)


def test_the_level_table_is_filtered_on_the_transform_not_the_currency() -> None:
    """EUR is the only non-``diff`` flow today, so the two filters agree today.

    `_employment_level_series` says it filters on the transform so that a EUR
    flow sourced from a published level would gain its stock without anyone
    remembering to add it. Replacing that with ``code != "EUR"`` passes the
    whole suite. The filters part company the moment a second manual flow
    exists, and then a currency-code filter would hand it a level derived from a
    hand-keyed number, so `employment_trend` would divide a hand-keyed flow by a
    hand-keyed flow.
    """
    from dataclasses import replace

    from fbe.datasources import registry

    flow = INDICATORS[CHG]
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(
            registry,
            "EMPLOYMENT_CHG",
            replace(flow, series={**flow.series, "SEK": flow.series["EUR"]}),
        )
        derived = registry._employment_level_series()

    assert "EUR" not in derived
    assert "SEK" not in derived
    assert set(derived) == {"USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"}


def test_moving_one_currency_moves_a_currency_that_did_not_move(
    pillar: EmploymentPillar,
) -> None:
    """Normalisation is cross-sectional, so no score here is a per-currency answer.

    JPY's three series are identical in both runs. Only CAD's unemployment rate
    changes, and JPY's score has to move anyway, because the yardstick it is
    measured against moved. Nothing else in this file asserts that coupling, so
    a `_normalise` override scoring each currency against its own history alone
    would pass everything and quietly change what the pillar means.

    ``raw`` must not move, because it is the published number rather than the
    standardised one, and asserting both together is what separates the two.
    """

    def universe(cad_rate: Sequence[float]) -> list[Observation]:
        return [
            *healthy("USD", rate_values=(5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
            *healthy("JPY", rate_values=(3.0, 3.0, 2.9, 2.9, 2.8, 2.8, 2.7)),
            *healthy("CAD", rate_values=cad_rate),
        ]

    wanted = ("USD", "JPY", "CAD")
    before = pillar.compute(universe((6.0, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6)), wanted, ASOF)
    after = pillar.compute(universe((6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0)), wanted, ASOF)

    assert before["JPY"].raw == pytest.approx(after["JPY"].raw)
    assert before["JPY"].score != pytest.approx(after["JPY"].score)


def test_the_spec_row_names_the_keys_the_pillar_actually_reads() -> None:
    """Section 3.4's sub-indicator table against `component_indicators`.

    `tests/test_worked_example.py` pins section 7's transcription of the
    sub-weights, and `tests/test_spec_thresholds.py` refuses bare numbers in
    sections 4 to 6. Neither covers the key names in the 3.4 table, and this
    change edited that row: the momentum component now reads two keys rather
    than one. A spec naming a key the pillar does not read sends the next reader
    to the wrong series, which for this component is the difference between a
    hiring rate and a headcount.
    """
    labels = {
        "| Unemployment rate, 6-month change |": "unemployment_6m",
        "| Employment change momentum |": "employment_trend",
    }

    found: dict[str, set[str]] = {}
    for line in SPEC.read_text().splitlines():
        for prefix, component in labels.items():
            if line.startswith(prefix):
                cell = line.split("|")[2]
                found[component] = {
                    key.strip() for key in cell.replace("`", "").split(",")
                }

    assert set(found) == set(labels.values()), found
    for component, keys in found.items():
        assert keys == set(EmploymentPillar.component_indicators[component]), component


def test_a_flow_print_spanning_a_hole_is_refused(pillar: EmploymentPillar) -> None:
    """Counting the prints in the window is not enough for a ``diff`` series.

    ``employment_chg`` carries the change since the previous observation, so
    each value's span is whatever gap precedes it. The periods here are
    2026-01-01 then 2026-05-01, 2026-06-01 and 2026-07-01: three prints inside
    a three-month window, and a step of one month read off the newest two, so a
    count alone passes. But the 2026-05-01 value is a four-month change.

    Summing it would report 50 + 50 + 200 against a true 150, which is double
    the hiring, well inside the clip band, and biased in whichever direction the
    labour market moved during the hole. The pillar refuses instead, because the
    print before the window is not one step before the oldest print in it.
    """
    holed = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4), end=date(2026, 7, 1)),
        obs(CHG, 50.0, date(2026, 1, 1)),
        obs(CHG, 200.0, date(2026, 5, 1)),
        obs(CHG, 50.0, date(2026, 6, 1)),
        obs(CHG, 50.0, date(2026, 7, 1)),
        *series(LEVEL, (20_000_000.0,) * 4, end=date(2026, 7, 1)),
    ]

    built = run(pillar, holed, currencies=("USD",))

    assert built["USD"]["employment_trend"] is None


def test_a_flow_that_begins_at_the_window_is_refused(
    pillar: EmploymentPillar,
) -> None:
    """The oldest print in the window needs a predecessor to be checkable.

    With exactly three monthly prints and nothing before them, there is no way
    to tell whether the oldest one measures one month or ten years, because a
    ``diff`` value's span is the gap to the observation before it and there is
    no observation before it. Refusing is the only honest answer; assuming one
    month would be the plausible default this codebase refuses everywhere.
    """
    from_scratch = [
        *series(RATE, (5.0, 4.9, 4.8, 4.7, 4.6, 4.5, 4.4)),
        *series(CHG, (120.0,) * 3),
        *series(LEVEL, (150_000.0,) * 3),
    ]

    built = run(pillar, from_scratch, currencies=("USD",))

    assert built["USD"]["employment_trend"] is None
