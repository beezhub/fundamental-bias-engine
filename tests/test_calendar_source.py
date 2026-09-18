"""Tests for the calendar feed, where an empty answer is the dangerous one.

Every other source in this package fails safe: a missing price leaves a pillar
with less evidence and the coverage figure says so. This one fails the other
way. An empty calendar reads as "nothing is scheduled", which clears every
blackout at once and puts a position into an NFP print. The publisher
rate-limits exports and answers with a "Request Denied" page at HTTP 200, so the
empty answer is not hypothetical, it is the documented failure mode.

So the tests that matter most here are the ones separating three things a bare
sequence cannot tell apart: a week that is genuinely quiet, a fetch that failed,
and a week that simply does not reach as far as the caller is asking about.

The second theme is the offset. The feed publishes US Eastern with an explicit
offset that tracks daylight saving, so a payload spanning the November change
carries both ``-04:00`` and ``-05:00``. Assuming either one puts an event an hour
out, which on a 30-minute blackout window is the difference between standing
aside and trading into the release.

Nothing here reaches the network. The live shape is pinned by one committed
payload, recorded in `tests/fixtures/README.md` with its capture date.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources.base import SourceError
from fbe.datasources.cache import CacheMiss
from fbe.datasources.calendar import (
    AVOID_PATTERNS,
    BLACKOUT_IMPACTS,
    CURRENCY_MAP,
    DEFAULT_MIN_IMPACT,
    FEED_URL,
    IMPACT_LEVELS,
    IMPACT_SEVERITY,
    CalendarSource,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIVE_BODY = (FIXTURES / "forexfactory_calendar_thisweek.json").read_text()

DENIED_BODY = (
    "<html><head><title>Request Denied</title></head>"
    "<body><h1>Request Denied</h1><p>You have exceeded the download limit."
    "</p></body></html>"
)

WEEK_START = datetime(2026, 9, 13, tzinfo=UTC)
WEEK_END = datetime(2026, 9, 20, tzinfo=UTC)


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(cache_dir=tmp_path / "cache")


@pytest.fixture
def source(data_config: DataConfig) -> CalendarSource:
    return CalendarSource(data_config)


def row(
    title: str,
    country: str,
    when: str,
    impact: str = "High",
    **extra: str,
) -> dict[str, str]:
    """One feed row, in the feed's own shape.

    ``when`` is written with its offset exactly as the publisher writes it, so a
    test about the offset can put the offset in the payload rather than in the
    expectation.
    """
    built = {
        "title": title,
        "country": country,
        "date": when,
        "impact": impact,
        "forecast": "",
        "previous": "",
    }
    built.update(extra)
    return built


def serve(rows: list[dict[str, str]]) -> respx.Router:
    """Route the feed URL to a payload, with no network underneath."""
    router = respx.mock(assert_all_called=False)
    router.get(FEED_URL).mock(return_value=httpx.Response(200, text=json.dumps(rows)))
    return router


# --- parsing the six keys ----------------------------------------------------


def test_the_six_keys_parse_into_an_event(source: CalendarSource) -> None:
    """Criterion 1. The shape the module docstring records, as an assertion.

    ``country`` holds an ISO 4217 currency code despite its name, which is the
    one field whose name actively misleads. Reading it as a country code would
    put every event under a code that matches no currency in the universe, and
    the blackout would then never fire for anything.
    """
    payload = [row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00")]

    with serve(payload):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    assert len(events) == 1
    event = events[0]
    assert event.title == "Federal Funds Rate"
    assert event.currency == "USD"
    assert event.impact == "High"
    assert event.source == "forexfactory"
    assert event.scheduled_for == datetime(2026, 9, 16, 18, 0, tzinfo=UTC)


def test_an_all_event_is_returned_for_every_currency(source: CalendarSource) -> None:
    """Criterion 1. ``"All"`` maps to ``GLOBAL`` and belongs to everyone.

    An event with no single currency can move any of them, so filtering it out
    when the caller asks about one currency would silently drop exactly the
    events that are broadest.
    """
    payload = [
        row("G20 Meetings", "All", "2026-09-16T10:00:00-04:00"),
        row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00"),
    ]

    with serve(payload):
        for currency in ("USD", "JPY", "AUD"):
            titles = [
                event.title
                for event in source.events((currency,), WEEK_START, WEEK_END)
            ]
            assert "G20 Meetings" in titles, currency

    assert CURRENCY_MAP["All"] == "GLOBAL"


def test_a_currency_outside_the_universe_is_dropped(source: CalendarSource) -> None:
    """Chinese releases arrive regularly and are not a G10 blackout reason.

    Asserted rather than assumed because the alternative, passing the code
    through, produces events under a currency nothing ever asks about: they
    would sit in the feed forever, never match a request, and never be noticed.
    """
    payload = [
        row("Industrial Production y/y", "CNY", "2026-09-16T22:00:00-04:00"),
        row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00"),
    ]

    with serve(payload):
        every = source.events(tuple(CURRENCY_MAP.values()), WEEK_START, WEEK_END)

    assert "CNY" not in CURRENCY_MAP
    assert [event.currency for event in every] == ["USD"]


# --- the offset, which is the field most likely to be assumed ----------------


def test_the_offset_is_parsed_on_both_sides_of_the_daylight_saving_change(
    source: CalendarSource,
) -> None:
    """Criterion 2, and the reason it names both sides.

    US Eastern is ``-04:00`` in September and ``-05:00`` in November. A parser
    that hardcodes either one puts half the year's events an hour out, in the
    same direction, which against a 30-minute window is the difference between
    standing aside and trading straight into the release.

    Worked by hand: 14:00 at ``-04:00`` is 18:00 UTC, and 14:00 at ``-05:00`` is
    19:00 UTC. Same wall clock, different instant.
    """
    payload = [
        row("Summer Release", "USD", "2026-09-16T14:00:00-04:00"),
        row("Winter Release", "USD", "2026-11-18T14:00:00-05:00"),
    ]

    with serve(payload):
        events = source.events(
            ("USD",),
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 12, 1, tzinfo=UTC),
        )

    by_title = {event.title: event.scheduled_for for event in events}
    assert by_title["Summer Release"] == datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
    assert by_title["Winter Release"] == datetime(2026, 11, 18, 19, 0, tzinfo=UTC)


def test_every_event_comes_back_in_utc(source: CalendarSource) -> None:
    """Not merely aware: UTC specifically, so a comparison cannot surprise.

    Two aware datetimes in different zones compare correctly, so this is not
    about correctness of comparison. It is about the report and the journal,
    where an event printed in the publisher's zone reads as an hour that does
    not match anything else the engine prints.
    """
    payload = [row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00")]

    with serve(payload):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    assert events[0].scheduled_for.tzinfo is UTC


# --- impact, where Holiday is the trap ---------------------------------------


def test_a_holiday_is_not_swept_in_as_a_low_impact_event(
    source: CalendarSource,
) -> None:
    """Criterion 3. A closure is not a release and does not belong in the ramp.

    ``Holiday`` sits in `IMPACT_LEVELS` because the feed emits it, not because
    it is a severity. Ordering it below ``Low`` would mean a caller asking for
    everything down to low impact receives market closures as though they were
    releases, and a closure thins the book rather than spiking it, which calls
    for the opposite response.
    """
    payload = [
        row("Bank Holiday", "GBP", "2026-09-14T00:00:00-04:00", impact="Holiday"),
        row("Retail Sales m/m", "GBP", "2026-09-15T02:00:00-04:00", impact="Low"),
    ]

    with serve(payload):
        down_to_low = source.events(("GBP",), WEEK_START, WEEK_END, min_impact="Low")
        holidays = source.events(("GBP",), WEEK_START, WEEK_END, min_impact="Holiday")

    assert [event.title for event in down_to_low] == ["Retail Sales m/m"]
    assert [event.title for event in holidays] == ["Bank Holiday"]
    assert "Holiday" in IMPACT_LEVELS
    assert "Holiday" not in BLACKOUT_IMPACTS


def test_min_impact_includes_everything_at_or_above_it(
    source: CalendarSource,
) -> None:
    """Criterion 5, the impact filter, asserted at all three severities.

    Each level has to include the ones above it. A filter that matched only the
    level asked for would answer a request for medium-and-worse with the medium
    events alone, dropping the high ones, which is the failure that looks like
    a working filter right up until it matters.
    """
    payload = [
        row("High One", "USD", "2026-09-16T12:00:00-04:00", impact="High"),
        row("Medium One", "USD", "2026-09-16T13:00:00-04:00", impact="Medium"),
        row("Low One", "USD", "2026-09-16T14:00:00-04:00", impact="Low"),
    ]

    with serve(payload):
        titles = {
            level: [
                event.title
                for event in source.events(
                    ("USD",), WEEK_START, WEEK_END, min_impact=level
                )
            ]
            for level in ("High", "Medium", "Low")
        }

    assert titles["High"] == ["High One"]
    assert titles["Medium"] == ["High One", "Medium One"]
    assert titles["Low"] == ["High One", "Medium One", "Low One"]


def test_an_unknown_impact_raises_rather_than_being_dropped(
    source: CalendarSource,
) -> None:
    """A severity this module does not know is a schema change, not a row to skip.

    Dropping it would silently remove events from the blackout the moment the
    publisher renamed a level, and the engine would report a clear week. The
    four levels are pinned in `IMPACT_LEVELS` precisely so that a fifth is
    loud.
    """
    payload = [row("Mystery", "USD", "2026-09-16T14:00:00-04:00", impact="Critical")]

    with serve(payload), pytest.raises(SourceError, match="impact"):
        source.events(("USD",), WEEK_START, WEEK_END)


# --- the display strings -----------------------------------------------------


def test_the_display_strings_are_never_coerced_to_numbers(
    source: CalendarSource,
) -> None:
    """Criterion 4. ``"-1.2K"`` and ``"3.75%"`` survive as written.

    These are for a person to read beside the event. Parsing them would mean
    deciding what ``K`` means, what a bare ``%`` is a percentage of, and what to
    do with ``"<1.25%"``, which the live payload carries on the BOJ policy rate.
    None of those has an answer this module needs, so it does not guess at one.
    """
    payload = [
        row(
            "Claimant Count Change",
            "GBP",
            "2026-09-15T02:00:00-04:00",
            forecast="-1.2K",
            previous="3.75%",
            actual="<1.25%",
        )
    ]

    with serve(payload):
        event = source.events(("GBP",), WEEK_START, WEEK_END)[0]

    assert event.forecast == "-1.2K"
    assert event.previous == "3.75%"
    assert event.actual == "<1.25%"


def test_actual_is_absent_until_the_release_lands(source: CalendarSource) -> None:
    """Criterion 4, the other half. No key means ``None``, not an empty string.

    The captured payload carries no ``actual`` key on any of its 105 rows, so
    the absent case is the normal one and the present case is the exception.
    An empty string here would read as a released figure that happened to be
    blank.
    """
    payload = [
        row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00"),
        row(
            "Already Out",
            "USD",
            "2026-09-15T14:00:00-04:00",
            actual="4.00%",
        ),
    ]

    with serve(payload):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    by_title = {event.title: event.actual for event in events}
    assert by_title["Federal Funds Rate"] is None
    assert by_title["Already Out"] == "4.00%"


def test_an_empty_display_string_becomes_an_absence(source: CalendarSource) -> None:
    """The feed writes ``""`` for a figure it has not got, and that is not a value.

    `CalendarEvent` types all three as ``str | None``, and ``None`` is this
    codebase's marked absence. Carrying ``""`` through would put an empty cell
    in the report that a reader cannot tell from a figure the publisher chose
    not to forecast.
    """
    payload = [row("FOMC Statement", "USD", "2026-09-16T14:00:00-04:00")]

    with serve(payload):
        event = source.events(("USD",), WEEK_START, WEEK_END)[0]

    assert event.forecast is None
    assert event.previous is None


# --- filtering and ordering --------------------------------------------------


def test_events_are_filtered_by_currency(source: CalendarSource) -> None:
    """Criterion 5, the currency filter."""
    payload = [
        row("US Release", "USD", "2026-09-16T12:00:00-04:00"),
        row("UK Release", "GBP", "2026-09-16T13:00:00-04:00"),
    ]

    with serve(payload):
        events = source.events(("GBP",), WEEK_START, WEEK_END)

    assert [event.title for event in events] == ["UK Release"]


def test_events_are_filtered_by_window(source: CalendarSource) -> None:
    """Criterion 5, the window filter, asserted on both edges.

    The window is half-open at the end and closed at the start, which is stated
    in the docstring: an event exactly at ``start`` is inside the window a
    caller asked about, and one exactly at ``end`` belongs to the next window
    rather than being counted twice.
    """
    payload = [
        row("Before", "USD", "2026-09-16T09:59:00-04:00"),
        row("At Start", "USD", "2026-09-16T10:00:00-04:00"),
        row("At End", "USD", "2026-09-16T12:00:00-04:00"),
        row("After", "USD", "2026-09-16T12:01:00-04:00"),
    ]

    with serve(payload):
        events = source.events(
            ("USD",),
            datetime(2026, 9, 16, 14, 0, tzinfo=UTC),
            datetime(2026, 9, 16, 16, 0, tzinfo=UTC),
        )

    assert [event.title for event in events] == ["At Start"]


def test_events_come_back_ordered_by_time(source: CalendarSource) -> None:
    """Criterion 5, the ordering, on a payload deliberately out of order.

    The live feed arrives sorted, which is exactly why this is asserted on a
    shuffled payload: a parser that relies on the publisher's ordering works
    until the day it does not, and the failure is a blackout window built from
    the wrong end of the week.
    """
    payload = [
        row("Third", "USD", "2026-09-17T14:00:00-04:00"),
        row("First", "USD", "2026-09-15T14:00:00-04:00"),
        row("Second", "USD", "2026-09-16T14:00:00-04:00"),
    ]

    with serve(payload):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    assert [event.title for event in events] == ["First", "Second", "Third"]


# --- the failure that must never look like a quiet week ----------------------


def test_the_request_denied_page_raises(source: CalendarSource) -> None:
    """Criterion 6, and the reason this module exists in the shape it does.

    The publisher answers a rate-limited export with HTTP 200 and an HTML page.
    Parsed as a week, that is zero events, which clears every blackout at once.
    The message names the rate limit because that is the actionable cause: the
    fix is to wait and use the cache, not to retry harder.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(return_value=httpx.Response(200, text=DENIED_BODY))
        with pytest.raises(SourceError, match="rate"):
            source.events(("USD",), WEEK_START, WEEK_END)


def test_a_denied_body_is_not_cached(
    source: CalendarSource, data_config: DataConfig
) -> None:
    """A refusal kept on disk would serve itself back for the whole TTL.

    `BaseDataSource._request` writes the cache only after decoding succeeds, so
    refusing inside `_decode` is what keeps the page off disk. An offline run
    expires nothing, so one denied export cached here would be permanent until
    somebody deleted the file by hand.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(return_value=httpx.Response(200, text=DENIED_BODY))
        with pytest.raises(SourceError):
            source.events(("USD",), WEEK_START, WEEK_END)

    # Asserted on the cache itself, not on the second raise. An offline source
    # with an empty cache raises on the miss anyway, so a test that only
    # checked for an exception would pass whether or not the page was written.
    with pytest.raises(CacheMiss):
        source.cache.get(source.name, source._cache_key(source.feed_path, {}))

    offline = CalendarSource(replace(data_config, offline=True))
    with pytest.raises(SourceError):
        offline.events(("USD",), WEEK_START, WEEK_END)


def test_a_repeated_failure_raises_rather_than_returning_nothing(
    source: CalendarSource,
) -> None:
    """Criterion 7. An exhausted retry is not a week with nothing in it.

    This is the same rule as the denied page from the other direction: there,
    the body was wrong; here, there is no body. Both have to reach the caller
    as an error, because the one thing neither may become is an empty sequence.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(SourceError):
            source.events(("USD",), WEEK_START, WEEK_END)


def test_the_cached_week_is_still_served_offline(
    source: CalendarSource, data_config: DataConfig
) -> None:
    """Criterion 7, the other half. A schedule downloaded Monday is good Thursday.

    This is the one source where a cached copy is as good as a live one, which
    is why the publisher's own guidance is to download once and work from the
    copy. The offline run must reach the same events without a request.
    """
    payload = [row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00")]

    with serve(payload):
        first = source.events(("USD",), WEEK_START, WEEK_END)

    offline = CalendarSource(replace(data_config, offline=True))
    second = offline.events(("USD",), WEEK_START, WEEK_END)

    assert [event.title for event in second] == [event.title for event in first]
    assert second[0].scheduled_for == first[0].scheduled_for


def test_a_quiet_week_is_an_empty_sequence_and_not_an_error(
    source: CalendarSource,
) -> None:
    """The third of the three states, without which the other two prove nothing.

    A week with no events matching the request is data. If this raised, the
    tests above would pass against a source that treated everything as a
    failure, and the distinction they exist to draw would be untested.
    """
    with serve([]):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    assert events == ()


# --- how far the week reaches ------------------------------------------------


def test_the_horizon_reports_how_far_the_week_reaches(
    source: CalendarSource,
) -> None:
    """Criterion 8. A Friday run holding only this week must look short.

    The feed publishes the current week and nothing else, so a run late in the
    week can see nothing about Monday. Reporting only events would make that
    indistinguishable from a Monday with nothing scheduled, and the guard would
    clear a blackout it has no evidence about.
    """
    payload = [
        row("Early", "USD", "2026-09-14T14:00:00-04:00"),
        row("Late", "JPY", "2026-09-17T22:54:00-04:00"),
    ]

    with serve(payload):
        horizon = source.horizon()

    assert horizon == datetime(2026, 9, 18, 2, 54, tzinfo=UTC)


def test_the_horizon_is_an_absence_when_nothing_is_usable(
    source: CalendarSource,
) -> None:
    """A failed fetch with no cache under it vouches for no moment at all.

    ``None`` rather than the epoch or the current time. A horizon in the past
    would read as a week that has expired, and a horizon of now would read as
    a week that covers this instant and no further, and both of those are
    claims this source cannot make when it has nothing.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(side_effect=httpx.ConnectError("no route"))
        assert source.horizon() is None


def test_the_horizon_does_not_reach_past_the_events_it_holds(
    source: CalendarSource,
) -> None:
    """The horizon is evidence, not the calendar week it was drawn from.

    The feed carries no field saying how far it runs, so the only thing this
    source can vouch for is the span it actually holds. Asserting a full week
    from a payload ending on Tuesday would be claiming coverage of three days
    it has never seen.
    """
    payload = [row("Tuesday Only", "USD", "2026-09-15T14:00:00-04:00")]

    with serve(payload):
        horizon = source.horizon()

    assert horizon == datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
    assert horizon < WEEK_END


# --- classification ----------------------------------------------------------


def test_a_title_matching_two_classes_returns_both(source: CalendarSource) -> None:
    """Criterion 9, on the example the issue names.

    "Unemployment Rate" is in both ``employment_other`` and, on a payrolls
    Friday, the reason a trader stands aside is the payrolls print beside it.
    Returning one key would make the report name a single reason for an event
    that has two, and the trader reading it would not know the other applied.
    """
    # The issue's example, "Unemployment Rate", matches exactly one class in
    # the shipped `AVOID_PATTERNS` and so cannot demonstrate what the criterion
    # asks for. The titles that genuinely match two are the payrolls ones,
    # through "non-farm employment change" under `nfp` and "employment change"
    # under `employment_other`. "ADP Weekly Employment Change" is one of them
    # and it is in the committed payload, so this is a real title rather than
    # an invented one.
    assert source.classify("Unemployment Rate") == ("employment_other",)

    both = source.classify("ADP Weekly Employment Change")

    assert both == ("nfp", "employment_other")
    assert len(both) == 2
    assert "ADP Weekly Employment Change" in {
        entry["title"] for entry in json.loads(LIVE_BODY)
    }


def test_classification_is_case_insensitive(source: CalendarSource) -> None:
    """The feed capitalises titles and the patterns are lower case.

    Matching without folding the case would return nothing for every real
    title, which is a classifier that silently never fires: the report would
    show events with no class against any of them and read as though the plan's
    ten classes never occurred.
    """
    assert source.classify("Federal Funds Rate") == source.classify(
        "FEDERAL FUNDS RATE"
    )
    assert "rate_decision" in source.classify("Federal Funds Rate")


def test_an_unmatched_title_returns_empty(source: CalendarSource) -> None:
    """Empty, not a catch-all key.

    Most of a week is low-impact noise that belongs to none of the plan's ten
    classes. Filing those under a default class would make the blackout list
    the whole feed.
    """
    assert source.classify("BusinessNZ Services Index") == ()


def test_every_avoid_class_matches_at_least_one_live_title() -> None:
    """The patterns were drawn from real titles, so real titles must match them.

    A pattern that matches nothing in a live week is either a wording that has
    changed or one that was guessed. This does not require every class to
    appear in one week, which would be false, but it does require that the
    classes which do appear are matched rather than missed.
    """
    source = CalendarSource(DataConfig())
    titles = [entry["title"] for entry in json.loads(LIVE_BODY)]

    matched: set[str] = set()
    for title in titles:
        matched.update(source.classify(title))

    assert "rate_decision" in matched
    assert "inflation" in matched
    assert "gdp" in matched


# --- blackout windows --------------------------------------------------------


def test_overlapping_windows_are_merged(
    source: CalendarSource, data_config: DataConfig
) -> None:
    """Three releases thirty minutes apart are one window, not three.

    A caller handed three overlapping intervals has to reconcile them before it
    can answer a single question, and every caller would do it the same way.
    Doing it once here means the guard reads a list it can scan rather than a
    set it has to normalise.

    The live payload has exactly this shape: the FOMC statement, the rate and
    the projections all land at 18:00 UTC, with the press conference thirty
    minutes later.
    """
    payload = [
        row("FOMC Statement", "USD", "2026-09-16T14:00:00-04:00"),
        row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00"),
        row("FOMC Press Conference", "USD", "2026-09-16T14:30:00-04:00"),
    ]

    with serve(payload):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    windows = source.blackout_windows(events)

    assert list(windows) == ["USD"]
    assert len(windows["USD"]) == 1
    opened, closed = windows["USD"][0]
    assert opened == datetime(2026, 9, 16, 17, 30, tzinfo=UTC)
    assert closed == datetime(2026, 9, 16, 19, 30, tzinfo=UTC)


def test_the_window_widths_come_from_config(data_config: DataConfig) -> None:
    """The two widths are read, not retyped, which is the config-drift trap.

    `DataConfig` defaults to 30 and 60, so a test that builds its expectation
    from `data_config` on a default config is comparing the literal against
    itself and passes against an implementation with 30 and 60 written into it.
    This one widens both and asserts the boundaries move, which is the only
    shape that can fail.

    The standards table names this exact pattern: the risk ladder hardcoding 1%
    and 2% beside a config that held them. An owner widening the window for an
    FOMC week would otherwise get the old window and a report claiming the new
    one.
    """
    widened = replace(
        data_config,
        calendar_blackout_before_min=45,
        calendar_blackout_after_min=90,
    )
    source = CalendarSource(widened)
    payload = [row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00")]

    with serve(payload):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    opened, closed = source.blackout_windows(events)["USD"][0]

    assert opened == datetime(2026, 9, 16, 17, 15, tzinfo=UTC)
    assert closed == datetime(2026, 9, 16, 19, 30, tzinfo=UTC)


def test_touching_windows_merge(source: CalendarSource) -> None:
    """A window ending exactly when the next begins leaves no tradeable instant.

    `_merge` publishes this in its docstring and nothing held it. Two events 90
    minutes apart at the default 30 and 60 give ``(17:30, 19:00)`` and
    ``(19:00, 20:30)``, which touch at 19:00. Reporting two windows would invite
    a caller to read 19:00 as a gap, and a caller looking for a moment to enter
    would find one that does not exist.

    `test_windows_far_apart_stay_separate` uses events three days apart, so it
    says nothing about this boundary.
    """
    payload = [
        row("First", "USD", "2026-09-16T14:00:00-04:00"),
        row("Second", "USD", "2026-09-16T15:30:00-04:00"),
    ]

    with serve(payload):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    windows = source.blackout_windows(events)

    assert len(windows["USD"]) == 1
    assert windows["USD"][0] == (
        datetime(2026, 9, 16, 17, 30, tzinfo=UTC),
        datetime(2026, 9, 16, 20, 30, tzinfo=UTC),
    )


def test_windows_far_apart_stay_separate(source: CalendarSource) -> None:
    """Merging must not run the whole week together.

    Without this the test above passes against an implementation that returns
    one window per currency spanning the first event to the last, which would
    black out every pair for five days.
    """
    payload = [
        row("Monday", "USD", "2026-09-14T14:00:00-04:00"),
        row("Thursday", "USD", "2026-09-17T14:00:00-04:00"),
    ]

    with serve(payload):
        events = source.events(("USD",), WEEK_START, WEEK_END)

    windows = source.blackout_windows(events)

    assert len(windows["USD"]) == 2


def test_windows_are_kept_apart_by_currency(source: CalendarSource) -> None:
    """A dollar release does not black out the yen on its own.

    Whether a *pair* is blacked out by one leg is the guard's question, and it
    reads this mapping to answer it. Merging across currencies here would take
    that decision away from the guard and make every release global.
    """
    payload = [
        row("US Release", "USD", "2026-09-16T14:00:00-04:00"),
        row("JP Release", "JPY", "2026-09-16T14:00:00-04:00"),
    ]

    with serve(payload):
        events = source.events(("USD", "JPY"), WEEK_START, WEEK_END)

    windows = source.blackout_windows(events)

    assert set(windows) == {"USD", "JPY"}
    assert len(windows["USD"]) == 1
    assert len(windows["JPY"]) == 1


# --- the protocol methods that do nothing ------------------------------------


def test_this_source_produces_no_observations(source: CalendarSource) -> None:
    """It feeds the execution filter, not a pillar.

    Asserted so the collector can call it like every other source without a
    special case, and so a later change that started emitting observations from
    here has to be deliberate.
    """
    assert source.fetch((), (), date(2026, 9, 1), date(2026, 9, 30)) == ()
    assert source.refs() == {}


def test_available_answers_without_touching_the_network(
    source: CalendarSource,
) -> None:
    """Criterion 10, against the contract the base class actually enforces.

    `tests/test_datasource_base.py` holds every source to "availability is a
    question about configuration, not connectivity" and asserts no request is
    made. So an online run answers ``True`` on configuration alone: this feed
    needs no credential and no directory, and whether it is reachable is
    `events`' question, which is the only place a failed fetch can be told from
    a quiet week.

    The stub's own docstring said "whether the feed is reachable", which is the
    check the base contract forbids. The contract wins and the issue records
    why.
    """
    with respx.mock(assert_all_called=False) as router:
        route = router.get(FEED_URL).mock(
            return_value=httpx.Response(200, text=DENIED_BODY)
        )
        assert source.available() is True
        assert route.call_count == 0


def test_available_offline_reads_the_cached_body_rather_than_its_presence(
    source: CalendarSource, data_config: DataConfig
) -> None:
    """Criterion 10's shape check, on the only response this can inspect offline.

    Offline there is no request, so the response whose shape is checked is the
    one already on disk. A body that is the publisher's rate-limit page cannot
    normally get there, because `_decode` refuses before the cache is written,
    but an older version or a hand-edited file could leave one. Answering
    ``True`` for it would promise a week that `events` then refuses, which is
    the same mismatch between "available" and "usable" that a status-code check
    produces online.
    """
    offline = CalendarSource(replace(data_config, offline=True))
    assert offline.available() is False

    with serve([row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00")]):
        source.events(("USD",), WEEK_START, WEEK_END)
    assert offline.available() is True

    # Written through the online instance, because the cache refuses a write
    # from an offline run: a run that writes its own inputs is not reproducible.
    # That guard is why a poisoned entry can only arrive from an earlier online
    # run or by hand, which is exactly the case being covered.
    source.cache.put(
        source.name,
        source._cache_key(source.feed_path, {}),
        DENIED_BODY.encode(),
        {},
    )
    assert offline.available() is False


# --- the live payload --------------------------------------------------------


def test_the_committed_payload_holds_the_shape_the_docstring_records() -> None:
    """Criterion 11's premise, asserted before anything is read from the fixture.

    The module docstring makes several claims about the live feed: six keys, an
    ISO 4217 code under ``country``, the literal ``"All"``, capitalised impacts,
    an explicit offset, and 80 to 100 rows. If the capture stops matching those,
    the spot check below is pinning a shape the docstring no longer describes.
    """
    rows = json.loads(LIVE_BODY)

    assert 80 <= len(rows) <= 110
    assert {key for entry in rows for key in entry} == {
        "title",
        "country",
        "date",
        "impact",
        "forecast",
        "previous",
    }
    assert {entry["impact"] for entry in rows} <= set(IMPACT_LEVELS)
    assert "All" in {entry["country"] for entry in rows}
    assert all(entry["date"].endswith(("-04:00", "-05:00")) for entry in rows)


def test_one_live_event_spot_checked_end_to_end(source: CalendarSource) -> None:
    """Criterion 11. One event from the captured payload, through the real parser.

    The FOMC rate decision on 2026-09-16, published as 14:00 at ``-04:00``,
    which is 18:00 UTC. Chosen because it is the highest-consequence event in
    the week, it classifies as a rate decision, and its offset is the one the
    parser has to read rather than assume.

    A spot check is what breaks the build when the publisher changes a field
    underneath us. Everything else in this file uses payloads written here, and
    those cannot notice a change in the live shape.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(return_value=httpx.Response(200, text=LIVE_BODY))
        events = source.events(
            ("USD",),
            datetime(2026, 9, 16, 17, 0, tzinfo=UTC),
            datetime(2026, 9, 16, 19, 0, tzinfo=UTC),
        )

    titles = {event.title: event for event in events}
    decision = titles["Federal Funds Rate"]
    assert decision.currency == "USD"
    assert decision.impact == "High"
    assert decision.scheduled_for == datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
    assert decision.forecast == "4.00%"
    assert decision.previous == "3.75%"
    assert decision.actual is None
    assert "rate_decision" in source.classify(decision.title)


def test_the_module_records_that_the_feed_is_unofficial() -> None:
    """Criterion 12. The notice is already there and must stay there.

    The publisher is not affiliated with this project and has not endorsed it.
    That sentence is a term of use rather than a courtesy, and a later edit
    tidying the module docstring should fail here rather than quietly removing
    it.
    """
    import fbe.datasources.calendar as module

    text = (module.__doc__ or "").lower()
    assert "unofficial" in text
    assert "not affiliated" in text


# --- what the review pass found these could not see --------------------------


def test_the_default_min_impact_is_high(source: CalendarSource) -> None:
    """Criterion 5's "defaulting to High", which nothing held.

    Every other test either passes ``min_impact`` explicitly or builds rows that
    are already high impact, so the default could drift to ``Low`` and the file
    would stay green. A caller that omits the argument would then black out most
    of the London session on most days, which for a trader limited to a few
    positions means never trading.
    """
    payload = [
        row("High One", "USD", "2026-09-16T12:00:00-04:00", impact="High"),
        row("Medium One", "USD", "2026-09-16T13:00:00-04:00", impact="Medium"),
        row("Low One", "USD", "2026-09-16T14:00:00-04:00", impact="Low"),
    ]

    with serve(payload):
        defaulted = source.events(("USD",), WEEK_START, WEEK_END)

    assert [event.title for event in defaulted] == ["High One"]


def test_a_date_with_no_offset_raises(source: CalendarSource) -> None:
    """The documented raise with the most plausible substitute behind it.

    `_event` lists a naive ``date`` as a `SourceError` case and explains why, and
    nothing held it. Reading it as UTC would move every event four or five hours
    in one direction, silently, and against a 30-minute window that is a
    blackout firing for the wrong half-hour on every row of the week.
    """
    payload = [row("Federal Funds Rate", "USD", "2026-09-16T14:00:00")]

    with serve(payload), pytest.raises(SourceError, match="offset"):
        source.events(("USD",), WEEK_START, WEEK_END)


def test_a_payload_that_is_not_an_array_raises(source: CalendarSource) -> None:
    """The denied page from the other direction, and equally untested.

    The publisher can answer HTTP 200 with a JSON object: an error envelope or a
    maintenance notice. Parsed as a week that is zero events, which clears every
    blackout at once. The HTML refusal is well covered and this one was not
    covered at all.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(
            return_value=httpx.Response(200, json={"error": "temporarily down"})
        )
        with pytest.raises(SourceError, match="array"):
            source.events(("USD",), WEEK_START, WEEK_END)


def test_a_row_that_is_not_an_object_raises(source: CalendarSource) -> None:
    """Per row, the same failure as the payload one above."""
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(
            return_value=httpx.Response(200, json=["not an object"])
        )
        with pytest.raises(SourceError, match="not an object"):
            source.events(("USD",), WEEK_START, WEEK_END)


def test_a_row_missing_a_required_key_raises(source: CalendarSource) -> None:
    """A renamed field must stop the run, not empty the week.

    If the publisher renamed ``title`` to ``name``, skipping the unreadable rows
    would skip all of them, `events` would return nothing, and the week would
    read as clear. That is the module's whole failure mode arriving through the
    quietest possible door.
    """
    payload = [
        {"country": "USD", "date": "2026-09-16T14:00:00-04:00", "impact": "High"}
    ]

    with serve(payload), pytest.raises(SourceError, match="title"):
        source.events(("USD",), WEEK_START, WEEK_END)


def test_an_unknown_min_impact_raises(source: CalendarSource) -> None:
    """Criterion 5's guard. An unrecognised level must not match nothing quietly.

    Without it the severity lookup misses, the fallback compares for equality
    against a string no event carries, and the run reports a clear week, which
    is the one answer this module may never give by accident.
    """
    with pytest.raises(ValueError, match="min_impact"):
        source.events(("USD",), WEEK_START, WEEK_END, min_impact="Critical")


def test_min_impact_accepts_the_spelling_the_cli_already_uses(
    source: CalendarSource,
) -> None:
    """`fbe.cli.Impact` spells these in lower case and the feed capitalises them.

    Two vocabularies for one idea already exist in this repository, so the
    filter folds the case rather than making the first caller discover the
    mismatch as a crash. `Impact.HIGH` is ``"high"``, and before this it raised.
    """
    payload = [
        row("High One", "USD", "2026-09-16T12:00:00-04:00", impact="High"),
        row("Low One", "USD", "2026-09-16T14:00:00-04:00", impact="Low"),
    ]

    with serve(payload):
        folded = source.events(("USD",), WEEK_START, WEEK_END, min_impact="high")
        shouted = source.events(("USD",), WEEK_START, WEEK_END, min_impact="HIGH")

    assert [event.title for event in folded] == ["High One"]
    assert [event.title for event in shouted] == ["High One"]


def test_the_retry_actually_retries(source: CalendarSource) -> None:
    """Criterion 7 says "repeated", and nothing asserted more than one attempt.

    A single-attempt implementation satisfies a test that only checks the raise.
    The retry exists because a transport failure is the case worth retrying, and
    on a feed fetched a few times a day a lost connection that is never retried
    means a run with no calendar at all.
    """
    with respx.mock(assert_all_called=False) as router:
        route = router.get(FEED_URL).mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(SourceError):
            source.events(("USD",), WEEK_START, WEEK_END)

    assert route.call_count > 1


def test_classification_comes_back_in_a_stable_order(source: CalendarSource) -> None:
    """`classify` promises `AVOID_PATTERNS` order and nothing held it.

    Two runs over the same title must produce the same tuple, or a report
    reorders its own reasons between runs and a reader comparing yesterday's
    output with today's sees a change that is not one.
    """
    ordered = tuple(AVOID_PATTERNS)
    matched = source.classify("ADP Weekly Employment Change")

    assert list(matched) == [key for key in ordered if key in set(matched)]


def test_every_named_class_is_present_and_the_bank_patterns_match(
    source: CalendarSource,
) -> None:
    """The pattern table is the classifier, and it was barely pinned.

    `AVOID_PATTERNS` is ten classes because the plan names ten. Dropping one, or
    thinning the rate-decision list, leaves a classifier that still works on the
    titles the other tests happen to use while silently missing the highest
    consequence events of the week. The three central banks in the committed
    payload are the case that matters: their wordings differ, so matching one is
    no evidence for the others.
    """
    assert len(AVOID_PATTERNS) == 10
    assert set(AVOID_PATTERNS) == {
        "nfp",
        "rate_decision",
        "gdp",
        "inflation",
        "retail_sales",
        "employment_other",
        "trade_balance",
        "central_bank_speech",
        "minutes",
        "geopolitical",
    }

    for title in ("Federal Funds Rate", "Official Bank Rate", "BOJ Policy Rate"):
        assert "rate_decision" in source.classify(title), title


def test_the_horizon_from_the_committed_payload(source: CalendarSource) -> None:
    """The fixture as a worked example for the horizon, which had none.

    Drawn from every event the week holds rather than from the high-impact ones.
    The last row in this capture is ECOFIN Meetings, low impact, at
    2026-09-19T10:15Z, while the last high-impact row is 28.75 hours earlier. A
    horizon taken from the high-impact subset would understate the coverage by
    more than a day and the guard would report "unknown" for a Friday it can
    actually vouch for.

    Understating is the safe direction, which is why this is coverage rather
    than a defect, but a whole day of it is worth pinning.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(return_value=httpx.Response(200, text=LIVE_BODY))
        horizon = source.horizon()

    assert horizon == datetime(2026, 9, 19, 10, 15, tzinfo=UTC)


def test_an_unknown_currency_raises_rather_than_reporting_a_clear_week(
    source: CalendarSource,
) -> None:
    """The same rule as the impact guard, applied to the request instead.

    A caller that passes a pair string, or lower-cases a code, would otherwise
    match nothing and receive a clear calendar. Worse, a week holding any
    ``GLOBAL`` row comes back non-empty, so the result looks populated while
    every currency-specific event has silently vanished.

    `_event` already refuses an impact it does not recognise and says why.
    Nothing was applying that reasoning to what the caller asked for.
    """
    payload = [row("Federal Funds Rate", "USD", "2026-09-16T14:00:00-04:00")]

    for bad in ("usd", "EURUSD", "SEK"):
        with serve(payload), pytest.raises(ValueError, match="not currencies"):
            source.events((bad,), WEEK_START, WEEK_END)


def test_a_naive_window_bound_raises(source: CalendarSource) -> None:
    """A naive bound renders two different ways depending on the payload.

    Against a week with events it raises `TypeError` from the comparison, which
    no caller catching `SourceError` expects. Against a week that parses to
    nothing the comparison never runs and the result is an empty sequence, so
    the same caller bug reads as a quiet week. `fbe.calendar_guard` documents a
    `ValueError` for naive datetimes and this source was the odd one out.
    """
    naive = datetime(2026, 9, 13)

    with pytest.raises(ValueError, match="timezone-aware"):
        source.events(("USD",), naive, WEEK_END)

    with pytest.raises(ValueError, match="timezone-aware"):
        source.events(("USD",), WEEK_START, naive)


def test_transposed_window_bounds_raise(source: CalendarSource) -> None:
    """Start after end matches nothing, which would read as a clear week.

    An empty window is not a question worth answering, and answering it with an
    empty sequence is the failure this module exists to prevent wearing the
    clothes of an ordinary result.
    """
    with pytest.raises(ValueError, match="after start"):
        source.events(("USD",), WEEK_END, WEEK_START)


def test_the_default_min_impact_follows_the_blackout_policy(
    source: CalendarSource,
) -> None:
    """`BLACKOUT_IMPACTS` and the default are one decision, not two.

    Before this, the threshold was stated in `BLACKOUT_IMPACTS` and written
    again as a literal default, and nothing read the first. Widening the policy
    to include ``Medium`` would have left every call that does not override the
    argument still filtering at ``High``, so the stated policy and the shipped
    behaviour would disagree with nothing to catch it.
    """
    assert DEFAULT_MIN_IMPACT in BLACKOUT_IMPACTS
    assert (
        min(BLACKOUT_IMPACTS, key=lambda level: IMPACT_SEVERITY[level])
        == DEFAULT_MIN_IMPACT
    )

    payload = [
        row("High One", "USD", "2026-09-16T12:00:00-04:00", impact="High"),
        row("Medium One", "USD", "2026-09-16T13:00:00-04:00", impact="Medium"),
    ]

    with serve(payload):
        defaulted = source.events(("USD",), WEEK_START, WEEK_END)
        explicit = source.events(
            ("USD",), WEEK_START, WEEK_END, min_impact=DEFAULT_MIN_IMPACT
        )

    assert [event.title for event in defaulted] == [event.title for event in explicit]


def test_the_horizon_is_absent_for_a_week_that_fetched_but_holds_nothing(
    source: CalendarSource,
) -> None:
    """``None`` means two things, and the docstring used to name only one.

    A fetch that failed and a week that came back genuinely empty both leave
    this source with no moment it can vouch for, so both answer ``None``. They
    are different facts and the guard tells them apart by whether `events`
    raised, not by this value, which is why `horizon` carries no opinion about
    which happened.

    Recorded as a test because the pairing is easy to read the wrong way round:
    a reader seeing ``None`` and reaching for "the fetch failed" would be wrong
    a third of the time.
    """
    with serve([]):
        assert source.horizon() is None

    with serve([row("Chinese Release", "CNY", "2026-09-16T22:00:00-04:00")]):
        assert source.horizon() is None


def test_an_event_already_past_is_outside_a_window_that_starts_now(
    source: CalendarSource,
) -> None:
    """The composition trap, pinned so it is known rather than discovered.

    `events` filters on the event instant, so a caller asking from ``now``
    forward does not receive a release that has already printed, even when its
    after-window is still open. At the default 60 minutes after, an NFP print at
    12:30 is still inside its blackout at 13:00, and a caller that asked from
    13:00 gets no window for it at all.

    The fix belongs to the caller, which has to widen its request start by
    `DataConfig.calendar_blackout_after_min` before handing the result to
    `blackout_windows`. Both docstrings now say so. This test exists so that the
    behaviour cannot change quietly in either direction.
    """
    payload = [row("Non-Farm Employment Change", "USD", "2026-09-16T08:30:00-04:00")]
    printed_at = datetime(2026, 9, 16, 12, 30, tzinfo=UTC)
    asked_from = printed_at + timedelta(minutes=30)

    with serve(payload):
        narrow = source.events(("USD",), asked_from, asked_from + timedelta(hours=24))
        widened = source.events(
            ("USD",),
            asked_from - timedelta(minutes=60),
            asked_from + timedelta(hours=24),
        )

    assert narrow == ()
    assert source.blackout_windows(narrow) == {}

    assert [event.title for event in widened] == ["Non-Farm Employment Change"]
    opened, closed = source.blackout_windows(widened)["USD"][0]
    assert opened < asked_from < closed
