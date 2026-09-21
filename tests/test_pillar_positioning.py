"""Tests for POSITIONING, the one pillar whose sign is not monotonic in its input.

Four traps here, and three of them produce a plausible number rather than an
error.

**The publication lag.** Positions are snapped at Tuesday's close and published
on Friday afternoon, so this is the pillar where the visibility rule bites
hardest: a run dated Wednesday must not see the Tuesday that has not been
published. Filtering on ``period`` instead admits it, the score moves, and
nothing raises. That is why the Wednesday test asserts both sides of the
boundary rather than only the exclusion.

**The cross-section.** Five pillars z-score across the universe and this one
must not. A stray re-standardisation would leave every value plausible, on the
right band, and ranked the wrong way: `f(p)` already means something in its own
units, and scoring it against the other seven would say a currency is crowded
relative to today's company rather than relative to its own record. The
perturbation test is what catches it, because a cross-sectional step makes one
currency's move shift every other currency's score.

**The short history.** `time_series_z` refuses a window under twelve
observations, and a pillar that read that refusal as zero would report a
contract with two months of history as sitting exactly at its own average.

**The scale.** ``cot_net_pct_oi`` arrives as a percent of open interest, which
ADR 0011 settled. The scale cancels in the z-score, so a pillar handed shares
instead would score identically and print a headline number a hundred times
flatter. The fixture is built on the section 7.4 percents for that reason.

Every observation is built in this file. Nothing reaches the network.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from math import sqrt

import pytest

from fbe.config import ScoringConfig
from fbe.datasources.registry import INDICATORS
from fbe.pillars.base import MIN_TIME_SERIES_WINDOW, BasePillar
from fbe.pillars.positioning import (
    CONTRARIAN_CAP,
    CONTRARIAN_SLOPE,
    MOMENTUM_PEAK_Z,
    SIGN_FLIP_Z,
    PositioningPillar,
)
from fbe.types import Observation, PillarName
from fbe.universe import G10

INDICATOR = "cot_net_pct_oi"

NEWEST = date(2026, 9, 8)
"""A Tuesday. The CFTC snaps positions at Tuesday's close."""

RELEASE_LAG = timedelta(days=3)
"""Tuesday to Friday, the CFTC's own schedule. `fbe.datasources.cot` stamps it."""

ASOF = date(2026, 9, 14)
"""The Monday after the release, so every week in the fixture is visible."""

WEEKS = 20
"""Window length per currency.

Eight more than `MIN_TIME_SERIES_WINDOW` so a test can shorten a history and
still be testing the floor rather than the fixture. It also has to clear
``2 * p ** 2 + 1``, which is 12.52 at the section 7.4 fixture's widest reading
of -2.40, or `window` cannot build a history with that z and a real spread.
"""

# Section 7.4 of `docs/scoring-spec.md`: {currency: (net_pct_oi, mean, p, f(p))}.
FIXTURE: Mapping[str, tuple[float, float, float, float]] = {
    "USD": (26.2, 9.8, 1.90, 0.10),
    "EUR": (-2.4, 3.6, -0.60, -0.60),
    "GBP": (7.9, 3.7, 0.40, 0.40),
    "JPY": (-31.5, -6.3, -2.40, 0.60),
    "CHF": (-14.1, -2.0, -1.10, -0.90),
    "CAD": (-12.5, -4.5, -0.80, -0.80),
    "AUD": (5.4, -1.6, 0.70, 0.70),
    "NZD": (18.4, 4.1, 1.30, 0.70),
}
"""The published run, minus the standard deviation column.

`window` derives the standard deviation from the net, the mean and ``p`` so that
``p`` comes back exactly rather than to the 5e-3 the published triple supports.
`test_the_derived_standard_deviations_match_the_published_column` holds those
derived figures against the spec's own, so the fixture cannot drift away from
the document it claims to reproduce while still satisfying itself.
"""

PUBLISHED_SD: Mapping[str, float] = {
    "USD": 8.63,
    "EUR": 10.00,
    "GBP": 10.50,
    "JPY": 10.50,
    "CHF": 11.00,
    "CAD": 10.00,
    "AUD": 10.00,
    "NZD": 11.00,
}
"""Section 7.4's three-year standard deviation column, for that check alone."""

EMIT_SD = 0.6437
"""Section 3.6's published cross-sectional spread of `f(p)` on this fixture.

Against 1.0 for the five pillars that pass through the section 2.3
re-standardisation, which is why the declared 0.10 buys about 0.059 of effective
influence. Quoted here to be asserted, not to be believed.
"""


def window(net: float, mean: float, p: float, count: int = WEEKS) -> list[float]:
    """Build a history whose newest value z-scores to exactly ``p``.

    Args:
        net: The newest reading, in percent of open interest.
        mean: The mean the window is to have.
        p: The z-score the newest reading is to carry, non-zero.
        count: Window length. Must exceed ``2 * p ** 2 + 1`` or no window with
            this mean and this z exists.

    Returns:
        ``count`` values, oldest first, with the newest last. Their mean is
        ``mean`` and their sample standard deviation is ``(net - mean) / p``, so
        `BasePillar.time_series_z` returns ``p`` to floating-point precision.

    The construction is one deviation of ``+p * sd`` for the newest reading, one
    of ``-p * sd`` to cancel it, and the rest in ``+c, -c`` pairs sized to make
    the sum of squares come out at ``sd ** 2 * (count - 1)``. Deriving the
    history from the answer is deliberate: the alternative is to write values
    and hope their z lands near the published figure, which would make every
    assertion downstream approximate for no reason.
    """
    if p == 0.0:
        raise ValueError("p of zero leaves the standard deviation undetermined")
    pairs, remainder = divmod(count - 2, 2)
    if remainder:
        raise ValueError("count must be even, so the balancing deviations pair up")
    sd = (net - mean) / p
    spare = count - 1 - 2 * p * p
    if spare <= 0:
        raise ValueError(f"count of {count} is too short for a z of {p}")
    c = sd * sqrt(spare / (count - 2))
    deviations = [-p * sd] + [value for _ in range(pairs) for value in (c, -c)]
    return [mean + deviation for deviation in deviations] + [net]


def weekly(
    currency: str,
    values: Sequence[float],
    *,
    newest: date = NEWEST,
    stamped: bool = True,
) -> list[Observation]:
    """Turn values into consecutive weekly reports ending at ``newest``.

    Args:
        currency: ISO code.
        values: Readings, oldest first.
        newest: Period of the last reading, a Tuesday.
        stamped: Whether to carry ``released_at``. ``False`` leaves it absent so
            the assumed publication lag governs visibility instead, which is the
            case `DEFAULT_PUBLICATION_LAG_DAYS` exists for.

    Returns:
        One observation per value, weekly periods, each stamped as released the
        Friday after its Tuesday when ``stamped``.
    """
    spec = INDICATORS[INDICATOR]
    built = []
    for index, value in enumerate(reversed(values)):
        period = newest - timedelta(weeks=index)
        released = period + RELEASE_LAG
        built.append(
            Observation(
                indicator=INDICATOR,
                currency=currency,
                value=value,
                period=period,
                source="test",
                series_id="derived",
                unit=spec.unit,
                frequency=spec.frequency,
                released_at=(
                    datetime(
                        released.year, released.month, released.day, 19, 30, tzinfo=UTC
                    )
                    if stamped
                    else None
                ),
            )
        )
    return list(reversed(built))


def universe() -> list[Observation]:
    """The section 7.4 run, one twenty-week history per currency."""
    return [
        observation
        for currency, (net, mean, p, _) in FIXTURE.items()
        for observation in weekly(currency, window(net, mean, p))
    ]


@pytest.fixture
def pillar() -> PositioningPillar:
    return PositioningPillar()


# --- the fixture itself, which every assertion below rests on ----------------


def test_the_window_helper_reproduces_the_z_it_was_built_for() -> None:
    """`window` derives a history from an answer, so it is checked against it.

    A helper that quietly produced a different z would leave every test in this
    file asserting something true about the wrong number, which is the failure
    this project treats as worse than a crash.
    """
    for currency, (net, mean, p, _) in FIXTURE.items():
        series = weekly(currency, window(net, mean, p))

        values = [item.value for item in series]

        assert len(series) == WEEKS, currency
        assert series[-1].value == pytest.approx(net), currency
        # The mean and the spread, not only their ratio. `time_series_z` is
        # ``(newest - mean) / sd``, so any wrong pair with the right ratio
        # would satisfy the z assertion below and leave the fixture claiming a
        # history it does not have.
        assert statistics.fmean(values) == pytest.approx(mean, abs=1e-9), currency
        assert statistics.stdev(values) == pytest.approx((net - mean) / p, abs=1e-9), (
            currency
        )
        assert BasePillar.time_series_z(
            series, ScoringConfig().lookback_years, ASOF
        ) == pytest.approx(p, abs=1e-9), currency


def test_the_derived_standard_deviations_match_the_published_column() -> None:
    """The fixture is section 7.4's, not a lookalike built to satisfy itself.

    `window` derives each standard deviation from the net, the mean and ``p``,
    so this is the check that those derived figures are the ones the spec
    published rather than whatever made the arithmetic work.
    """
    for currency, (net, mean, p, _) in FIXTURE.items():
        assert (net - mean) / p == pytest.approx(PUBLISHED_SD[currency], abs=5e-3), (
            currency
        )


# --- the history, and where it comes from ------------------------------------


def test_the_shared_extract_is_used_rather_than_a_copy() -> None:
    """Criterion 1, asserted on the function rather than on its output.

    An override that reimplemented the visibility and vintage rules would pass
    every behavioural test in this file on the day it was written and miss the
    next fix to either rule, which is the defect #122 exists to prevent. The
    base implementation already returns one row per period over the whole
    history, sorted ascending, which is exactly what a time-series z needs.
    """
    assert PositioningPillar._extract is BasePillar._extract


def test_the_history_arrives_oldest_first_and_one_row_per_period(
    pillar: PositioningPillar,
) -> None:
    """Sorted ascending, whatever order the observations were handed over in.

    `time_series_z` takes the newest by period rather than the last element, so
    an unsorted history would not misread the newest print. What it would
    misread is the window: the oldest rows are dropped by the lookback bound,
    and that bound is on the period, so the order matters to a reader of the
    extracted slice rather than to the arithmetic. Asserted because the
    docstring promises it.
    """
    series = weekly("USD", window(*FIXTURE["USD"][:3]))
    shuffled = [series[5], series[0], series[-1], *series[1:5], *series[6:-1]]
    revised = Observation(
        indicator=INDICATOR,
        currency="USD",
        value=99.0,
        period=NEWEST,
        source="test",
        series_id="derived",
        unit=series[-1].unit,
        frequency=series[-1].frequency,
        released_at=series[-1].released_at,
        revision=1,
    )

    extracted = pillar._extract([*shuffled, revised], ["USD"], ASOF)
    found = extracted["USD"][INDICATOR]

    assert [item.period for item in found] == sorted(item.period for item in series)
    assert len(found) == WEEKS
    assert found[-1].value == 99.0, "the newest vintage of the newest period wins"


def test_a_wednesday_run_cannot_see_the_tuesday_the_cftc_has_not_published(
    pillar: PositioningPillar,
) -> None:
    """The criterion this pillar turns on, asserted from both sides.

    Tuesday's positions are published on Friday. A run dated the Wednesday in
    between must score on the previous week, and a run dated the Friday must see
    the new one. Filtering on ``period`` would admit a reading three days before
    it existed, on the pillar whose whole premise is that the crowd's position is
    already known.
    """
    series = weekly("USD", window(*FIXTURE["USD"][:3]))
    wednesday = NEWEST + timedelta(days=1)
    friday = NEWEST + RELEASE_LAG

    before = pillar._extract(series, ["USD"], wednesday)["USD"][INDICATOR]
    after = pillar._extract(series, ["USD"], friday)["USD"][INDICATOR]

    assert wednesday.strftime("%A") == "Wednesday"
    assert friday.strftime("%A") == "Friday"
    assert [item.period for item in before] == [item.period for item in series[:-1]], (
        "the unpublished Tuesday is not knowable on the Wednesday"
    )
    assert after[-1].period == NEWEST


def test_an_unstamped_report_falls_back_to_the_assumed_weekly_lag(
    pillar: PositioningPillar,
) -> None:
    """The source stamps `released_at`, and the rule must not depend on it.

    A weekly series with no stamp is admitted seven days after its period under
    `DEFAULT_PUBLICATION_LAG_DAYS`, which is four days later than the CFTC's own
    schedule. That is the conservative direction, and it is the one the base
    class already implements; this pins that POSITIONING does not quietly get a
    different rule.
    """
    series = weekly("USD", window(*FIXTURE["USD"][:3]), stamped=False)

    visible = pillar._extract(series, ["USD"], NEWEST + timedelta(days=6))
    admitted = pillar._extract(series, ["USD"], NEWEST + timedelta(days=7))

    assert visible["USD"][INDICATOR][-1].period == NEWEST - timedelta(weeks=1)
    assert admitted["USD"][INDICATOR][-1].period == NEWEST

    # And the count of inputs resting on the assumption, which is the diagnostic
    # the unstamped path exists to feed. Every print here is unstamped, so it is
    # the whole history.
    scored = PositioningPillar().compute(series, ["USD"], NEWEST + timedelta(days=7))
    assert scored["USD"].diagnostics["assumed_lag_inputs"] == pytest.approx(WEEKS)


# --- p, and the shape function it feeds --------------------------------------


def test_p_is_the_time_series_z_of_the_currencys_own_history(
    pillar: PositioningPillar,
) -> None:
    """Criterion 3. Every currency, against the helper rather than a restatement."""
    scores = pillar.compute(universe(), list(G10), ASOF)

    for currency, (_, _, p, response) in FIXTURE.items():
        assert scores[currency].z == pytest.approx(response, abs=5e-3), currency
        assert scores[currency].z == pytest.approx(
            PositioningPillar.response(p), abs=1e-9
        ), currency


def test_the_lookback_window_is_the_configured_one() -> None:
    """The wire, not the value. A hardcoded window would pass the test above.

    The twenty-week fixture cannot show this: every positive ``lookback_years``
    holds all twenty weeks, so one, three and five all give the same z and a
    corrupted window would be invisible. The history here is long enough for
    the bound to bite, so three years and five years take different windows of
    it, and the newest reading carries a different z in each. Both are real
    readings rather than one being a refusal, which is what makes it a test of
    the window rather than of the floor.
    """
    trending = [float(index) for index in range(280)]
    observations = weekly("USD", trending)

    three = PositioningPillar(ScoringConfig(lookback_years=3))
    five = PositioningPillar(ScoringConfig(lookback_years=5))
    three_z = three.compute(observations, ["USD"], ASOF)["USD"].z
    five_z = five.compute(observations, ["USD"], ASOF)["USD"].z

    assert three_z is not None and five_z is not None
    assert three_z != pytest.approx(five_z)
    assert ScoringConfig().lookback_years == 5
    assert five_z == pytest.approx(
        PositioningPillar.response(BasePillar.time_series_z(observations, 5, ASOF))
    )

    # And the note counts the window rather than the history, which is the
    # first fixture here where the two differ: 280 weekly prints, of which 261
    # fall inside the five years before the run date and 156 inside three.
    five_note = five.compute(observations, ["USD"], ASOF)["USD"].notes
    three_note = three.compute(observations, ["USD"], ASOF)["USD"].notes
    assert "over 261 weekly reports" in five_note
    assert "over 156 weekly reports" in three_note


def test_the_lookback_is_anchored_on_the_run_date_and_not_on_the_last_print(
    pillar: PositioningPillar,
) -> None:
    """The two anchors differ once a contract stops reporting, and only then.

    `BasePillar.time_series_z` defaults its ``asof`` to the newest period in the
    series, and `_extract` has already dropped anything later than the run date,
    so for a current contract the two give the same window and nothing would
    notice the argument missing. They part company on a contract whose newest
    print is old: anchored on the run date the window is the five years before
    today, which for a print two years stale holds only the three years that
    overlap; anchored on the print it is the five years before that print.

    The run date is the right anchor. `ScoringConfig.lookback_years` names the
    history the model wants relative to the run, so a dead contract is measured
    against a shrinking window and eventually falls under the twelve-observation
    floor and is refused, which is the loud direction. Anchoring on the last
    print would z-score it against its own five best years forever and report a
    confident reading on a contract nobody trades.
    """
    stale_newest = NEWEST - timedelta(weeks=104)
    trending = [float(index) for index in range(200)]
    series = weekly("USD", trending, newest=stale_newest)

    anchored = BasePillar.time_series_z(series, pillar.config.lookback_years, ASOF)
    on_the_print = BasePillar.time_series_z(series, pillar.config.lookback_years)
    scored = pillar.compute(series, ["USD"], ASOF)["USD"]

    assert anchored is not None and on_the_print is not None
    assert anchored != pytest.approx(on_the_print), (
        "the fixture must separate the two anchors or this test proves nothing"
    )
    assert scored.z == pytest.approx(PositioningPillar.response(anchored))
    # The window the note reports is anchored the same way: 157 of the 200
    # prints fall inside the five years before the run date, where all 200 fall
    # inside the five years before the newest print.
    assert "over 157 weekly reports" in scored.notes


def test_a_history_under_the_window_floor_scores_none_rather_than_zero(
    pillar: PositioningPillar,
) -> None:
    """Criterion 4. Eleven weeks is not a positioning extreme, it is no reading.

    A zero would say this contract sits exactly at its own average, which is a
    statement about the crowd that nobody made. The neutral 0.0 on the score is
    `missing_score`'s placeholder and the ``None`` on ``z`` is what the
    aggregator reads to drop the pillar's weight.
    """
    values = window(*FIXTURE["USD"][:3])
    short = weekly("USD", values[-(MIN_TIME_SERIES_WINDOW - 1) :])

    scores = pillar.compute(short, ["USD"], ASOF)

    assert len(short) == MIN_TIME_SERIES_WINDOW - 1
    assert scores["USD"].z is None
    assert scores["USD"].raw is None
    assert scores["USD"].score == 0.0
    # `compute` names the missing indicator only where the currency had none of
    # it at all, and this currency has eleven weeks of it. So the note says it
    # could not score and stops there, which is thin for a reader but is
    # `BasePillar.compute`'s behaviour for all seven pillars rather than this
    # pillar's to change. What matters here is that it claims no reading.
    assert "could not score" in scores["USD"].notes
    assert "standard deviation" not in scores["USD"].notes


def test_twelve_weekly_prints_score_at_full_weight_and_say_how_few(
    pillar: PositioningPillar,
) -> None:
    """The other half of the floor, and the half that produces a number.

    `MIN_TIME_SERIES_WINDOW` is a count with no notion of the series' frequency,
    so on a weekly series it is about eleven weeks rather than the year its own
    docstring reasons about. A contract with twelve prints therefore scores, at
    the configured weight, with the freshness ramp satisfied because the newest
    print is current. Over twelve readings with ``ddof=1`` the reachable ``|p|``
    runs to 3.17, which is inside the contrarian branch.

    That is issue #214 and it is not fixed here, because the floor is shared
    with every other pillar and the frequency-aware replacement is a number
    nobody has ruled on. What this asserts is that the case is visible: the note
    names the print count, so a reader is told the mean is over twelve weeks
    rather than over five years.
    """
    values = window(*FIXTURE["NZD"][:3], count=MIN_TIME_SERIES_WINDOW)
    short = weekly("NZD", values)

    scored = pillar.compute(short, ["NZD"], ASOF)["NZD"]

    assert len(short) == MIN_TIME_SERIES_WINDOW
    assert scored.z is not None, "twelve prints is the floor, not below it"
    assert scored.weight == pytest.approx(0.10)
    assert scored.freshness_factor == pytest.approx(1.0)
    assert f"{MIN_TIME_SERIES_WINDOW} weekly reports" in scored.notes
    assert "5-year" not in scored.notes


def test_the_note_counts_the_window_rather_than_claiming_the_configured_years(
    pillar: PositioningPillar,
) -> None:
    """The window is the history the contract has, not the years config asked for.

    An earlier version of this note said "its own 5-year mean" whatever the
    window held, which is the one part of it a reader could not check. The count
    is checkable, and it is what tells a reader a twenty-week history from a
    five-year one.
    """
    scored = pillar.compute(universe(), list(G10), ASOF)["USD"]

    assert f"over {WEEKS} weekly reports" in scored.notes
    assert str(ScoringConfig().lookback_years) + "-year" not in scored.notes


def test_p_has_a_typed_home_and_not_only_prose(pillar: PositioningPillar) -> None:
    """Nothing may parse `notes`, and this pillar's score is unreadable without ``p``.

    A score of +0.60 is a crowded short being faded, and a score of +0.60 from
    the confirming branch is a moderate long. Only ``p`` tells the two apart, so
    `fbe.types.PillarScore.notes` requires it to have a field or a diagnostics
    key of its own.
    """
    scores = pillar.compute(universe(), list(G10), ASOF)

    for currency, (_, _, p, _) in FIXTURE.items():
        assert scores[currency].diagnostics["positioning_z"] == pytest.approx(
            p, abs=1e-9
        ), currency


def test_an_unscored_currency_carries_no_positioning_z(
    pillar: PositioningPillar,
) -> None:
    """An absent key, not a placeholder. A zero here would read as a reading."""
    scores = pillar.compute([], list(G10), ASOF)

    assert "positioning_z" not in scores["USD"].diagnostics
    assert "emit_sd" in scores["USD"].diagnostics, "the base measurements survive"


def test_a_history_that_never_moved_scores_none(pillar: PositioningPillar) -> None:
    """A flat book is not a crowded one and is not an average one either.

    `time_series_z` refuses a window whose readings are all equal, because a
    series that has not moved says nothing about whether its newest print is
    high or low. The pillar has to carry that refusal through as an absence.
    """
    scores = pillar.compute(weekly("USD", [4.0] * WEEKS), ["USD"], ASOF)

    assert scores["USD"].z is None
    assert scores["USD"].score == 0.0


def test_a_currency_with_no_contract_scores_none_and_not_neutral(
    pillar: PositioningPillar,
) -> None:
    """Criterion 9. There is no NZD contract in this run, so there is no reading.

    The shortfall has to reach the reader as reduced coverage, which the scorer
    derives from ``z is None``. A neutral zero would put the currency at its own
    average and spend the pillar's weight saying so.
    """
    observations = [
        observation for observation in universe() if observation.currency != "NZD"
    ]

    scores = pillar.compute(observations, list(G10), ASOF)

    assert scores["NZD"].z is None
    assert scores["NZD"].raw is None
    assert scores["NZD"].score == 0.0
    # The indicator key, not the currency code. `compute` puts the code in the
    # note on every branch, so asserting it would pass with the absent-indicator
    # list empty, which is the half of the message a reader needs.
    assert "cot_net_pct_oi" in scores["NZD"].notes
    assert scores["USD"].z is not None, "the rest of the universe still scores"


# --- no cross-sectional step -------------------------------------------------


def test_one_currencys_history_moving_leaves_every_other_score_untouched(
    pillar: PositioningPillar,
) -> None:
    """Criterion 5, and the test a stray re-standardisation fails.

    Positioning is meaningful against a currency's own record, not against the
    company it keeps today. A cross-sectional step would make the yen's crowding
    change what the pound's score means, which is precisely what the shape
    function exists to avoid.
    """
    before = pillar.compute(universe(), list(G10), ASOF)

    moved = [
        observation
        for currency, (net, mean, p, _) in FIXTURE.items()
        for observation in weekly(
            currency,
            window(net, mean, p) if currency != "JPY" else window(-31.5, -6.3, -1.10),
        )
    ]
    after = pillar.compute(moved, list(G10), ASOF)

    assert after["JPY"].z != pytest.approx(before["JPY"].z)
    for currency in G10:
        if currency == "JPY":
            continue
        assert after[currency].z == pytest.approx(before[currency].z), currency


def test_normalise_returns_the_response_unchanged(pillar: PositioningPillar) -> None:
    """Criterion 5's first half, on the method rather than through `compute`."""
    responses = {"USD": 1.75, "EUR": -0.5, "JPY": None}

    normalised = pillar._normalise(
        {
            currency: {"positioning_response": value}
            for currency, value in responses.items()
        }
    )

    assert normalised == responses


def test_transform_refuses_a_slice_built_without_the_series(
    pillar: PositioningPillar,
) -> None:
    """`BasePillar._extract` guarantees the key, carrying an empty sequence.

    So a missing key means the mapping was built some other way, and a `.get`
    would read that as a currency with no contract, which is a reading. The
    same reasoning as `_normalise`'s guard below, and this repository's rule is
    that a failure has to be tested to fail.
    """
    with pytest.raises(KeyError):
        pillar._transform({"USD": {}}, ASOF)


def test_normalise_refuses_a_mapping_built_without_the_response(
    pillar: PositioningPillar,
) -> None:
    """`_transform` always emits the key, carrying ``None`` where it has no reading.

    So a missing key means the caller built the mapping some other way, and
    answering ``None`` for it would report that as a universe-wide data outage.
    The same reasoning as `fbe.pillars.risk.RiskPillar._normalise`.
    """
    with pytest.raises(KeyError):
        pillar._normalise({"USD": {"net_percent": 26.2}})


# --- what a reader sees ------------------------------------------------------


def test_raw_carries_the_net_percent_and_the_notes_carry_p(
    pillar: PositioningPillar,
) -> None:
    """``raw`` is the natural unit and ``p`` is prose, per the two contracts.

    `fbe.types.PillarScore.raw` is "the pillar's headline number in its natural
    unit before normalisation" and `BasePillar.headline_component` names this
    pillar as the case where a report-only component fills it. A z-score is
    neither natural-unit nor pre-normalisation, and a renderer printing ``raw``
    beside the input's unit would label ``+1.90`` as a percent of open interest
    for a currency actually holding +26.2. So the net percent goes to ``raw``
    and ``p`` goes to ``notes``, which is what this pillar's own docstring says.
    The pull request records where that leaves the issue's wording.
    """
    scores = pillar.compute(universe(), list(G10), ASOF)

    assert scores["USD"].raw == pytest.approx(26.2)
    assert scores["JPY"].raw == pytest.approx(-31.5)
    assert "1.90" in scores["USD"].notes
    assert "26.2" in scores["USD"].notes
    assert NEWEST.isoformat() in scores["USD"].notes


def test_the_notes_name_the_branch_the_currency_landed_on(
    pillar: PositioningPillar,
) -> None:
    """The one thing a reader cannot work out from the two numbers alone.

    A negative score on a large net long is the pillar doing its job, and the
    branch name is what says so. Section 7.4 labels the same three branches.
    """
    scores = pillar.compute(universe(), list(G10), ASOF)

    assert "momentum" in scores["GBP"].notes, "p of 0.40 confirms"
    assert "fading" in scores["USD"].notes, "p of 1.90 has stopped helping"
    assert "contrarian" in scores["JPY"].notes, "p of -2.40 argues the other way"


def test_a_currency_the_pillar_could_not_score_gets_no_working(
    pillar: PositioningPillar,
) -> None:
    """`_notes` must not describe a number nobody has.

    `compute` calls it only on the scored branch, and the unscored branch
    carries its own note naming the indicator that was missing. Asserted here
    because an override that formatted the absent case would put prose about a
    reading in front of a reader who has none.
    """
    scores = pillar.compute([], list(G10), ASOF)

    assert "standard deviation" not in scores["USD"].notes
    assert "could not score" in scores["USD"].notes


def test_emit_sd_reports_the_spread_the_spec_publishes(
    pillar: PositioningPillar,
) -> None:
    """Criterion 7, against section 3.6's 0.6437 on this fixture.

    Recomputed from the pillar's own output rather than read out of the
    document, and the document's figure is the assertion target, so the two have
    to agree. It is what makes the gap between the declared 0.10 and the roughly
    0.059 of effective influence visible on the page instead of in a comment.
    """
    scores = pillar.compute(universe(), list(G10), ASOF)

    # Not recomputed as a population standard deviation over the eight scores
    # here: that is definitionally what `_emit_sd` does when all eight score, so
    # it would check the base class rather than this pillar.
    # `tests/test_emit_sd.py` owns that, including the rule that excludes a
    # currency the pillar could not score.
    assert scores["USD"].diagnostics["emit_sd"] == pytest.approx(EMIT_SD, abs=5e-5)
    assert all(
        scores[currency].diagnostics["emit_sd"] == scores["USD"].diagnostics["emit_sd"]
        for currency in G10
    )


def test_the_pillar_disagrees_with_a_crowded_long(pillar: PositioningPillar) -> None:
    """The point of the pillar, stated as an assertion on the published run.

    The yen holds the largest short in the fixture and scores positively; the
    dollar holds the largest long and scores almost nothing. A run where this
    pillar never disagrees with the crowd is a run where it is not being
    computed, which the issue says in as many words.
    """
    scores = pillar.compute(universe(), list(G10), ASOF)

    assert scores["JPY"].raw is not None and scores["JPY"].raw < 0
    assert scores["JPY"].score > 0, "a record short is a squeeze risk, not a sell"
    assert scores["USD"].raw is not None and scores["USD"].raw > 0
    assert abs(scores["USD"].score) < 0.2, "the dollar bull case is well owned"


# --- the response function's four properties ---------------------------------


@pytest.mark.parametrize(
    ("p", "expected"),
    [
        (0.0, "momentum"),
        (0.9, "momentum"),
        (MOMENTUM_PEAK_Z, "momentum"),
        (-MOMENTUM_PEAK_Z, "momentum"),
        (1.5, "fading"),
        (SIGN_FLIP_Z, "fading"),
        (-SIGN_FLIP_Z, "fading"),
        (2.4, "contrarian"),
        (-12.0, "contrarian"),
        (None, ""),
    ],
)
def test_the_branch_names_follow_the_same_two_constants_as_the_response(
    p: float | None, expected: str
) -> None:
    """The split lives once, beside `response`, and both joins are pinned.

    Each boundary belongs to the branch below it, which is what makes the label
    agree with the value: at ``|p| = 1`` both branches give magnitude 1.0 and at
    ``|p| = 2`` both give zero, so a currency exactly on a join is described by
    either name and the code has to pick one and keep picking it.
    """
    assert PositioningPillar.branch(p) == expected


def test_a_ragged_run_reads_each_currency_at_its_own_newest_week(
    pillar: PositioningPillar,
) -> None:
    """A real run does not arrive with every contract at the same week.

    One source failing part way leaves one currency a fortnight behind the rest,
    and both the headline number and the note read the newest print per
    currency rather than per run. Every currency in the section 7.4 fixture
    shares a newest period, so nothing there could tell a per-currency read from
    a per-run one.
    """
    behind = NEWEST - timedelta(weeks=2)
    observations = [
        observation
        for currency, (net, mean, p, _) in FIXTURE.items()
        for observation in weekly(
            currency,
            window(net, mean, p),
            newest=behind if currency == "CHF" else NEWEST,
        )
    ]

    scores = pillar.compute(observations, list(G10), ASOF)

    assert behind.isoformat() in scores["CHF"].notes
    assert NEWEST.isoformat() in scores["USD"].notes
    assert scores["CHF"].raw == pytest.approx(FIXTURE["CHF"][0])
    assert scores["CHF"].staleness_days > scores["USD"].staleness_days
    assert scores["CHF"].z == pytest.approx(FIXTURE["CHF"][3], abs=5e-3), (
        "a fortnight behind is still that currency's own record"
    )


@pytest.mark.parametrize("p", [0.0, 0.25, 1.0, 1.5, 2.0, 2.4, 3.33, 4.0, 12.0])
def test_the_response_is_odd_about_zero(p: float) -> None:
    """Section 3.6: ``f(-p) = -f(p)``. No long or short asymmetry."""
    positive = PositioningPillar.response(p)
    negative = PositioningPillar.response(-p)

    assert positive is not None and negative is not None
    assert negative == pytest.approx(-positive)


def test_the_response_is_continuous_at_both_joins() -> None:
    """Both branches agree at ``|p| = 1`` and at ``|p| = 2``.

    A discontinuity would let one week's data move a currency from a large
    positive score to a large negative one, which is the behaviour the
    continuous sign flip exists to prevent.
    """
    tiny = 1e-9
    for boundary in (MOMENTUM_PEAK_Z, SIGN_FLIP_Z):
        below = PositioningPillar.response(boundary - tiny)
        at = PositioningPillar.response(boundary)
        above = PositioningPillar.response(boundary + tiny)

        assert below is not None and at is not None and above is not None
        assert below == pytest.approx(at, abs=1e-6), boundary
        assert above == pytest.approx(at, abs=1e-6), boundary


def test_the_confirming_branch_peaks_at_one_and_crosses_zero_at_two() -> None:
    """The two landmarks section 3.6 asks an implementer to assert."""
    peak = PositioningPillar.response(MOMENTUM_PEAK_Z)
    crossing = PositioningPillar.response(SIGN_FLIP_Z)

    assert peak == pytest.approx(MOMENTUM_PEAK_Z)
    assert crossing == pytest.approx(0.0)
    assert max(
        abs(PositioningPillar.response(p / 100) or 0.0) for p in range(0, 201)
    ) == pytest.approx(MOMENTUM_PEAK_Z)


def test_the_contrarian_branch_saturates_below_the_clip() -> None:
    """Magnitude 2.0 from ``|p| = 3.33`` upward, never reaching the 3.0 clip.

    The cap is what stops the weakest data in the model from becoming its
    loudest voice at an extreme, which `CONTRARIAN_CAP` records.
    """
    saturation = SIGN_FLIP_Z + CONTRARIAN_CAP / CONTRARIAN_SLOPE

    for p in (saturation, 4.0, 10.0, 100.0):
        response = PositioningPillar.response(p)

        assert response is not None
        assert response == pytest.approx(-CONTRARIAN_CAP), p
    assert ScoringConfig().score_clip > CONTRARIAN_CAP


def test_response_carries_an_absent_z_through_as_absent() -> None:
    """``None`` in, ``None`` out. A zero here would be a reading."""
    assert PositioningPillar.response(None) is None


# --- the contract with the rest of the engine --------------------------------


def test_the_pillar_reads_only_the_one_key_it_declares() -> None:
    """One component, one series, sub-weight 1.00, as section 3.6 specifies."""
    pillar = PositioningPillar()

    assert pillar.name is PillarName.POSITIONING
    assert tuple(pillar.requires) == (INDICATOR,)
    assert pillar.component_weights == {"positioning_response": 1.0}
    assert pillar.headline_component == "net_percent"


def test_a_single_component_pillar_records_no_blend_path(
    pillar: PositioningPillar,
) -> None:
    """Nothing blends here, so the divisor path stays empty rather than stale.

    `compute` clears it before the run for exactly this case: a `_normalise`
    that does not blend would otherwise leave the previous call's path on this
    call's scores.
    """
    scores = pillar.compute(universe(), list(G10), ASOF)

    assert scores["USD"].blend_divisor_path == ""
    assert pillar.last_blend_sd is None
