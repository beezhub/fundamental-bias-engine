"""The entry half of the calendar guard: blocked, clear, or could not tell.

Four traps here, and three of them return a plausible answer rather than
raising.

**The feed capitalises its impact field.** `fbe.datasources.calendar` publishes
``"High"``, ``"Medium"``, ``"Low"`` and ``"Holiday"`` exactly as the feed emits
them, and records that `fbe.types.CalendarEvent.impact`'s docstring is stale in
saying otherwise. A comparison written as ``event.impact == "high"`` matches
nothing on real data, and the failure is silent: every week reads as clear and
the guard never blocks anything. `IMPACT_LEVELS` is imported here so the
assertion is tied to what the feed actually sends.

**The order of the two checks.** `coverage_gap` has to run before a single
event is inspected. A guard that looks at events first returns a confident
"clear" for a morning it could not see, which is the whole reason the third
answer exists. The test for it puts a blocking event in the window and a
coverage horizon short of the moment, so the assertion turns on the ordering
rather than on the absence of events.

**Both legs.** A pair is a ratio and either side moves it. Checking only the
quote currency is the easy mistake, because the quote carries the pip, and it
would leave every EUR, GBP, AUD and NZD release unguarded on exactly the dollar
pairs the plan trades. Base and quote have their own tests.

**Adjacent windows.** Two releases forty minutes apart produce two windows a
naive check can fall between. They merge, and so do windows that merely touch:
a window ending at the instant the next begins leaves no tradeable moment
between them, so reporting two would invite a caller to find a gap that is not
there.

Every event is built in this file. Nothing reaches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from fbe.calendar_guard import (
    HIGH_IMPACT_KEYWORDS,
    CalendarCoverage,
    CoverageGap,
    blackout_windows,
    coverage_gap,
    is_blacked_out,
    is_high_impact,
)
from fbe.config import DataConfig
from fbe.datasources.calendar import IMPACT_LEVELS
from fbe.types import CalendarEvent

CONFIG = DataConfig()
"""Defaults: 30 minutes before a release and 60 after."""

WHEN = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
"""A Monday morning, the moment an entry is being considered."""

QUIET_TITLE = "Widget Sentiment Index"
"""A title matching no category in `HIGH_IMPACT_KEYWORDS`.

Asserted rather than assumed in `test_the_quiet_title_really_matches_nothing`,
because a title that happens to match a keyword would turn every test using it
into a test of something else.
"""


def event(
    currency: str = "EUR",
    scheduled_for: datetime | None = None,
    *,
    title: str = "CPI y/y",
    impact: str = "High",
) -> CalendarEvent:
    """Build one calendar event.

    Args:
        currency: ISO code the release belongs to.
        scheduled_for: Release time, defaulting to `WHEN`.
        title: Feed title, verbatim.
        impact: Feed impact string. Capitalised by default, because that is
            what the feed sends.

    Returns:
        A `CalendarEvent`, with the feed's own defaults for the fields this
        module does not read.
    """
    return CalendarEvent(
        title=title,
        currency=currency,
        scheduled_for=WHEN if scheduled_for is None else scheduled_for,
        impact=impact,
    )


def covering(
    *events: CalendarEvent,
    through: datetime | None = None,
    fetch_ok: bool = True,
    fetch_error: str | None = None,
) -> CalendarCoverage:
    """Wrap events in coverage that reaches `WHEN` unless told otherwise."""
    return CalendarCoverage(
        events=events,
        covers_through=WHEN if through is None else through,
        fetch_ok=fetch_ok,
        fetch_error=fetch_error,
    )


# --- is_high_impact, one half of the OR at a time ----------------------------


def test_the_quiet_title_really_matches_nothing() -> None:
    """Guards every other test that uses `QUIET_TITLE` to mean "not a keyword"."""
    folded = QUIET_TITLE.lower()

    for keywords in HIGH_IMPACT_KEYWORDS.values():
        for keyword in keywords:
            assert keyword not in folded, keyword


def test_the_feeds_rating_alone_qualifies() -> None:
    """First half of the OR. The title carries no keyword at all here."""
    assert is_high_impact(event(title=QUIET_TITLE, impact="High")) is True


def test_the_feed_capitalises_its_rating_and_the_comparison_folds_case() -> None:
    """The documented trap, asserted against the feed's own vocabulary.

    `fbe.datasources.calendar.IMPACT_LEVELS` is what the feed sends, capitalised,
    and its docstring says a consumer comparing against the lower-case spelling
    in `fbe.types.CalendarEvent` "would match nothing and report every week as
    clear". Both spellings are asserted so neither direction of the fold can
    regress.
    """
    assert "High" in IMPACT_LEVELS
    assert "high" not in IMPACT_LEVELS

    for spelling in ("High", "high", "HIGH"):
        assert is_high_impact(event(title=QUIET_TITLE, impact=spelling)) is True, (
            spelling
        )


def test_a_keyword_in_the_title_alone_qualifies() -> None:
    """Second half of the OR. The feed rates this one low.

    The feeds assign impact editorially, so an unscheduled remark or a revision
    routinely arrives tagged medium and moves a pair forty pips. The keyword
    list is the trading plan's own list of events to avoid, which is why either
    side of the OR is enough on its own.
    """
    assert is_high_impact(event(title="US CPI y/y", impact="Low")) is True


def test_neither_the_rating_nor_the_title_leaves_it_out() -> None:
    """The case that has to stay False, or the guard blocks every morning."""
    assert is_high_impact(event(title=QUIET_TITLE, impact="Low")) is False


@pytest.mark.parametrize("category", sorted(HIGH_IMPACT_KEYWORDS))
def test_every_category_has_a_title_that_qualifies_on_the_keyword_alone(
    category: str,
) -> None:
    """Each of the ten categories, so a matcher reading one of them is caught.

    The rating is held at ``"Low"`` so the only thing that can qualify the
    event is its title.
    """
    for keyword in HIGH_IMPACT_KEYWORDS[category]:
        assert is_high_impact(event(title=keyword, impact="Low")) is True, keyword


def test_the_title_match_folds_case() -> None:
    """The feed capitalises titles and the keywords are written lower case.

    Without the fold this returns False for every real title, which is a
    classifier that never fires.
    """
    assert is_high_impact(event(title="NON-FARM PAYROLLS", impact="Low")) is True


# --- blackout_windows --------------------------------------------------------


def test_one_event_becomes_one_window_of_the_configured_width() -> None:
    """Thirty minutes before and sixty after, from `DataConfig`."""
    windows = blackout_windows([event()], CONFIG)

    assert list(windows) == [
        (WHEN - timedelta(minutes=30), WHEN + timedelta(minutes=60))
    ]


def test_the_window_widths_come_from_config_and_not_from_the_code() -> None:
    """The wire, not the value. A literal 30 and 60 would pass the test above."""
    narrow = DataConfig(calendar_blackout_before_min=5, calendar_blackout_after_min=10)

    windows = blackout_windows([event()], narrow)

    assert list(windows) == [
        (WHEN - timedelta(minutes=5), WHEN + timedelta(minutes=10))
    ]


def test_overlapping_windows_merge_into_one() -> None:
    """A release at 13:30 and a speaker at 14:00, which is the criterion's case.

    Two windows here would leave 14:30 to 15:00 looking clear to a check that
    took the first window it found, and that is the half hour after a release
    when the first move reverses.
    """
    release = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)
    speaker = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)

    windows = blackout_windows(
        [
            event(scheduled_for=release),
            event(scheduled_for=speaker, title="Fed Chair Speaks"),
        ],
        CONFIG,
    )

    assert list(windows) == [
        (release - timedelta(minutes=30), speaker + timedelta(minutes=60))
    ]


def test_windows_that_only_touch_merge_as_well() -> None:
    """A window ending exactly when the next begins leaves no tradeable instant.

    Reporting two would invite a caller to find a gap that is not there, which
    is the reasoning `fbe.datasources.calendar` records for the same rule.
    """
    first = WHEN
    second = first + timedelta(minutes=90)

    windows = blackout_windows(
        [event(scheduled_for=first), event(scheduled_for=second)], CONFIG
    )

    assert list(windows) == [
        (first - timedelta(minutes=30), second + timedelta(minutes=60))
    ]


def test_windows_come_back_sorted_disjoint_and_in_utc() -> None:
    """Three events handed over newest first, and one in another timezone."""
    late = datetime(2026, 9, 14, 20, 0, tzinfo=UTC)
    middle = datetime(2026, 9, 14, 15, 0, tzinfo=timezone(timedelta(hours=2)))
    early = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)

    windows = blackout_windows(
        [
            event(scheduled_for=late),
            event(scheduled_for=middle),
            event(scheduled_for=early),
        ],
        CONFIG,
    )

    starts = [start for start, _ in windows]
    assert starts == sorted(starts)
    assert len(windows) == 3
    for (_, closes), (opens, _) in zip(windows[:-1], windows[1:], strict=True):
        assert closes < opens, "disjoint after merging"
    for opens, closes in windows:
        assert opens.utcoffset() == timedelta(0)
        assert closes.utcoffset() == timedelta(0)


def test_an_event_that_does_not_qualify_produces_no_window() -> None:
    """Nothing scheduled is not a window of zero length."""
    assert blackout_windows([event(title=QUIET_TITLE, impact="Low")], CONFIG) == ()


def test_no_events_produce_no_windows() -> None:
    assert blackout_windows([], CONFIG) == ()


def test_a_naive_scheduled_for_is_refused() -> None:
    """Comparing a naive moment against an aware one raises later and elsewhere.

    Two hours is the whole window plus change, which is what a naive South
    African time read as UTC would cost, so this refuses rather than assuming
    either zone.
    """
    naive = datetime(2026, 9, 14, 9, 0)

    with pytest.raises(ValueError, match="naive"):
        blackout_windows([event(scheduled_for=naive)], CONFIG)


def test_a_naive_event_is_refused_even_when_it_would_not_qualify() -> None:
    """ "Any event", per the docstring, and not just the ones that make windows.

    A feed sending naive times is broken in a way that will reach a qualifying
    event on the next run, so the refusal is not conditional on this run's
    events happening to be quiet.
    """
    naive = datetime(2026, 9, 14, 9, 0)

    with pytest.raises(ValueError, match="naive"):
        blackout_windows(
            [event(scheduled_for=naive, title=QUIET_TITLE, impact="Low")], CONFIG
        )


# --- coverage_gap, beyond what tests/test_calendar_guard.py already pins -----


def test_coverage_gap_refuses_a_naive_when() -> None:
    with pytest.raises(ValueError, match="naive"):
        coverage_gap(covering(), datetime(2026, 9, 14, 9, 0))


def test_a_failed_fetch_with_no_message_still_names_a_reason() -> None:
    """`fetch_error` is optional on the dataclass, and the reason is not.

    The contract is that an unknown answer always carries a reason, so a
    failure that arrived without a message cannot be the one case that returns
    `None` and reads as clear.
    """
    # Built directly: `covering` substitutes `WHEN` for a `None` horizon, and
    # the absent horizon is the whole point of this case.
    failed = CalendarCoverage(
        events=(), covers_through=None, fetch_ok=False, fetch_error=None
    )

    gap = coverage_gap(failed, WHEN)

    assert gap is not None
    category, reason = gap
    assert category is CoverageGap.FETCH_FAILED
    assert reason


def test_a_failed_fetch_is_a_gap_even_when_its_cache_reaches_the_moment() -> None:
    """A failure sitting on top of a usable cache is still a failure.

    `CalendarCoverage.events` is documented as meaningful only when ``fetch_ok``
    is true, and says a caller must read ``fetch_ok`` "before reading
    ``events``, not after, or a failure sitting on top of a stale cache reads as
    an empty week rather than as a fetch that failed". So the horizon reaching
    `WHEN` is not enough on its own: the events that would have to be scanned
    for a clear answer are the ones the failed fetch did not bring back.

    This is the case a horizon-first implementation gets wrong while passing
    every other test in this file, because no other test pairs a failed fetch
    with a horizon that reaches the moment.
    """
    stale = CalendarCoverage(
        events=(),
        covers_through=WHEN + timedelta(days=1),
        fetch_ok=False,
        fetch_error="Request Denied",
    )

    gap = coverage_gap(stale, WHEN)

    assert gap is not None
    assert gap[0] is CoverageGap.STALE_CACHE
    assert is_blacked_out("EURUSD", WHEN, stale, CONFIG)[0] is None


def test_coverage_exactly_reaching_when_is_not_a_gap() -> None:
    """The boundary. ``covers_through == when`` is covered, per the docstring."""
    assert coverage_gap(covering(through=WHEN), WHEN) is None


def test_coverage_one_second_short_is_a_gap() -> None:
    """The other side of the same boundary."""
    gap = coverage_gap(covering(through=WHEN - timedelta(seconds=1)), WHEN)

    assert gap is not None
    assert gap[0] is CoverageGap.BEYOND_HORIZON


# --- is_blacked_out, the three answers ---------------------------------------


def test_the_coverage_check_runs_before_any_event_is_inspected() -> None:
    """The criterion this issue turns on.

    A qualifying event sits in the window, so a guard that inspected events
    first would answer `(True, ...)`, which looks like the guard working. The
    coverage stops an hour short of the moment, so the honest answer is that it
    could not tell. Blocked and clear are both dishonest here.
    """
    blocking = event(scheduled_for=WHEN)
    coverage = covering(blocking, through=WHEN - timedelta(hours=1))

    blocked, reason = is_blacked_out("EURUSD", WHEN, coverage, CONFIG)

    assert blocked is None, "an event found inside a gap is not a blocked answer"
    assert reason is not None
    assert CoverageGap.BEYOND_HORIZON.value in reason


def test_blocked_when_the_base_leg_carries_the_event() -> None:
    """EUR on EURUSD. The half a quote-only check would miss."""
    coverage = covering(event("EUR", WHEN))

    blocked, reason = is_blacked_out("EURUSD", WHEN, coverage, CONFIG)

    assert blocked is True
    assert reason is not None
    assert "EUR" in reason


def test_blocked_when_the_quote_leg_carries_the_event() -> None:
    """USD on EURUSD, the leg carrying the pip."""
    coverage = covering(event("USD", WHEN, title="Non-Farm Payrolls"))

    blocked, reason = is_blacked_out("EURUSD", WHEN, coverage, CONFIG)

    assert blocked is True
    assert reason is not None
    assert "USD" in reason


def test_an_event_on_a_third_currency_does_not_block() -> None:
    """A yen release is not a reason to stand aside from EURUSD."""
    coverage = covering(event("JPY", WHEN))

    assert is_blacked_out("EURUSD", WHEN, coverage, CONFIG) == (False, None)


def test_a_non_qualifying_event_on_the_right_leg_does_not_block() -> None:
    """The filter has to be the event's impact, not merely its currency.

    A low-impact release with no keyword in its title, on a leg of the pair, at
    the exact moment being asked about. Everything about it lines up except the
    one thing that matters, so an implementation that filtered on the currency
    alone would block a morning it has no reason to.
    """
    coverage = covering(event("EUR", WHEN, title=QUIET_TITLE, impact="Low"))

    assert is_blacked_out("EURUSD", WHEN, coverage, CONFIG) == (False, None)


def test_the_reason_reports_the_release_time_in_utc() -> None:
    """The event is scheduled at 11:00 in a +02:00 zone, which is 09:00 UTC.

    A reason that printed the local hour and labelled it UTC is the same class
    of error as a naive comparison: the string looks right and is two hours
    wrong, and the trader checking it against their platform finds nothing at
    that time.
    """
    local = datetime(2026, 9, 14, 11, 0, tzinfo=timezone(timedelta(hours=2)))
    coverage = covering(event("EUR", local, title="CPI y/y"))

    blocked, reason = is_blacked_out("EURUSD", WHEN, coverage, CONFIG)

    assert blocked is True
    assert reason is not None
    assert "09:00 UTC" in reason
    assert "11:00" not in reason


def test_the_reason_names_the_event_its_currency_and_its_time() -> None:
    """So a shortlist row explains the skip rather than carrying a bare flag.

    The pair and the word "blocked" are the caller's: `fbe.bias.apply_filters`
    prefixes this string with ``"event: "``, and the row it renders reads
    "EURUSD blocked: EUR CPI y/y at ...". Repeating the pair here would put it
    in the string twice.
    """
    coverage = covering(event("EUR", WHEN, title="CPI y/y"))

    _, reason = is_blacked_out("EURUSD", WHEN, coverage, CONFIG)

    assert reason is not None
    assert "EUR" in reason
    assert "CPI y/y" in reason
    assert "09:00" in reason
    assert "UTC" in reason
    assert "EURUSD" not in reason


def test_clear_is_false_with_no_reason() -> None:
    """The only outcome the checklist's news box may be ticked on."""
    assert is_blacked_out("EURUSD", WHEN, covering(), CONFIG) == (False, None)


def test_an_event_outside_the_window_does_not_block() -> None:
    """Three hours earlier is over and done with."""
    coverage = covering(event("EUR", WHEN - timedelta(hours=3)))

    assert is_blacked_out("EURUSD", WHEN, coverage, CONFIG) == (False, None)


@pytest.mark.parametrize("offset_min", [-30, 60])
def test_the_window_edges_are_inside_the_blackout(offset_min: int) -> None:
    """Both boundaries block, which is what makes touching windows merge.

    An entry at the instant a window opens or closes is an entry inside it. The
    same inclusivity is why two windows that merely touch leave no tradeable
    moment between them.
    """
    release = WHEN - timedelta(minutes=offset_min)
    coverage = covering(event("EUR", release))

    blocked, _ = is_blacked_out("EURUSD", WHEN, coverage, CONFIG)

    assert blocked is True


@pytest.mark.parametrize("offset_min", [-31, 61])
def test_one_minute_outside_the_window_is_clear(offset_min: int) -> None:
    """The other side of both boundaries, so the edges are pinned from both."""
    release = WHEN - timedelta(minutes=offset_min)
    coverage = covering(event("EUR", release))

    assert is_blacked_out("EURUSD", WHEN, coverage, CONFIG) == (False, None)


@pytest.mark.parametrize(
    ("category", "coverage"),
    [
        (
            CoverageGap.FETCH_FAILED,
            CalendarCoverage(
                events=(),
                covers_through=None,
                fetch_ok=False,
                fetch_error="Request Denied",
            ),
        ),
        (
            CoverageGap.STALE_CACHE,
            CalendarCoverage(
                events=(),
                covers_through=WHEN - timedelta(days=3),
                fetch_ok=False,
                fetch_error="Request Denied",
            ),
        ),
        (
            CoverageGap.BEYOND_HORIZON,
            CalendarCoverage(
                events=(), covers_through=WHEN - timedelta(days=3), fetch_ok=True
            ),
        ),
    ],
)
def test_unknown_names_the_coverage_gap_category(
    category: CoverageGap, coverage: CalendarCoverage
) -> None:
    """Each category reaches the reason, because each calls for a different act.

    A failed fetch with nothing behind it means check the calendar by hand. A
    stale cache means refresh when convenient. Beyond horizon means wait, since
    the feed publishes one week at a time and no retry brings Monday forward.
    """
    blocked, reason = is_blacked_out("EURUSD", WHEN, coverage, CONFIG)

    assert blocked is None
    assert reason is not None
    assert category.value in reason


def test_the_unknown_answer_is_never_false() -> None:
    """`(False, None)` standing in for the third answer is the defect itself."""
    coverage = CalendarCoverage(
        events=(), covers_through=None, fetch_ok=False, fetch_error="Request Denied"
    )

    blocked, reason = is_blacked_out("EURUSD", WHEN, coverage, CONFIG)

    assert blocked is not False
    assert blocked is None
    assert reason is not None


def test_a_naive_when_is_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        is_blacked_out("EURUSD", datetime(2026, 9, 14, 9, 0), covering(), CONFIG)


@pytest.mark.parametrize("pair", ["EUR", "EURUSDX", "", "EURUS"])
def test_a_malformed_pair_is_refused(pair: str) -> None:
    """Six characters, two legs. A five-character pair has no honest legs."""
    with pytest.raises(ValueError):
        is_blacked_out(pair, WHEN, covering(), CONFIG)


def test_the_blackout_minutes_reach_is_blacked_out_from_config() -> None:
    """The wire again, on the function the pre-trade check actually calls.

    The same release is inside a wide window and outside a narrow one, so this
    fails against an `is_blacked_out` that built its own windows from literals.
    """
    release = WHEN - timedelta(minutes=45)
    coverage = covering(event("EUR", release))
    wide = DataConfig(calendar_blackout_before_min=30, calendar_blackout_after_min=90)
    narrow = DataConfig(calendar_blackout_before_min=30, calendar_blackout_after_min=10)

    assert is_blacked_out("EURUSD", WHEN, coverage, wide)[0] is True
    assert is_blacked_out("EURUSD", WHEN, coverage, narrow) == (False, None)
