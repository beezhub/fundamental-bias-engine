"""The morning commands read the calendar and list windows without blocking.

Issue #335, from proposal #334. The owner's rule is to avoid placing a trade
inside a short window around a high-impact release, and there is news on most
days, so a morning report that blocked a pair for a release due later that day
would leave almost nothing to trade. The report lists each pair's windows as a
non-blocking ``event:window`` marker; blocking belongs to the entry moment
(#336). No network: every event is built here and the source is replaced.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta

import pytest

from fbe.bias import BLOCKERS, apply_filters, blocking, kind_of, shortlist
from fbe.calendar_guard import CalendarCoverage, DailyCalendar
from fbe.config import Config, DataConfig
from fbe.types import (
    CalendarEvent,
    Conviction,
    CurrencyScore,
    Direction,
    PairBias,
)
from fbe.universe import G10, GLOBAL

CONFIG = DataConfig()
DAY = date(2026, 10, 5)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def release(
    currency: str = "USD",
    when: datetime | None = None,
    title: str = "CPI y/y",
    impact: str = "High",
) -> CalendarEvent:
    return CalendarEvent(
        title=title,
        currency=currency,
        scheduled_for=at(12, 30) if when is None else when,
        impact=impact,
    )


def week(*events: CalendarEvent, through: datetime | None = None) -> DailyCalendar:
    return DailyCalendar(
        CalendarCoverage(
            events=events,
            covers_through=at(23, 0, DAY + timedelta(days=4))
            if through is None
            else through,
        ),
        CONFIG,
        at(6),
    )


# --- the adapter ---------------------------------------------------------------


def test_a_leg_release_never_blocks_in_the_morning() -> None:
    daily = week(release())

    assert daily.guard("USD", DAY) == ((), None)


def test_a_leg_release_is_listed_as_its_window() -> None:
    """USD CPI at 12:30 shuts USD pairs from 12:00 to 13:30."""
    (note,) = week(release()).windows("USD", DAY)

    assert "USD CPI y/y at 2026-10-05 12:30 UTC" in note
    assert "no entries 12:00 to 13:30 UTC" in note


def test_only_that_currency_and_that_day_are_listed() -> None:
    daily = week(
        release("USD"),
        release("JPY", title="BoJ Policy Rate"),
        release("USD", at(12, 30, DAY + timedelta(days=1)), "Retail Sales m/m"),
    )

    notes = daily.windows("USD", DAY)

    assert len(notes) == 1
    assert "CPI y/y" in notes[0]


def test_a_window_reaching_into_the_day_from_the_night_before_is_listed() -> None:
    """A release at 23:45 the day before shuts until 00:45."""
    late = release("USD", at(23, 45, DAY - timedelta(days=1)), "Fed Chair Speaks")

    assert week(late).windows("USD", DAY)


def test_a_release_the_feed_rates_low_but_the_keywords_qualify_is_listed() -> None:
    """The daily commands fetch every level and let `is_high_impact` decide."""
    rated_low = release("USD", title="Non-Farm Employment Change", impact="Low")

    assert week(rated_low).windows("USD", DAY)


def test_a_quiet_release_is_not_listed() -> None:
    quiet = release("USD", title="Widget Sentiment Index", impact="Low")

    assert week(quiet).windows("USD", DAY) == ()


def test_a_global_event_that_day_is_named_for_global() -> None:
    summit = release(GLOBAL, at(9), "G20 Meetings", "Low")

    found, unknown = week(summit).guard(GLOBAL, DAY)

    assert unknown is None
    assert len(found) == 1 and "G20 Meetings" in found[0]


def test_a_failed_fetch_is_unknown_for_every_leg_and_lists_nothing() -> None:
    failed = DailyCalendar(
        CalendarCoverage(
            events=(), covers_through=None, fetch_ok=False, fetch_error="Request Denied"
        ),
        CONFIG,
        at(6),
    )

    found, unknown = failed.guard("USD", DAY)

    assert found == ()
    assert unknown is not None and "Request Denied" in unknown
    assert failed.windows("USD", DAY) == ()


def test_a_cache_that_does_not_reach_the_run_is_unknown() -> None:
    """A cache from an earlier week ends before the run asks."""
    _, unknown = week(release(), through=at(5)).guard("EUR", DAY)

    assert unknown is not None


def test_coverage_ending_later_that_day_still_answers() -> None:
    """The feed's horizon is the week's last release, so on the week's last
    day of releases it ends partway through the day, every week. Asked at 06:00
    with coverage to 15:00, the day is answered and its windows are listed."""
    daily = week(release(), through=at(15))

    assert daily.guard("EUR", DAY) == ((), None)
    assert daily.windows("USD", DAY)


def test_a_stale_cache_lists_nothing_even_when_it_holds_a_release() -> None:
    """Events held, but coverage ends before the run asks. Listing them would
    read as today's schedule from data that cannot vouch for today."""
    daily = week(release(), through=at(5))

    assert daily.windows("USD", DAY) == ()
    assert daily.events_on(DAY) == ()


def test_the_day_s_events_are_what_the_report_lists() -> None:
    daily = week(
        release("USD"),
        release(GLOBAL, at(9), "G20 Meetings", "Low"),
        release("USD", title="Widget Sentiment Index", impact="Low"),
        release("USD", at(12, 30, DAY + timedelta(days=1)), "Retail Sales m/m"),
    )

    titles = {event.title for event in daily.events_on(DAY)}

    assert titles == {"CPI y/y", "G20 Meetings"}


# --- through the bias layer ------------------------------------------------------


def _bias(pair: str) -> PairBias:
    return PairBias(
        pair=pair,
        base=pair[:3],
        quote=pair[3:],
        spread=1.4,
        direction=Direction.LONG,
        conviction=Conviction.HIGH,
        asof=DAY,
        base_score=0.8,
        quote_score=-0.6,
        agreement=0.9,
    )


def _scores() -> dict[str, CurrencyScore]:
    return {
        code: CurrencyScore(
            currency=code,
            composite=0.0,
            pillars={},
            dispersion=0.2,
            coverage=1.0,
            asof=DAY,
        )
        for code in G10
    }


def _filtered(daily: DailyCalendar, pair: str = "EURUSD") -> PairBias:
    return apply_filters(
        _bias(pair),
        _scores(),
        Config(),
        DAY,
        calendar_guard=daily.guard,
        calendar_windows=daily.windows,
        cost_ratio=0.0,
    )


def test_the_owners_case_a_1230_release_leaves_the_pair_tradeable() -> None:
    """The constraint set on review of #334, pinned: news later in the day does
    not block the morning run or shrink the shortlist."""
    filtered = _filtered(week(release("USD")))

    assert filtered.tradeable
    assert not blocking(filtered.blockers)
    assert [m for m in filtered.blockers if kind_of(m) == "event:window"]
    assert "event:unchecked" not in filtered.blockers
    assert shortlist([filtered], limit=3) == (filtered,)


def test_a_pair_with_no_release_carries_no_window() -> None:
    filtered = _filtered(week(release("USD")), "EURJPY")

    assert filtered.tradeable
    assert not [m for m in filtered.blockers if kind_of(m) == "event:window"]
    assert "event:unchecked" not in filtered.blockers


def test_an_unread_calendar_marks_unknown_and_never_clear() -> None:
    failed = DailyCalendar(
        CalendarCoverage(
            events=(), covers_through=None, fetch_ok=False, fetch_error="Request Denied"
        ),
        CONFIG,
        at(6),
    )

    filtered = _filtered(failed)

    assert any(kind_of(m) == "event:unknown" for m in filtered.blockers)


def test_the_window_kind_is_non_blocking() -> None:
    assert BLOCKERS["event:window"] is False


# --- through the commands --------------------------------------------------------


@pytest.fixture
def real_daily_calendar(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Undo the suite-wide default that the commands read no calendar, and
    replace the source with built events instead of the network."""
    from tests.conftest import REAL_DAILY_CALENDAR

    monkeypatch.setattr("fbe.cli._daily_calendar", REAL_DAILY_CALENDAR)
    monkeypatch.setattr("fbe.cli._now", lambda: at(6))
    asked: list[object] = []

    def events(
        self: object,
        currencies: Sequence[str],
        start: datetime,
        end: datetime,
        min_impact: str = "High",
    ) -> Sequence[CalendarEvent]:
        asked.append(min_impact)
        return (release("USD"),)

    monkeypatch.setattr("fbe.cli.CalendarSource.events", events)
    monkeypatch.setattr(
        "fbe.cli.CalendarSource.horizon",
        lambda self: at(23, 0, DAY + timedelta(days=4)),
    )
    yield asked


def test_the_command_reads_today_s_calendar_at_every_impact_level(
    real_daily_calendar: list[object],
) -> None:
    from fbe import cli

    daily = cli._daily_calendar(Config(), DAY)

    assert daily is not None
    assert real_daily_calendar == ["low"]
    assert daily.windows("USD", DAY)


def test_a_historical_run_does_not_read_this_week_s_calendar(
    real_daily_calendar: list[object],
) -> None:
    """The feed carries the current week only. Reading it for an old date would
    find no release and print the day as clear, so the run says unchecked."""
    from fbe import cli

    assert cli._daily_calendar(Config(), DAY - timedelta(days=30)) is None
    assert real_daily_calendar == []


def test_the_commands_pass_the_calendar_to_every_pair() -> None:
    """`_filtered` is what `fbe bias` and `fbe report` call."""
    from fbe import cli

    pairs = cli._filtered(
        [_bias("EURUSD"), _bias("EURJPY")], _scores(), Config(), DAY, week(release())
    )

    assert all("event:unchecked" not in row.blockers for row in pairs)
    assert any(kind_of(m) == "event:window" for m in pairs[0].blockers)
    assert all(row.tradeable for row in pairs)


def test_without_a_calendar_the_commands_say_unchecked() -> None:
    from fbe import cli

    (row,) = cli._filtered([_bias("EURUSD")], _scores(), Config(), DAY, None)

    assert "event:unchecked" in row.blockers


def test_the_run_note_says_whether_the_calendar_was_read() -> None:
    from fbe import cli

    (read,) = cli._bias_notes(consulted=True)
    (unread,) = cli._bias_notes(consulted=False)

    assert "event:window" in read and "Nothing here is blocked" in read
    assert "No calendar was consulted" in unread


def test_refresh_caches_the_week_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fbe import cli
    from tests.conftest import REAL_REFRESH_CALENDAR

    monkeypatch.setattr("fbe.cli._now", lambda: at(6))
    monkeypatch.setattr(
        "fbe.cli.CalendarSource.events", lambda self, *a, **k: (release(), release())
    )
    monkeypatch.setattr(
        "fbe.cli.CalendarSource.horizon",
        lambda self: at(23, 0, DAY + timedelta(days=4)),
    )

    REAL_REFRESH_CALENDAR(Config())  # type: ignore[operator]

    assert "Calendar: 2 events cached, through 2026-10-09 23:00 UTC." in (
        capsys.readouterr().out
    )
    assert cli._refresh_calendar is not REAL_REFRESH_CALENDAR


def test_refresh_reports_a_failed_calendar_without_failing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fbe.datasources.base import SourceError
    from tests.conftest import REAL_REFRESH_CALENDAR

    def refused(self: object, *args: object, **kwargs: object) -> None:
        raise SourceError("Request Denied")

    monkeypatch.setattr("fbe.cli._now", lambda: at(6))
    monkeypatch.setattr("fbe.cli.CalendarSource.events", refused)

    REAL_REFRESH_CALENDAR(Config())  # type: ignore[operator]

    out = capsys.readouterr().out
    assert "Calendar: not cached (Request Denied)" in out
    assert "event:unknown" in out


def test_refresh_fetches_nothing_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from tests.conftest import REAL_REFRESH_CALENDAR

    monkeypatch.setattr(
        "fbe.cli.CalendarSource.events",
        lambda self, *a, **k: pytest.fail("an offline refresh fetched the calendar"),
    )
    offline = replace(Config(), data=replace(Config().data, offline=True))

    REAL_REFRESH_CALENDAR(offline)  # type: ignore[operator]
