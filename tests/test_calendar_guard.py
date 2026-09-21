"""The calendar guard's three-outcome contract: blocked, clear, or unknown.

Before this contract, ``is_blacked_out`` returned ``(False, None)`` whenever
nothing in a bare event sequence covered ``when``. That is what a genuinely
clear calendar returns, and it is also what a failed fetch and a stale Friday
cache return, because an empty sequence cannot say which of the three
happened. ``CalendarCoverage`` and `coverage_gap` exist to make that
difference visible, and this file pins the shape of the fix.

``is_blacked_out`` and `coverage_gap` are both still scaffolded (Phase 4 in
``docs/roadmap.md``), so most assertions here are guarded: while a function
raises ``NotImplementedError``, the test asserts exactly that, the same
pattern ``tests/test_datasource_base.py`` uses for ``available()``. Removing a
name from `SCAFFOLDED` is what turns the guard into a real assertion, and
forgetting to would leave the guard silently asserting nothing, which a bare
``except NotImplementedError: pass`` would do by accident. What is checked
unconditionally, because it needs no arithmetic to be true today: the contract
shape itself, so a regression in the signature is caught even while the body
is still a stub.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from fbe.calendar_guard import (
    CalendarCoverage,
    CoverageGap,
    coverage_gap,
    is_blacked_out,
)
from fbe.config import DataConfig
from fbe.types import CalendarEvent

SCAFFOLDED: frozenset[str] = frozenset()
"""Remove a name once its function stops raising ``NotImplementedError``.

Forces this file to start asserting real values the day Phase 4 lands, rather
than continuing to pass on the strength of a guarded branch nobody revisits.

Empty since #198 implemented both, so every assertion below is now real. The
guard machinery stays rather than being deleted with the set: `next_clear_time`
and `action_for_open_position` are the second half of this module and are still
scaffolded, so the next issue to land here has the pattern to hand.
"""

CONFIG = DataConfig()
MONDAY_9AM = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
FRIDAY_EOD = datetime(2026, 9, 11, 23, 59, tzinfo=UTC)


def _event(
    currency: str, scheduled_for: datetime, title: str = "CPI y/y"
) -> CalendarEvent:
    return CalendarEvent(
        title=title,
        currency=currency,
        scheduled_for=scheduled_for,
        impact="high",
    )


def _assert_guarded_or(name: str, call: object, expected: object) -> None:
    """Run ``call`` (a zero-arg callable); compare to ``expected`` unless scaffolded."""
    if name in SCAFFOLDED:
        with pytest.raises(NotImplementedError):
            call()  # type: ignore[operator]
        return
    assert call() == expected  # type: ignore[operator]


# --- the contract shape, checked unconditionally -----------------------------


def test_is_blacked_out_takes_coverage_not_a_bare_event_sequence() -> None:
    """A bare ``Sequence[CalendarEvent]`` cannot say whether it was ever fetched.

    This is the parameter that removes the ambiguity, so its name and presence
    are asserted directly rather than only through behaviour that a stub
    cannot yet produce.
    """
    parameters = inspect.signature(is_blacked_out).parameters
    assert "calendar" in parameters
    assert parameters["calendar"].annotation in ("CalendarCoverage", CalendarCoverage)
    assert "events" not in parameters


def test_is_blacked_out_can_return_a_third_state() -> None:
    """``bool`` alone cannot hold blocked, clear and unknown.

    ``(False, None)`` must stop being the only shape this function can return
    for a ``when`` the data does not cover, which is the defect in issue #43.
    """
    return_annotation = inspect.signature(is_blacked_out).return_annotation
    assert "None" in str(return_annotation)


def test_coverage_gap_returns_none_when_the_moment_is_covered_by_signature() -> None:
    """`coverage_gap` is a query with an honest negative, not a bare flag."""
    return_annotation = inspect.signature(coverage_gap).return_annotation
    assert "None" in str(return_annotation)
    assert "CoverageGap" in str(return_annotation)


# --- CalendarCoverage itself: not a stub, so this is a real assertion -------


def test_calendar_coverage_defaults_to_a_successful_fetch() -> None:
    """A concrete guard that never touches ``fetch_ok`` should read as fetched.

    Only a source that knows it failed sets ``fetch_ok=False``; a caller who
    forgets the field entirely must not accidentally claim failure.
    """
    coverage = CalendarCoverage(events=(), covers_through=MONDAY_9AM)
    assert coverage.fetch_ok is True
    assert coverage.fetch_error is None


def test_calendar_coverage_with_no_cache_has_no_horizon() -> None:
    """The state after a first-ever fetch fails with nothing to fall back on."""
    coverage = CalendarCoverage(
        events=(), covers_through=None, fetch_ok=False, fetch_error="Request Denied"
    )
    assert coverage.covers_through is None
    assert coverage.fetch_error == "Request Denied"


# --- coverage_gap's intended categories, guarded --------------------------


def test_fetch_failed_with_no_cache_is_unknown() -> None:
    """`CoverageGap.FETCH_FAILED`: nothing to fall back on."""
    coverage = CalendarCoverage(
        events=(), covers_through=None, fetch_ok=False, fetch_error="Request Denied"
    )
    _assert_guarded_or(
        "coverage_gap",
        lambda: coverage_gap(coverage, MONDAY_9AM),
        (CoverageGap.FETCH_FAILED, "Request Denied"),
    )


def test_fetch_failed_with_an_older_cache_is_stale_not_failed() -> None:
    """`CoverageGap.STALE_CACHE`: a previous fetch's coverage is being reused.

    Distinct from `FETCH_FAILED`, because the response here is different: the
    data is probably still accurate and only needs a refresh when convenient,
    not an immediate manual check.
    """
    coverage = CalendarCoverage(
        events=(_event("EUR", FRIDAY_EOD),),
        covers_through=FRIDAY_EOD,
        fetch_ok=False,
        fetch_error="Request Denied",
    )
    _assert_guarded_or(
        "coverage_gap",
        lambda: coverage_gap(coverage, MONDAY_9AM),
        (CoverageGap.STALE_CACHE, "cached through 2026-09-11T23:59:00+00:00"),
    )


def test_friday_cache_queried_for_monday_is_beyond_horizon() -> None:
    """The recurring case: the feed covers one week and Monday is next week.

    ``docs/data-sources.md``: "Because the feed only covers the current week,
    the blackout check is blind to anything from Sunday onwards. A Friday run
    cannot see Monday's events." A fresh Friday fetch (``fetch_ok=True``) that
    genuinely does not reach Monday is `BEYOND_HORIZON`, not a failure: nothing
    was wrong with the fetch, the feed just does not publish that far ahead.
    """
    coverage = CalendarCoverage(events=(), covers_through=FRIDAY_EOD, fetch_ok=True)
    _assert_guarded_or(
        "coverage_gap",
        lambda: coverage_gap(coverage, MONDAY_9AM),
        (CoverageGap.BEYOND_HORIZON, "covers through 2026-09-11T23:59:00+00:00"),
    )


def test_coverage_reaching_when_has_no_gap() -> None:
    """The moment being asked about is inside the fetched horizon: no gap."""
    coverage = CalendarCoverage(events=(), covers_through=MONDAY_9AM, fetch_ok=True)
    _assert_guarded_or("coverage_gap", lambda: coverage_gap(coverage, MONDAY_9AM), None)


# --- is_blacked_out's three outcomes, guarded -------------------------------


def test_blocked_when_a_leg_has_a_qualifying_event_in_window() -> None:
    coverage = CalendarCoverage(
        events=(_event("EUR", MONDAY_9AM),), covers_through=MONDAY_9AM, fetch_ok=True
    )
    _assert_guarded_or(
        "is_blacked_out",
        lambda: is_blacked_out("EURUSD", MONDAY_9AM, coverage, CONFIG)[0],
        True,
    )


def test_clear_when_coverage_reaches_when_and_nothing_qualifies() -> None:
    coverage = CalendarCoverage(events=(), covers_through=MONDAY_9AM, fetch_ok=True)
    _assert_guarded_or(
        "is_blacked_out",
        lambda: is_blacked_out("EURUSD", MONDAY_9AM, coverage, CONFIG),
        (False, None),
    )


def test_unknown_when_the_fetch_failed_and_there_is_no_cache() -> None:
    """The defect itself: this used to be indistinguishable from clear."""
    coverage = CalendarCoverage(
        events=(), covers_through=None, fetch_ok=False, fetch_error="Request Denied"
    )
    _assert_guarded_or(
        "is_blacked_out",
        lambda: is_blacked_out("EURUSD", MONDAY_9AM, coverage, CONFIG)[0],
        None,
    )


def test_unknown_for_a_friday_cache_queried_for_the_following_monday() -> None:
    """The weekly recurring case, exercised end to end through ``is_blacked_out``."""
    coverage = CalendarCoverage(events=(), covers_through=FRIDAY_EOD, fetch_ok=True)
    _assert_guarded_or(
        "is_blacked_out",
        lambda: is_blacked_out("EURUSD", MONDAY_9AM, coverage, CONFIG)[0],
        None,
    )


def test_unknown_outcome_never_returns_a_none_reason() -> None:
    """`(None, None)` would be exactly the ambiguity this contract removes."""
    coverage = CalendarCoverage(
        events=(), covers_through=None, fetch_ok=False, fetch_error="Request Denied"
    )
    if "is_blacked_out" in SCAFFOLDED:
        with pytest.raises(NotImplementedError):
            is_blacked_out("EURUSD", MONDAY_9AM, coverage, CONFIG)
        return
    blocked, reason = is_blacked_out("EURUSD", MONDAY_9AM, coverage, CONFIG)
    assert blocked is None
    assert reason is not None
