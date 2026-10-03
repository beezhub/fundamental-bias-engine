"""When a blocked pair is next clear, and what the guard says it cannot know.

Issue #199. `next_clear_time` turns a blocked pair from "not today" into "check
again at 15:00". Ruled on the issue on 2026-10-03, reading B: it and
`action_for_open_position` take a `CalendarCoverage` and run `coverage_gap`
first, so "unknown" means one thing across the three functions that decide
blocked or clear, and a failed fetch never reads as clear or as hold.

Every event is built here. No network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from fbe.calendar_guard import (
    CalendarCoverage,
    OpenPositionAction,
    action_for_open_position,
    is_blacked_out,
    next_clear_time,
)
from fbe.config import DataConfig
from fbe.types import CalendarEvent
from fbe.universe import GLOBAL

CONFIG = DataConfig()
"""Defaults: 30 minutes before a release and 60 after."""

DAY = datetime(2026, 9, 14, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def event(currency: str, when: datetime, title: str = "CPI y/y") -> CalendarEvent:
    return CalendarEvent(
        title=title, currency=currency, scheduled_for=when, impact="High"
    )


def covering(
    *events: CalendarEvent,
    through: datetime | None = None,
    fetch_ok: bool = True,
    fetch_error: str | None = None,
) -> CalendarCoverage:
    return CalendarCoverage(
        events=events,
        covers_through=at(23, 59) if through is None else through,
        fetch_ok=fetch_ok,
        fetch_error=fetch_error,
    )


FAILED_FETCH = CalendarCoverage(
    events=(), covers_through=None, fetch_ok=False, fetch_error="Request Denied"
)
"""A scrape that returned nothing: no events and no horizon to vouch for, built
directly because `covering` always supplies a horizon."""


# --- outside and inside a window ------------------------------------------------


def test_a_moment_outside_every_window_is_returned_unchanged() -> None:
    calendar = covering(event("EUR", at(14)))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(9)) == at(9)


def test_a_moment_inside_a_window_returns_the_end_of_that_window() -> None:
    """CPI at 09:00 shuts 08:30 to 10:00."""
    calendar = covering(event("EUR", at(9)))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(9, 15)) == at(10)


def test_the_quote_leg_shuts_the_pair_as_the_base_leg_does() -> None:
    calendar = covering(event("USD", at(9)))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(9, 15)) == at(10)


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (at(8, 30), at(10)),  # the instant the window opens is inside it
        (at(10), at(10)),  # the instant it closes is inside it too
        (at(8, 29), at(8, 29)),  # a minute before it opens is clear
        (at(10, 1), at(10, 1)),  # a minute after it closes is clear
    ],
)
def test_the_exact_boundaries(after: datetime, expected: datetime) -> None:
    """Containment is inclusive at both ends, as in `is_blacked_out`, so the
    window's own closing instant answers with itself."""
    calendar = covering(event("EUR", at(9)))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=after) == expected


# --- merged windows ---------------------------------------------------------------


def test_overlapping_windows_return_the_end_of_the_run() -> None:
    """The #198 case. Releases at 13:30 and 14:00 shut 13:00 to 14:30 and
    13:30 to 15:00, one run ending at 15:00, not at 14:30."""
    calendar = covering(event("USD", at(13, 30)), event("USD", at(14), "NFP"))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(13, 10)) == at(15)


def test_windows_on_the_two_legs_merge_into_one_run() -> None:
    """EUR at 09:00 shuts until 10:00, and USD at 10:15 opens at 09:45."""
    calendar = covering(event("EUR", at(9)), event("USD", at(10, 15)))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(9)) == at(11, 15)


def test_a_window_that_only_touches_the_run_still_extends_it() -> None:
    """A window ending exactly when the next opens leaves no tradeable instant."""
    calendar = covering(event("EUR", at(9)), event("EUR", at(10, 30), "GDP q/q"))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(9)) == at(11, 30)


def test_events_on_neither_leg_and_global_events_do_not_shut_the_pair() -> None:
    """Legs only, as `is_blacked_out` matches. A global event is named, never
    enforced (#227)."""
    calendar = covering(event("JPY", at(9)), event(GLOBAL, at(9), "G20 Meetings"))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(9)) == at(9)


def test_it_agrees_with_the_entry_check_on_either_side_of_the_answer() -> None:
    """Blocked just before the clear time it returns, and clear just after."""
    calendar = covering(event("USD", at(13, 30)), event("USD", at(14), "NFP"))
    clear = next_clear_time("EURUSD", calendar, CONFIG, after=at(13, 10))
    assert clear is not None

    assert is_blacked_out("EURUSD", clear - timedelta(minutes=1), calendar, CONFIG)[0]
    after_clear = is_blacked_out(
        "EURUSD", clear + timedelta(minutes=1), calendar, CONFIG
    )
    assert after_clear == (False, None)


# --- time zones -------------------------------------------------------------------


def test_the_answer_is_aware_utc_even_when_the_question_was_not() -> None:
    johannesburg = timezone(timedelta(hours=2))
    calendar = covering(event("EUR", at(14)))

    answer = next_clear_time(
        "EURUSD", calendar, CONFIG, after=at(9).astimezone(johannesburg)
    )

    assert answer == at(9)
    assert answer is not None and answer.utcoffset() == timedelta(0)
    assert answer.tzinfo is UTC


def test_a_naive_moment_is_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        next_clear_time("EURUSD", covering(), CONFIG, after=datetime(2026, 9, 14, 9))


# --- what it cannot know ----------------------------------------------------------


def test_a_failed_fetch_is_unknown_not_clear() -> None:
    """An empty calendar from a failed scrape must never read as clear now."""
    calendar = FAILED_FETCH

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(9)) is None


def test_a_moment_past_the_coverage_is_unknown() -> None:
    calendar = covering(through=at(8))

    assert next_clear_time("EURUSD", calendar, CONFIG, after=at(9)) is None


def test_coverage_ending_before_the_run_is_known_to_end_is_unknown() -> None:
    """Criterion 4. The window closes at 10:00, but a release up to 10:30 would
    open a window touching it, so coverage must reach 10:30 to know 10:00 is
    really the end. Coverage to 10:15 cannot vouch for that."""
    short = covering(event("EUR", at(9)), through=at(10, 15))
    enough = covering(event("EUR", at(9)), through=at(10, 30))

    assert next_clear_time("EURUSD", short, CONFIG, after=at(9, 15)) is None
    assert next_clear_time("EURUSD", enough, CONFIG, after=at(9, 15)) == at(10)


def test_a_quiet_calendar_that_covers_the_moment_is_clear() -> None:
    """Reading A was refused for this: a genuinely quiet week is clear, not
    unknown, as long as the data vouches for it."""
    assert next_clear_time("EURUSD", covering(), CONFIG, after=at(9)) == at(9)


# --- the sibling, which had the same blind spot ------------------------------------


def test_a_failed_fetch_no_longer_reads_as_hold() -> None:
    """Before #199, ``(HOLD, None)`` answered a failed scrape and a quiet
    morning alike. Now the scrape says it could not tell, and why."""
    calendar = FAILED_FETCH

    action, reason = action_for_open_position("EURUSD", at(9), calendar, CONFIG)

    assert action is None
    assert reason is not None and "Request Denied" in reason


def test_a_quiet_covered_morning_still_holds() -> None:
    action, reason = action_for_open_position("EURUSD", at(9), covering(), CONFIG)

    assert action is OpenPositionAction.HOLD
    assert reason is None


def test_a_moment_past_the_coverage_is_unknown_for_a_position_too() -> None:
    action, reason = action_for_open_position(
        "EURUSD", at(9), covering(through=at(8)), CONFIG
    )

    assert action is None
    assert reason is not None
