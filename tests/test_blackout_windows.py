"""The entry half of the calendar guard: blocked, clear, or could not tell.

Four traps here, and three of them return a plausible answer rather than
raising.

**The feed capitalises its impact field.** `fbe.datasources.calendar` publishes
``"High"``, ``"Medium"``, ``"Low"`` and ``"Holiday"`` exactly as the feed emits
them, and is named by `fbe.types.CalendarEvent.impact` as the authority on
that vocabulary. A comparison written as ``event.impact == "high"`` matches
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

import json
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from fbe.calendar_guard import (
    HIGH_IMPACT_KEYWORDS,
    CalendarCoverage,
    CoverageGap,
    _merge,
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
    """Wrap events in coverage that comfortably reaches `WHEN`.

    An hour past it, not exactly on it. An earlier version defaulted to `WHEN`
    itself, which parked every test in this file on the ``covers_through ==
    when`` boundary: flipping that comparison turned seventeen tests red, so the
    two that own the boundary were lost in the noise and no test exercised the
    ordinary case of coverage that reaches well past the moment.

    The helper cannot express an absent horizon, since `None` here means "use
    the default". The two states that carry no horizon build their
    `CalendarCoverage` directly, and say so where they do.
    """
    return CalendarCoverage(
        events=events,
        covers_through=WHEN + timedelta(hours=1) if through is None else through,
        fetch_ok=fetch_ok,
        fetch_error=fetch_error,
    )


# --- is_high_impact, one half of the OR at a time ----------------------------


def test_the_quiet_title_really_matches_nothing() -> None:
    """Guards every other test that uses `QUIET_TITLE` to mean "not a keyword".

    `test_neither_the_rating_nor_the_title_leaves_it_out` fails in the same
    scenario, so this is redundant as coverage. It is kept for its failure
    message, which names the keyword that started matching, and it folds with
    `casefold` because that is what `is_high_impact` uses.
    """
    folded = QUIET_TITLE.casefold()

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
    "would match nothing and report every week as clear". Both spellings are
    asserted so neither direction of the fold can
    regress.
    """
    assert "High" in IMPACT_LEVELS
    assert "high" not in IMPACT_LEVELS

    for spelling in ("High", "high", "HIGH"):
        assert is_high_impact(event(title=QUIET_TITLE, impact=spelling)) is True, (
            spelling
        )


def test_a_padded_rating_still_qualifies() -> None:
    """The rating is stripped before it is folded, and nothing pinned that.

    The JSON payload sends the four levels clean, which the committed fixture
    confirms, but the feed publishes the same week as XML and CSV and those go
    through a different parser. A rating arriving as ``" High "`` from one of
    them would otherwise read as not high, which is the silent all-clear this
    module is built to avoid.
    """
    assert is_high_impact(event(title=QUIET_TITLE, impact="  High  ")) is True


def test_a_keyword_in_the_title_alone_qualifies() -> None:
    """Second half of the OR. The feed rates this one low.

    The feeds assign impact editorially, and the view the keyword half of the OR
    rests on is that a mislabelled event still moves the pair. That view is the
    trading plan's rather than a measurement, and `HIGH_IMPACT_KEYWORDS` is the
    plan's own list of events to avoid, which is why either side of the OR is
    enough on its own.
    """
    assert is_high_impact(event(title="US CPI y/y", impact="Low")) is True


def test_neither_the_rating_nor_the_title_leaves_it_out() -> None:
    """The case that has to stay False, or the guard blocks every morning."""
    assert is_high_impact(event(title=QUIET_TITLE, impact="Low")) is False


def test_a_market_closure_is_not_a_high_impact_release() -> None:
    """The fourth value, which nothing here represented until now.

    ``Holiday`` is in `IMPACT_LEVELS` because the feed emits it and out of
    `IMPACT_SEVERITY` because it is not a severity. Neither fact stops a
    consumer treating the string as one, and the direction of that mistake
    matters: read as a severity below ``Low`` it is harmless, read as one above
    ``High`` it blocks every closure. The guard has to return False, and the
    title is held quiet so the rating is the only thing under test.
    """
    assert "Holiday" in IMPACT_LEVELS

    assert is_high_impact(event(title=QUIET_TITLE, impact="Holiday")) is False


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


def test_every_high_rated_row_in_the_committed_feed_qualifies() -> None:
    """The trap checked against 105 real rows rather than against a constant.

    `IMPACT_LEVELS` only claims what the feed sends. The repository holds a
    capture of what it actually sent, and this reads it: sixteen rows are rated
    `High`, and a comparison against the lower-case spelling would qualify none
    of them and report the week clear. It also catches `IMPACT_LEVELS` drifting
    away from the feed while staying self-consistent, which neither of the two
    tests above can.

    No network: the file is committed, and its provenance is in
    ``tests/fixtures/README.md``.
    """
    rows = json.loads(
        (
            Path(__file__).resolve().parent
            / "fixtures"
            / "forexfactory_calendar_thisweek.json"
        ).read_text()
    )
    rated_high = [row for row in rows if row["impact"] == "High"]

    assert len(rows) == 105
    assert len(rated_high) == 16, "the capture's own count, so a re-capture fails"
    for row in rated_high:
        built = CalendarEvent(
            title=row["title"],
            currency=row["country"],
            scheduled_for=datetime.fromisoformat(row["date"]),
            impact=row["impact"],
        )
        assert is_high_impact(built) is True, row["title"]


def test_a_global_event_never_blocks_a_pair_today() -> None:
    """Pinned as it behaves, because nothing pinned it in either direction.

    `fbe.datasources.calendar` maps the feed's "All" country to ``GLOBAL`` and
    hands those events to every request, on the stated grounds that an event
    with no single currency can move any of them. This function matches events
    by leg, so it drops every one of them, which makes
    ``HIGH_IMPACT_KEYWORDS["geopolitical"]`` unreachable on real input: the
    committed capture's one such row is a BRICS summit, published under "All".

    Whether a global event should block every pair is a decision rather than an
    oversight, and it is filed rather than taken here. This test exists so the
    behaviour is stated somewhere rather than merely happening, and so that
    changing it has to change a test that says why.
    """
    summit = event("GLOBAL", WHEN, title="BRICS Summit", impact="Low")

    assert is_high_impact(summit) is True, "the keyword half rates it high"
    assert is_blacked_out("EURUSD", WHEN, covering(summit), CONFIG) == (False, None)


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


def test_a_window_wholly_inside_another_does_not_shorten_it() -> None:
    """Direct on `_merge`, because `blackout_windows` cannot produce this.

    Every window it builds is the same width, ``before + after``, so a later
    window's end is always later and the longer-end branch never runs. It is
    still the branch that makes `_merge` a merge rather than a fold: drop it and
    the merged interval takes the contained window's end, cutting the blackout
    short and reporting the remainder of the real window as tradeable. The
    helper mirrors `fbe.datasources.calendar._merge`, whose caller windows a
    whole currency at once, so the case is one config change or one new caller
    away rather than hypothetical.
    """
    long_window = (WHEN, WHEN + timedelta(hours=2))
    inside = (WHEN + timedelta(minutes=10), WHEN + timedelta(minutes=20))

    assert _merge([long_window, inside]) == (long_window,)


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
    assert (
        list(blackout_windows([event(title=QUIET_TITLE, impact="Low")], CONFIG)) == []
    )


def test_no_events_produce_no_windows() -> None:
    """The empty case, which a merge written around a first element would trip."""
    assert list(blackout_windows([], CONFIG)) == []


def test_an_exhaustible_iterator_of_events_still_produces_its_windows() -> None:
    """The annotation says `Sequence`, so only an untyped caller gets here.

    `blackout_windows` reads its argument twice, once to validate every event's
    timezone and once to build the spans. A generator would be empty by the
    second pass, and the failure is the silent kind: no windows at all from a
    calendar full of releases, reported as a clear week. The materialising line
    is what stops it, so it is pinned rather than trusted.
    """
    events = (item for item in [event(), event("USD", WHEN + timedelta(hours=4))])

    windows = blackout_windows(events, CONFIG)  # type: ignore[arg-type]

    assert len(list(windows)) == 2


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


@pytest.mark.parametrize(
    ("before", "after"),
    [(-30, 60), (30, -60), (-30, -60)],
)
def test_a_negative_blackout_count_is_refused(before: int, after: int) -> None:
    """A sign typo in one config line would switch the guard off silently.

    A negative count inverts the window, so ``opens <= when <= closes`` is
    false at every instant and every moment reads clear, payrolls included.
    Nothing downstream can tell an inverted window from a valid one, and
    `Config.validate` covers no `DataConfig` field, so the refusal belongs to
    the consumer that cannot tell.
    """
    broken = DataConfig(
        calendar_blackout_before_min=before, calendar_blackout_after_min=after
    )

    with pytest.raises(ValueError, match="negative"):
        blackout_windows([event()], broken)


def test_both_counts_at_zero_are_refused() -> None:
    """A window of a single instant can only block an entry timed to the second.

    Which is the same all-clear reached a different way. Switching the guard
    off is a decision to take deliberately rather than by configuring it to
    nothing.
    """
    nothing = DataConfig(calendar_blackout_before_min=0, calendar_blackout_after_min=0)

    with pytest.raises(ValueError, match="zero"):
        blackout_windows([event()], nothing)


def test_one_count_at_zero_is_allowed() -> None:
    """Asymmetric is the point of two counts, so only both at zero is refused.

    Thirty before and nothing after is a defensible setting for a trader who
    re-enters on the first candle close.
    """
    half = DataConfig(calendar_blackout_before_min=30, calendar_blackout_after_min=0)

    assert list(blackout_windows([event()], half)) == [
        (WHEN - timedelta(minutes=30), WHEN)
    ]


def test_the_negative_window_would_otherwise_have_reported_every_moment_clear() -> None:
    """The defect the refusal above prevents, demonstrated on the raw arithmetic.

    Built here rather than through the guard, because the guard now raises. A
    release at the exact moment asked about, an inverted window, and the
    containment test that every caller uses comes back false.
    """
    opens = WHEN + timedelta(minutes=30)
    closes = WHEN - timedelta(minutes=60)

    assert not opens <= WHEN <= closes


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


def test_a_successful_fetch_with_no_horizon_at_all_is_a_gap() -> None:
    """The fourth unknown state, and it was the only line here nothing reached.

    `CalendarSource.horizon` returns `None` for a fetch that succeeded on a week
    holding no event this universe scores, so a real source does build this,
    contrary to what the code comment used to assume. The value matters more
    than most: it is the difference between unknown and clear for a run whose
    calendar came back empty and fine.

    Built directly, because `covering` reads a `None` horizon as "use the
    default".
    """
    empty = CalendarCoverage(events=(), covers_through=None, fetch_ok=True)

    gap = coverage_gap(empty, WHEN)

    assert gap is not None
    category, reason = gap
    assert category is CoverageGap.BEYOND_HORIZON
    assert "no coverage at all" in reason
    assert "refetch" in reason, "retrying helps here, unlike the usual case"
    assert is_blacked_out("EURUSD", WHEN, empty, CONFIG)[0] is None


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

    assert reason == "EUR CPI y/y at 2026-09-14 09:00 UTC"
    # Asserted whole rather than by substring, because a format dropping the
    # calendar date passed the substring version: "09:00" and "UTC" were both
    # still there. The date is what makes the string unambiguous in a report
    # that outlives the terminal, since the guard is asked about an instant
    # while the run carries a date and a release can fall outside it.
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
def test_a_pair_of_the_wrong_length_is_refused(pair: str) -> None:
    """Six characters, two legs. A five-character pair has no honest legs.

    The message is matched because the G10 leg check downstream refuses all four
    of these too, on the leg a slice happens to produce. Asserting only
    ``ValueError`` would pass with `split_pair` taken out altogether, which is
    the canonical splitter and the one place the quoting convention is enforced.
    """
    with pytest.raises(ValueError, match="six-character"):
        is_blacked_out(pair, WHEN, covering(), CONFIG)


@pytest.mark.parametrize("pair", ["XXXYYY", "EURXXX", "XXXUSD"])
def test_a_leg_that_is_not_a_currency_is_refused(pair: str) -> None:
    """`split_pair` checks length only, so a typo splits as cleanly as a pair.

    And then it matches no event, so the guard would answer that the morning is
    clear for a pair that does not exist. `fbe.risk.currency_exposure` refuses
    the same input for the same reason, and the pre-trade check is hand-typed.
    """
    coverage = covering(event("EUR", WHEN))

    with pytest.raises(ValueError, match="not a G10 currency"):
        is_blacked_out(pair, WHEN, coverage, CONFIG)


def test_a_lower_case_pair_is_answered_rather_than_read_as_no_legs() -> None:
    """Uppercased before splitting, as `fbe.risk.currency_exposure` does.

    Without the fold, ``eurusd`` splits into legs matching no event and the
    guard reports clear on a morning it should block, which is the silent
    answer rather than the loud one.
    """
    coverage = covering(event("EUR", WHEN))

    assert is_blacked_out("eurusd", WHEN, coverage, CONFIG)[0] is True
    assert is_blacked_out("eurusd", WHEN, coverage, CONFIG) == is_blacked_out(
        "EURUSD", WHEN, coverage, CONFIG
    )


def test_a_naive_event_is_refused_even_on_a_currency_the_pair_does_not_hold() -> None:
    """A feed sending naive times is broken, whatever this pair happens to be.

    The refusal was conditional on the event sitting on a leg while the window
    builder was the only thing checking, so a naive yen event was silently
    ignored on EURUSD and would have reached a qualifying event on the next
    run. It is checked up front now, before the coverage question.
    """
    naive = CalendarEvent(
        title="Policy Rate",
        currency="JPY",
        scheduled_for=datetime(2026, 9, 14, 9, 0),
        impact="High",
    )

    with pytest.raises(ValueError, match="naive"):
        is_blacked_out("EURUSD", WHEN, covering(naive), CONFIG)


def test_a_zone_that_reports_no_offset_is_refused_like_a_naive_one() -> None:
    """``tzinfo is not None`` is not the same as aware, and the difference bites.

    A tzinfo whose ``utcoffset`` returns `None` passes a ``tzinfo is None``
    check and then raises `TypeError` from inside the interval arithmetic, which
    is loud but says nothing about which field was wrong. Both halves of the
    check are needed and only the first was pinned.
    """

    class Unset(tzinfo):
        """A zone that declines to say what its offset is."""

        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, dt: datetime | None) -> str | None:
            return None

    unset = datetime(2026, 9, 14, 9, 0, tzinfo=Unset())
    assert unset.tzinfo is not None
    assert unset.utcoffset() is None

    with pytest.raises(ValueError, match="naive"):
        blackout_windows([event(scheduled_for=unset)], CONFIG)
    with pytest.raises(ValueError, match="naive"):
        coverage_gap(covering(), unset)


def test_a_naive_coverage_horizon_is_refused() -> None:
    """The one datetime on the dataclass documented as UTC and not enforced.

    Comparing it against an aware ``when`` raises `TypeError` from inside the
    comparison, which is loud but says nothing about which field was wrong, and
    on the stale-cache path it would reach the report as a time string with no
    offset for a trader to act on.
    """
    naive_horizon = CalendarCoverage(
        events=(), covers_through=datetime(2026, 9, 14, 23, 0), fetch_ok=True
    )

    with pytest.raises(ValueError, match="naive"):
        coverage_gap(naive_horizon, WHEN)


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
