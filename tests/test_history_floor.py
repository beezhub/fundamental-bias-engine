"""The time-series history floor is per cadence, not a single count.

`BasePillar.time_series_z` refused a window under twelve observations whatever
the series' frequency. Twelve was reasoned from the monthly case and no monthly
series calls it: the two live callers are ``cot_net_pct_oi``, which is weekly,
and ``vol_index``, which is daily. So twelve prints was eleven weeks for one and
a fortnight for the other, and both scored at full weight on it.

The reachable range is what makes that more than untidy. Over twelve readings
with ``ddof=1`` the largest possible ``|z|`` is ``sqrt(121/12) = 3.17``, past
POSITIONING's sign flip at 2.0 and close to its saturation at 3.33. A rebuilt
cache, a newly registered contract or a backfill that stopped after its first
page could put the pillar near the loudest value it can emit, against a currency
the other six pillars scored on years of history. The freshness ramp does not
catch it: it ages the newest print, and a twelve-week window's newest print is
current. Age and length are different facts and only one of them was measured.

`MIN_HISTORY_OBSERVATIONS` replaces the count. One rule generates every row, so
these tests check the rule rather than re-listing the numbers: the floor is one
year of that series' own prints, and never fewer than `ABSOLUTE_MIN_HISTORY`.

Nothing here reaches the network.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fbe.datasources.registry import (
    ABSOLUTE_MIN_HISTORY,
    MIN_HISTORY_OBSERVATIONS,
    history_floor,
)
from fbe.pillars.base import BasePillar
from fbe.pillars.risk import DRAWDOWN_WINDOW_SESSIONS, MIN_DRAWDOWN_WINDOW_SESSIONS
from fbe.types import Frequency, Observation

PRINTS_PER_YEAR = {
    Frequency.DAILY: 252,
    Frequency.WEEKLY: 52,
    Frequency.MONTHLY: 12,
    Frequency.QUARTERLY: 4,
    Frequency.ANNUAL: 1,
}
"""How many times a year each cadence prints, for deriving the floor here.

Written out rather than imported so the test computes the rule independently
instead of restating the table it is checking. ``IRREGULAR`` is absent because
it has no period to take a year of, which is the whole reason it falls to the
count floor.
"""


def series(
    values: list[float],
    frequency: Frequency,
    *,
    step: timedelta = timedelta(days=7),
) -> list[Observation]:
    """A one-currency, one-indicator history ending today, oldest first."""
    end = date(2026, 6, 30)
    return [
        Observation(
            indicator="test_series",
            currency="USD",
            value=value,
            period=end - step * (len(values) - 1 - index),
            source="test",
            series_id="derived",
            unit="percent",
            frequency=frequency,
        )
        for index, value in enumerate(values)
    ]


def alternating(count: int) -> list[float]:
    """``count`` values with a non-zero standard deviation and no trend.

    `time_series_z` refuses a flat history separately, so a fixture testing the
    length floor has to vary or it would be refused for the other reason and
    the test would pass for the wrong one.
    """
    return [16.0 + (1.0 if index % 2 else -1.0) for index in range(count)]


def test_every_frequency_has_a_floor() -> None:
    """A cadence missing from the mapping would raise `KeyError` at score time.

    That is a loud failure rather than a quiet one, but it would be loud in a
    morning run rather than here, so the mapping is required to be total over
    `Frequency`.
    """
    assert set(MIN_HISTORY_OBSERVATIONS) == set(Frequency)


@pytest.mark.parametrize("frequency", sorted(Frequency, key=lambda item: item.name))
def test_each_floor_is_one_year_of_prints_or_the_count_floor(
    frequency: Frequency,
) -> None:
    """The rule, applied to every row, so no number is merely asserted.

    A test listing 252, 52 and twelve would agree with the table and say
    nothing about where those came from. This recomputes each from the stated
    rule, so a row edited without a reason fails.
    """
    expected = max(PRINTS_PER_YEAR.get(frequency, 0), ABSOLUTE_MIN_HISTORY)

    assert MIN_HISTORY_OBSERVATIONS[frequency] == expected


def test_the_monthly_floor_is_unchanged() -> None:
    """The one cadence the old count was reasoned for keeps its value.

    Twelve monthly prints is a year, which is what the replaced constant said
    and the only case where it was right. Nothing monthly calls
    `time_series_z` today, so this row protects a caller that does not exist
    yet from being made stricter by accident.
    """
    assert MIN_HISTORY_OBSERVATIONS[Frequency.MONTHLY] == 12
    assert ABSOLUTE_MIN_HISTORY == 12


def test_a_window_mixing_cadences_takes_the_strictest_floor() -> None:
    """The added criterion, and the failure it is written against.

    Mixed cadences are not a state the registry produces today, since one
    `SeriesRef` carries one frequency. A helper reading ``series[0].frequency``
    would be correct until a pillar blends a weekly leg with a monthly one, and
    would then apply a floor of twelve to a window holding weekly data. That is
    the defect this mapping exists to remove, arriving by a different door.
    """
    floors = [
        history_floor([Frequency.MONTHLY, Frequency.WEEKLY]),
        history_floor([Frequency.WEEKLY, Frequency.MONTHLY]),
    ]

    assert floors == [MIN_HISTORY_OBSERVATIONS[Frequency.WEEKLY]] * 2
    assert (
        history_floor([Frequency.WEEKLY, Frequency.DAILY])
        == (MIN_HISTORY_OBSERVATIONS[Frequency.DAILY])
    )


def test_the_order_of_a_mixed_window_does_not_change_the_floor() -> None:
    """Stated separately because "strictest" and "first" agree half the time.

    A helper taking the first frequency passes the weekly-then-monthly case and
    fails the reverse, so a single ordering would leave the defect reachable.
    """
    assert history_floor([Frequency.DAILY, Frequency.ANNUAL]) == history_floor(
        [Frequency.ANNUAL, Frequency.DAILY]
    )


def test_asking_for_the_floor_of_nothing_raises() -> None:
    """An empty window has no cadence, so there is no floor to answer with.

    Returning `ABSOLUTE_MIN_HISTORY` would be a number nobody derived, handed
    to a caller that cannot tell it from a real one. The prime directive here
    is that a wrong number which looks right is the worst outcome, so this
    refuses instead.
    """
    with pytest.raises(ValueError, match="no frequencies"):
        history_floor([])


def test_a_weekly_window_one_print_short_is_refused() -> None:
    """The boundary from below, on the cadence the defect was found on."""
    floor = MIN_HISTORY_OBSERVATIONS[Frequency.WEEKLY]
    short = series(alternating(floor - 1), Frequency.WEEKLY)

    assert len(short) == floor - 1
    assert BasePillar.time_series_z(short, 5, None) is None


def test_a_weekly_window_exactly_at_the_floor_scores() -> None:
    """The boundary from above. Without it the floor could be raised silently."""
    floor = MIN_HISTORY_OBSERVATIONS[Frequency.WEEKLY]
    full = series(alternating(floor), Frequency.WEEKLY)

    assert len(full) == floor
    assert BasePillar.time_series_z(full, 5, None) is not None


def test_twelve_weekly_prints_no_longer_score() -> None:
    """The case in the issue, asserted against the number it used to pass on.

    Derived from `ABSOLUTE_MIN_HISTORY` rather than written as twelve, because
    what makes this case interesting is that it is exactly the old floor.
    """
    twelve = series(alternating(ABSOLUTE_MIN_HISTORY), Frequency.WEEKLY)

    assert len(twelve) == 12
    assert BasePillar.time_series_z(twelve, 5, None) is None


def test_a_daily_window_of_a_fortnight_is_refused() -> None:
    """The other live caller. ``vol_index`` is daily and twelve of those is two weeks.

    A volatility z-score over a fortnight is the same defect as the weekly one
    with a different denominator, and it is the larger change: RISK carries
    0.10 and now drops out until a year of sessions exists.
    """
    fortnight = series(
        alternating(ABSOLUTE_MIN_HISTORY), Frequency.DAILY, step=timedelta(days=1)
    )

    assert BasePillar.time_series_z(fortnight, 5, None) is None


def test_a_daily_window_of_a_year_scores() -> None:
    """252 sessions is the floor and it is inclusive."""
    floor = MIN_HISTORY_OBSERVATIONS[Frequency.DAILY]
    year = series(alternating(floor), Frequency.DAILY, step=timedelta(days=1))

    assert len(year) == floor
    assert BasePillar.time_series_z(year, 5, None) is not None


def test_a_monthly_window_of_twelve_still_scores() -> None:
    """The cadence that must not be made stricter by accident.

    Twelve monthly prints scored before this change and still does. Nothing
    monthly calls `time_series_z` today, so without this the mapping's monthly
    row would be unexercised and could drift with nothing noticing.
    """
    year = series(
        alternating(MIN_HISTORY_OBSERVATIONS[Frequency.MONTHLY]),
        Frequency.MONTHLY,
        step=timedelta(days=30),
    )

    assert len(year) == 12
    assert BasePillar.time_series_z(year, 5, None) is not None


def test_the_drawdown_minimum_does_not_move_with_the_z_score_floor() -> None:
    """The fourth criterion: RISK's twelve sessions are preserved deliberately.

    `MIN_DRAWDOWN_WINDOW_SESSIONS` aliased the single count the z-score floor
    used. Under a frequency-aware floor that alias would have dragged it from
    twelve sessions to 252 as a side effect of a change about z-scores, and the
    two answer different questions: whether a standard deviation means
    anything, against how much of a 252-session window must be present before a
    fall from its high is worth reporting.

    Whether twelve is the right answer to the second is open and was not ruled
    on with #214. What this pins is that it does not move because the first
    answer did, which is what an alias would have done silently.
    """
    assert MIN_DRAWDOWN_WINDOW_SESSIONS == 12
    assert MIN_HISTORY_OBSERVATIONS[Frequency.DAILY] != MIN_DRAWDOWN_WINDOW_SESSIONS
    assert MIN_DRAWDOWN_WINDOW_SESSIONS < DRAWDOWN_WINDOW_SESSIONS
