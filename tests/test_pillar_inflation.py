"""Tests for INFLATION's two pillar-owned methods, and the lifted helpers.

This pillar never scores the level of inflation. It scores the gap to each
central bank's own target, and the gap is the only one of the two readings that
means the same thing in eight economies. A 3.0% print is 1.0 point above target
in six of them, 0.5 above in Australia against a 2-3% band midpoint, and 2.0
above in Switzerland. So most of the `_transform` cases here use AUD and CHF:
a pillar that hardcoded 2.0, or that read the level and forgot the target
entirely, passes every test built on a currency whose target happens to be 2.0.

`_extract`'s difficulty is the visibility rule, and after this change it is not
this pillar's rule to get right. `_visible` and `_newest_vintages` moved to
`BasePillar`, so the tests here assert that this pillar reaches the shared
helpers rather than reimplementing them, and the rule itself stays tested where
it lives.

`_transform`'s difficulty is absence. A component a currency cannot build is
`None`, never `0.0`: a zero gap reads as inflation exactly at target, which is
a specific and confident claim about a currency the model has no print for.

Nothing here reaches the network. Every value that reaches an assertion is
built in the test that uses it, so every gap is checkable by hand against
numbers written in this file. The registry is read for two things only, an
indicator's unit and its frequency, so a fixture carries the real ones rather
than a plausible-looking stand-in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from math import fsum

import pytest

from fbe.datasources.registry import INDICATORS
from fbe.pillars.base import MIN_COMPONENT_WEIGHT
from fbe.pillars.inflation import InflationPillar
from fbe.types import Frequency, Observation
from fbe.universe import G10, meta

ASOF = date(2026, 9, 15)

EXPECTED_REQUIRES = ("cpi_yoy", "core_cpi_yoy")
"""Spelled out rather than read off the pillar, so a key silently dropped from
`requires` fails here rather than quietly shrinking what these tests cover."""

UNITS = {key: INDICATORS[key].unit for key in EXPECTED_REQUIRES}
FREQUENCIES = {key: INDICATORS[key].frequency for key in EXPECTED_REQUIRES}


@pytest.fixture
def pillar() -> InflationPillar:
    return InflationPillar()


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


PERIOD = date(2026, 8, 1)


def prints(currency: str, headline: float, core: float) -> list[Observation]:
    """One visible headline and one visible core print for a currency."""
    return [
        obs("cpi_yoy", currency, headline, PERIOD, released_at=published(PERIOD)),
        obs("core_cpi_yoy", currency, core, PERIOD, released_at=published(PERIOD)),
    ]


# --- what _extract selects ---------------------------------------------------


def test_the_pillar_asks_for_the_two_series_it_blends(pillar: InflationPillar) -> None:
    """Guards every test below, which would narrow silently with `requires`."""
    assert tuple(pillar.requires) == EXPECTED_REQUIRES


def test_every_required_key_is_present_for_every_currency(
    pillar: InflationPillar,
) -> None:
    """A dropped key and an empty sequence read the same and are different facts.

    `BasePillar._extract` documents the shape: every currency asked for is a
    key, and every key in `requires` is present under it, empty where that
    currency has nothing.
    """
    extracted = pillar._extract(prints("USD", 3.0, 2.8), ("USD", "JPY"), ASOF)

    assert set(extracted) == {"USD", "JPY"}
    for currency in ("USD", "JPY"):
        assert set(extracted[currency]) == set(EXPECTED_REQUIRES)
    assert extracted["JPY"]["cpi_yoy"] == ()
    assert extracted["JPY"]["core_cpi_yoy"] == ()


def test_an_observation_not_yet_published_takes_no_part(
    pillar: InflationPillar,
) -> None:
    """The rule the whole visibility machinery exists for.

    The period precedes the run and the release follows it, which is the shape
    of every lagged macro series: August CPI describes August and prints in
    September. A run on 15 September that filtered on period would score itself
    with a number that does not exist for another five days, and the error is
    systematic rather than occasional because every series here is lagged.
    """
    future_print = obs(
        "cpi_yoy",
        "USD",
        9.9,
        date(2026, 9, 1),
        released_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )
    visible_print = obs("cpi_yoy", "USD", 3.0, PERIOD, released_at=published(PERIOD))

    extracted = pillar._extract([future_print, visible_print], ("USD",), ASOF)

    assert [entry.value for entry in extracted["USD"]["cpi_yoy"]] == [3.0]


def test_the_newest_vintage_visible_at_asof_wins(pillar: InflationPillar) -> None:
    """Not the newest vintage outright, which is the same mistake in other clothes."""
    first = obs("cpi_yoy", "USD", 3.0, PERIOD, released_at=published(PERIOD))
    revised = obs(
        "cpi_yoy", "USD", 3.2, PERIOD, released_at=published(PERIOD, 25), revision=1
    )
    later = obs(
        "cpi_yoy",
        "USD",
        9.9,
        PERIOD,
        released_at=datetime(2026, 12, 1, 12, 0, tzinfo=UTC),
        revision=2,
    )

    extracted = pillar._extract([first, revised, later], ("USD",), ASOF)

    assert [entry.value for entry in extracted["USD"]["cpi_yoy"]] == [3.2]


def test_the_pillar_uses_the_shared_helpers_and_defines_none_of_its_own(
    pillar: InflationPillar,
) -> None:
    """The point of the lift, asserted rather than left to a reviewer's eye.

    The rule is the same for every pillar, and a second copy of it is a second
    place for #121 to be fixed and missed. These attributes have to resolve to
    `BasePillar`'s, not to something defined in `fbe.pillars.inflation`.
    """
    import fbe.pillars.base as base_module
    import fbe.pillars.inflation as inflation_module

    assert "_visible" not in vars(InflationPillar)
    assert "_newest_vintages" not in vars(InflationPillar)
    assert not hasattr(inflation_module, "_vintage_key")
    assert type(pillar)._visible is base_module.BasePillar._visible
    assert type(pillar)._newest_vintages is base_module.BasePillar._newest_vintages


def test_observations_come_back_sorted_by_period(pillar: InflationPillar) -> None:
    """Ascending, because every consumer downstream takes the last as newest."""
    older = date(2026, 6, 1)
    middle = date(2026, 7, 1)
    supplied = [
        obs("cpi_yoy", "USD", 3.0, PERIOD, released_at=published(PERIOD)),
        obs("cpi_yoy", "USD", 2.6, older, released_at=published(older)),
        obs("cpi_yoy", "USD", 2.8, middle, released_at=published(middle)),
    ]

    extracted = pillar._extract(supplied, ("USD",), ASOF)

    assert [entry.period for entry in extracted["USD"]["cpi_yoy"]] == [
        older,
        middle,
        PERIOD,
    ]


def test_another_currencys_prints_do_not_leak(pillar: InflationPillar) -> None:
    """Each currency is scored against its own bank, so a leak inverts a sign."""
    supplied = prints("USD", 3.0, 2.8) + prints("CHF", 1.4, 1.2)

    extracted = pillar._extract(supplied, ("USD", "CHF"), ASOF)

    assert [entry.value for entry in extracted["USD"]["cpi_yoy"]] == [3.0]
    assert [entry.value for entry in extracted["CHF"]["cpi_yoy"]] == [1.4]


def test_an_indicator_this_pillar_does_not_ask_for_is_ignored(
    pillar: InflationPillar,
) -> None:
    """`policy_rate` belongs to MONETARY and must not reach this pillar's blend."""
    stray = Observation(
        indicator="policy_rate",
        currency="USD",
        value=4.25,
        period=PERIOD,
        source="test",
        series_id="policy_rate",
        unit="percent",
        frequency=Frequency.DAILY,
        released_at=published(PERIOD),
    )

    extracted = pillar._extract([*prints("USD", 3.0, 2.8), stray], ("USD",), ASOF)

    assert set(extracted["USD"]) == set(EXPECTED_REQUIRES)


# --- what _transform computes ------------------------------------------------


def gaps(
    pillar: InflationPillar,
    currency: str,
    headline: float,
    core: float,
) -> dict[str, float | None]:
    """Run the two prints through both methods and return one currency's components."""
    extracted = pillar._extract(prints(currency, headline, core), (currency,), ASOF)
    return dict(pillar._transform(extracted, ASOF)[currency])


def test_the_gap_is_measured_against_australias_own_target(
    pillar: InflationPillar,
) -> None:
    """2.5, the midpoint of the RBA's 2-3% band, and not 2.0.

    Computed here rather than read back: 3.0 less 2.5 is 0.5, and 2.8 less 2.5
    is 0.3. A pillar hardcoding 2.0 gives 1.0 and 0.8 and passes every test
    built on a currency whose target really is 2.0.
    """
    assert meta("AUD").inflation_target == 2.5

    built = gaps(pillar, "AUD", 3.0, 2.8)

    assert built["cpi_gap"] == pytest.approx(0.5)
    assert built["core_gap"] == pytest.approx(0.3)


def test_the_gap_is_measured_against_switzerlands_own_target(
    pillar: InflationPillar,
) -> None:
    """1.0, because the SNB aims below 2%. The same print is a different problem.

    3.0 less 1.0 is 2.0, against the 1.0 a hardcoded target would give. This is
    the currency where the level and the gap disagree most, which is the whole
    argument for the pillar being built this way.
    """
    assert meta("CHF").inflation_target == 1.0

    built = gaps(pillar, "CHF", 3.0, 2.8)

    assert built["cpi_gap"] == pytest.approx(2.0)
    assert built["core_gap"] == pytest.approx(1.8)


def test_the_same_print_gives_three_different_gaps(pillar: InflationPillar) -> None:
    """The sentence the module docstring opens with, as an assertion.

    One 3.0% print is 1.0 above target in the United States, 0.5 in Australia
    and 2.0 in Switzerland. If these three ever agree, the pillar has started
    scoring the level.
    """
    built = {
        currency: gaps(pillar, currency, 3.0, 3.0)["cpi_gap"]
        for currency in ("USD", "AUD", "CHF")
    }

    assert built["USD"] == pytest.approx(1.0)
    assert built["AUD"] == pytest.approx(0.5)
    assert built["CHF"] == pytest.approx(2.0)


def test_above_target_is_positive_and_below_target_is_negative(
    pillar: InflationPillar,
) -> None:
    """The sign rule, on both components, for a currency on each side of it.

    Above target obliges the bank to hold or tighten, which the front end
    prices, so an overshoot is currency-positive. An inverted pillar would flip
    the sign of 0.15 of every composite.
    """
    hot = gaps(pillar, "USD", 3.4, 3.1)
    cold = gaps(pillar, "USD", 1.1, 1.4)

    assert hot["cpi_gap"] == pytest.approx(1.4)
    assert hot["core_gap"] == pytest.approx(1.1)
    assert cold["cpi_gap"] == pytest.approx(-0.9)
    assert cold["core_gap"] == pytest.approx(-0.6)


def test_a_print_exactly_at_target_is_zero_and_not_absent(
    pillar: InflationPillar,
) -> None:
    """A real zero and a missing component are different facts with one shape.

    This is the case that makes `None` load-bearing everywhere else in the file:
    0.0 here means the bank is exactly on target, which is a finding.
    """
    built = gaps(pillar, "USD", 2.0, 2.0)

    assert built["cpi_gap"] == 0.0
    assert built["core_gap"] == 0.0


def test_nothing_but_the_target_is_subtracted(pillar: InflationPillar) -> None:
    """A plain difference, so `cpi_gap` can serve as the reported `raw`.

    Any normalisation, clipping or scaling applied here would reach a report as
    a number labelled percentage points that is not in percentage points.
    """
    built = gaps(pillar, "USD", 7.5, 6.25)

    assert built["cpi_gap"] == pytest.approx(5.5)
    assert built["core_gap"] == pytest.approx(4.25)


def test_a_currency_with_no_prints_at_all_produces_two_absences(
    pillar: InflationPillar,
) -> None:
    """Never a substituted or carried-forward value, and never a zero."""
    extracted = pillar._extract(prints("USD", 3.0, 2.8), ("USD", "JPY"), ASOF)

    built = pillar._transform(extracted, ASOF)

    assert built["JPY"]["cpi_gap"] is None
    assert built["JPY"]["core_gap"] is None


def test_a_currency_missing_core_still_reports_its_headline_gap(
    pillar: InflationPillar,
) -> None:
    """`_transform` reports what it has. The blend decides whether that is enough."""
    supplied = [obs("cpi_yoy", "USD", 3.0, PERIOD, released_at=published(PERIOD))]

    extracted = pillar._extract(supplied, ("USD",), ASOF)
    built = pillar._transform(extracted, ASOF)

    assert built["USD"]["cpi_gap"] == pytest.approx(1.0)
    assert built["USD"]["core_gap"] is None


def test_a_currency_outside_the_universe_raises(pillar: InflationPillar) -> None:
    """Rather than defaulting to 2%, which is right for six of the eight.

    A default here is the shape this repository refuses everywhere: it would be
    correct often enough never to be noticed, and wrong for exactly the two
    currencies whose targets make this pillar worth building.
    """
    supplied = prints("ZAR", 5.0, 4.6)
    extracted = {
        "ZAR": {
            "cpi_yoy": tuple(
                entry for entry in supplied if entry.indicator == "cpi_yoy"
            ),
            "core_cpi_yoy": tuple(
                entry for entry in supplied if entry.indicator == "core_cpi_yoy"
            ),
        }
    }

    with pytest.raises(KeyError, match="ZAR"):
        pillar._transform(extracted, ASOF)


def test_the_newest_period_is_the_one_differenced(pillar: InflationPillar) -> None:
    """Three months of prints, and the gap is built from the last of them."""
    older = date(2026, 6, 1)
    middle = date(2026, 7, 1)
    supplied = [
        obs("cpi_yoy", "USD", 4.4, older, released_at=published(older)),
        obs("cpi_yoy", "USD", 3.7, middle, released_at=published(middle)),
        obs("cpi_yoy", "USD", 3.0, PERIOD, released_at=published(PERIOD)),
        obs("core_cpi_yoy", "USD", 2.8, PERIOD, released_at=published(PERIOD)),
    ]

    extracted = pillar._extract(supplied, ("USD",), ASOF)
    built = pillar._transform(extracted, ASOF)

    assert built["USD"]["cpi_gap"] == pytest.approx(1.0)


# --- what reaches a score ----------------------------------------------------


def universe_observations(headline: float, core: float) -> list[Observation]:
    """One headline and one core print for every G10 currency."""
    supplied: list[Observation] = []
    for index, currency in enumerate(sorted(G10)):
        supplied += prints(currency, headline + index * 0.1, core + index * 0.1)
    return supplied


def test_the_headline_gap_reaches_raw_in_percentage_points(
    pillar: InflationPillar,
) -> None:
    """`raw` is the number a reader checks the pillar with.

    It is `cpi_gap`, in percentage points, not the z-score and not the level.
    AUD is used because its target is 2.5, so a `raw` carrying the level or a
    gap against 2.0 is visibly wrong rather than coincidentally right.
    """
    scores = pillar.compute(universe_observations(3.0, 2.8), sorted(G10), ASOF)

    aud = scores["AUD"]
    index = sorted(G10).index("AUD")
    assert aud.raw == pytest.approx(3.0 + index * 0.1 - 2.5)


def test_the_notes_carry_the_level_and_the_target(pillar: InflationPillar) -> None:
    """So a reader can check the gap without opening the observation set.

    `raw` is a difference, and a difference alone cannot be checked: +0.5 is
    consistent with 3.0 against 2.5 and with 2.5 against 2.0.

    Asserted as one whole string rather than as two substring checks. The
    substring version passed with the level and the target swapped, which
    produces "headline 2.5% against a 3.0% target": confidently, readably
    backwards, which is the failure this repository is built around.

    CHF rather than AUD, for two reasons that both hid a defect. AUD is index
    0 in ``sorted(G10)``, so the fixture ramp adds nothing and any
    off-by-a-currency error is invisible; and AUD is first in the components
    iteration order, so a note built from the wrong currency's components
    still reads correctly there.
    """
    scores = pillar.compute(universe_observations(3.0, 2.8), sorted(G10), ASOF)

    index = sorted(G10).index("CHF")
    assert scores["CHF"].notes == (
        f"inflation CHF: headline {3.0 + index * 0.1:.1f}% "
        f"for {PERIOD.isoformat()} against a 1.0% target"
    )


def test_a_currency_with_only_headline_is_scored_as_missing(
    pillar: InflationPillar,
) -> None:
    """0.40 of the sub-weight is under the floor, so headline alone is not enough.

    Asserted through the blend rather than read off the docstring, because the
    docstring cannot fail. Headline alone is too noisy to carry this pillar,
    and a score built on it would look exactly like a score built on both.
    """
    declared = fsum(pillar.component_weights.values())
    assert pillar.component_weights["cpi_gap"] / declared <= MIN_COMPONENT_WEIGHT

    supplied = universe_observations(3.0, 2.8)
    thinned = [
        entry
        for entry in supplied
        if not (entry.currency == "AUD" and entry.indicator == "core_cpi_yoy")
    ]

    scores = pillar.compute(thinned, sorted(G10), ASOF)

    assert scores["AUD"].z is None
    assert scores["AUD"].score == 0.0


def test_a_currency_with_only_core_is_still_scored(pillar: InflationPillar) -> None:
    """0.60 clears the floor, which is the asymmetry the sub-weights encode.

    Core is the series central banks act on, so it carries the pillar alone
    where headline cannot. Without this the previous test would pass against an
    implementation that simply refused every incomplete currency.
    """
    declared = fsum(pillar.component_weights.values())
    assert pillar.component_weights["core_gap"] / declared > MIN_COMPONENT_WEIGHT

    supplied = universe_observations(3.0, 2.8)
    thinned = [
        entry
        for entry in supplied
        if not (entry.currency == "AUD" and entry.indicator == "cpi_yoy")
    ]

    scores = pillar.compute(thinned, sorted(G10), ASOF)

    assert scores["AUD"].z is not None


def test_a_currency_with_nothing_is_marked_absent_not_neutral(
    pillar: InflationPillar,
) -> None:
    """`z` of None with a score of 0.0 is how every consumer tells the two apart."""
    supplied = [
        entry for entry in universe_observations(3.0, 2.8) if entry.currency != "JPY"
    ]

    scores = pillar.compute(supplied, sorted(G10), ASOF)

    assert scores["JPY"].z is None
    assert scores["JPY"].raw is None
    assert scores["JPY"].score == 0.0
    assert "cpi_yoy" in scores["JPY"].notes


def test_every_currency_asked_for_comes_back(pillar: InflationPillar) -> None:
    """Including the ones that could not be scored, which the scorer relies on."""
    scores = pillar.compute(universe_observations(3.0, 2.8), sorted(G10), ASOF)

    assert set(scores) == set(G10)


# --- what the review passes found these could not see ------------------------


def test_a_print_for_a_period_after_asof_takes_no_part(
    pillar: InflationPillar,
) -> None:
    """A forecast published early describes a month that has not happened.

    The visibility test above covers the other filter: a print describing the
    past that had not been published yet. This is the opposite shape and needs
    its own bound, because `_gap` reads the last element as the newest, so a
    forward-dated print silently becomes the current level. A run on 15
    September would score itself on a December number that was published,
    visible, and about a month that does not exist.

    `staleness_days` cannot be relied on to catch it: it floors a forward-dated
    period's age at zero rather than dropping it.
    """
    forecast = obs(
        "cpi_yoy",
        "USD",
        9.9,
        date(2026, 12, 1),
        released_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
    )
    current = obs("cpi_yoy", "USD", 3.0, PERIOD, released_at=published(PERIOD))

    extracted = pillar._extract([forecast, current], ("USD",), ASOF)

    assert [entry.value for entry in extracted["USD"]["cpi_yoy"]] == [3.0]


def test_an_unscorable_currency_gets_the_absence_reason_and_no_working(
    pillar: InflationPillar,
) -> None:
    """The exclusivity `_notes`'s docstring states, which nothing checked.

    AUD holds a real headline gap here and is still refused by
    `MIN_COMPONENT_WEIGHT`, so it is the case that makes the rule bite: there
    is a value to describe and no score to attach the description to. A hook
    called on the unscored branch would explain a number the pillar declined
    to publish, sitting beside the sentence saying it could not score.
    """
    supplied = universe_observations(3.0, 2.8)
    thinned = [
        entry
        for entry in supplied
        if not (entry.currency == "AUD" and entry.indicator == "core_cpi_yoy")
    ]

    scores = pillar.compute(thinned, sorted(G10), ASOF)

    assert scores["AUD"].z is None
    assert "core_cpi_yoy" in scores["AUD"].notes
    assert "headline" not in scores["AUD"].notes


def test_a_currency_scored_on_core_alone_still_shows_its_working(
    pillar: InflationPillar,
) -> None:
    """The case where an empty working is least affordable.

    Core carries 0.60 and clears the floor by itself, so such a currency
    reaches a report with a real score and an empty ``raw``. A note that
    describes only the headline would be blank for exactly the row whose
    headline number does not exist.
    """
    supplied = universe_observations(3.0, 2.8)
    thinned = [
        entry
        for entry in supplied
        if not (entry.currency == "AUD" and entry.indicator == "cpi_yoy")
    ]

    scores = pillar.compute(thinned, sorted(G10), ASOF)

    assert scores["AUD"].z is not None
    assert scores["AUD"].raw is None
    assert "core" in scores["AUD"].notes
    assert "no headline print" in scores["AUD"].notes


def test_the_note_names_the_period_the_print_describes(
    pillar: InflationPillar,
) -> None:
    """Both series are quarterly for AUD and NZD, so the age is not obvious.

    "headline 3.0% against a 2.5% target" reads identically whether the print
    landed last month or five months ago, and the second is the case a reader
    needs to notice. The note exists to be checkable, and a number with no
    period attached cannot be checked against a source.
    """
    scores = pillar.compute(universe_observations(3.0, 2.8), sorted(G10), ASOF)

    assert PERIOD.isoformat() in scores["USD"].notes


def test_only_the_targets_create_the_ranking_when_every_print_agrees(
    pillar: InflationPillar,
) -> None:
    """Eight identical prints, and the cross-section is entirely the targets.

    A pillar that scored the level would hand `cross_sectional_z` eight equal
    numbers and every score would come back 0.0. This one assertion kills the
    whole "forgot the target" family at the level a reader reads, rather than
    at `_transform`.
    """
    supplied: list[Observation] = []
    for currency in sorted(G10):
        supplied += prints(currency, 2.0, 2.0)

    scores = pillar.compute(supplied, sorted(G10), ASOF)

    assert scores["CHF"].score > 0.0
    assert scores["AUD"].score < 0.0
    assert scores["USD"].score == pytest.approx(scores["EUR"].score)
    assert scores["CHF"].score != pytest.approx(scores["AUD"].score)


def test_moving_one_currency_moves_the_ones_that_did_not(
    pillar: InflationPillar,
) -> None:
    """The scores are cross-sectional, so nothing here stands on its own.

    GBP's headline rises and everything else is held. CHF, AUD and the
    unchanged majority each move by a different amount, because each sits at a
    different distance from the new mean. A pillar scoring each currency in
    isolation passes every other test in this file and fails this one.
    """
    before = pillar.compute(universe_observations(3.0, 2.8), sorted(G10), ASOF)

    supplied = [
        obs(
            "cpi_yoy",
            entry.currency,
            entry.value + 2.0,
            entry.period,
            released_at=entry.released_at,
        )
        if entry.currency == "GBP" and entry.indicator == "cpi_yoy"
        else entry
        for entry in universe_observations(3.0, 2.8)
    ]
    after = pillar.compute(supplied, sorted(G10), ASOF)

    assert after["GBP"].score > before["GBP"].score
    for currency in ("CHF", "AUD", "USD"):
        assert after[currency].score != pytest.approx(before[currency].score)
    gbp_move = after["GBP"].score - before["GBP"].score
    chf_move = after["CHF"].score - before["CHF"].score
    assert abs(gbp_move) > abs(chf_move)
