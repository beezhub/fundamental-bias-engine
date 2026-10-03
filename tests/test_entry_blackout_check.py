"""`fbe journal add` checks the entry time against the news windows.

Issue #336, from proposal #334. The owner's rule is to avoid placing a trade
inside a high-impact release window. The morning report lists the windows and
blocks nothing (#335); this is the check at the moment the trade was placed,
recorded on the trade as `TradeRecord.blackout_check`. The journal records what
happened, so an entry inside a window is recorded and named, not refused.

No network: the calendar source is replaced, and `journal add` reads only the
cache in any case.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fbe.config import Config, DataConfig
from fbe.datasources.base import SourceError
from fbe.journal import BlackoutCheck, append, load
from fbe.types import CalendarEvent

MONDAY = datetime(2026, 10, 5, tzinfo=UTC)


def at(hour: int, minute: int = 0, days: int = 0) -> datetime:
    return MONDAY + timedelta(days=days, hours=hour, minutes=minute)


def release(
    when: datetime, currency: str = "USD", title: str = "CPI y/y"
) -> CalendarEvent:
    return CalendarEvent(
        title=title, currency=currency, scheduled_for=when, impact="High"
    )


WEEK = (
    release(at(1, 30), "AUD", "Retail Sales m/m"),
    release(at(12, 30)),
    release(at(14, 0, days=4), "USD", "Non-Farm Employment Change"),
)


@pytest.fixture
def entry_check(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[DataConfig]]:
    """The real `_entry_blackout`, over `WEEK` instead of the feed. Yields the
    data configs the source was built with, so a test can assert it read the
    cache only."""
    from tests.conftest import REAL_ENTRY_BLACKOUT

    monkeypatch.setattr("fbe.cli._entry_blackout", REAL_ENTRY_BLACKOUT)
    built: list[DataConfig] = []
    original_init = __import__(
        "fbe.cli", fromlist=["CalendarSource"]
    ).CalendarSource.__init__

    def init(self: object, config: DataConfig) -> None:
        built.append(config)
        original_init(self, config)

    def events(
        self: object,
        currencies: Sequence[str],
        start: datetime,
        end: datetime,
        min_impact: str = "high",
    ) -> Sequence[CalendarEvent]:
        return tuple(e for e in WEEK if start <= e.scheduled_for < end)

    monkeypatch.setattr("fbe.cli.CalendarSource.__init__", init)
    monkeypatch.setattr("fbe.cli.CalendarSource.events", events)
    monkeypatch.setattr(
        "fbe.cli.CalendarSource.horizon", lambda self: at(14, 0, days=4)
    )
    yield built


def check(pair: str, when: datetime) -> tuple[BlackoutCheck, str | None]:
    from fbe import cli

    return cli._entry_blackout(Config(), pair, when)


# --- the check ---------------------------------------------------------------------


def test_an_entry_inside_a_window_is_recorded_as_inside_it(
    entry_check: list[DataConfig],
) -> None:
    """USD CPI at 12:30 shuts EURUSD 12:00 to 13:30. An entry at 12:40 is in it."""
    state, notice = check("EURUSD", at(12, 40))

    assert state is BlackoutCheck.INSIDE_WINDOW
    assert notice is not None
    assert "USD CPI y/y" in notice
    assert "13:30 UTC" in notice


def test_the_same_pair_earlier_that_day_is_clear(entry_check: list[DataConfig]) -> None:
    """The owner's point on #334: news that day does not make the day untradeable."""
    assert check("EURUSD", at(10)) == (BlackoutCheck.CLEAR, None)


def test_a_release_on_neither_leg_does_not_touch_the_entry(
    entry_check: list[DataConfig],
) -> None:
    assert check("EURJPY", at(12, 40)) == (BlackoutCheck.CLEAR, None)


def test_the_check_reads_the_cache_and_never_the_network(
    entry_check: list[DataConfig],
) -> None:
    """`journal add` already scores from the cache only. The calendar too."""
    check("EURUSD", at(10))

    assert entry_check and all(config.offline for config in entry_check)


def test_an_unreadable_calendar_is_unknown(
    entry_check: list[DataConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(self: object, *args: object, **kwargs: object) -> None:
        raise SourceError("no cached week")

    monkeypatch.setattr("fbe.cli.CalendarSource.events", refused)

    state, notice = check("EURUSD", at(10))

    assert state is BlackoutCheck.UNKNOWN
    assert notice is not None and "no cached week" in notice


def test_an_entry_from_before_the_cached_week_is_unknown_not_clear(
    entry_check: list[DataConfig],
) -> None:
    """The feed holds the current week only. A trade from last week finds no
    release in this week's payload, which must not read as a quiet entry."""
    state, notice = check("EURUSD", at(12, 40, days=-7))

    assert state is BlackoutCheck.UNKNOWN
    assert notice is not None


def test_an_entry_past_the_cached_week_is_unknown(
    entry_check: list[DataConfig],
) -> None:
    state, _ = check("EURUSD", at(9, days=6))

    assert state is BlackoutCheck.UNKNOWN


# --- the vocabulary ------------------------------------------------------------------


def test_the_new_state_round_trips_and_old_lines_still_load(tmp_path: Path) -> None:
    from tests.test_journal import record

    path = tmp_path / "trades.jsonl"
    append(record("NEW", blackout_check=BlackoutCheck.INSIDE_WINDOW), path)
    append(record("OLD"), path)
    lines = path.read_text(encoding="utf-8").splitlines()
    old = json.loads(lines[1])
    del old["blackout_check"]
    path.write_text(lines[0] + "\n" + json.dumps(old) + "\n", encoding="utf-8")

    states = {r.trade_id: r.blackout_check for r in load(path=path)}

    assert states == {
        "NEW": BlackoutCheck.INSIDE_WINDOW,
        "OLD": BlackoutCheck.NOT_RUN,
    }


# --- through the command -----------------------------------------------------------


def test_journal_add_records_the_check_and_does_not_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_cli_journal_add import add, only_record, taken

    monkeypatch.setattr(
        "fbe.cli._entry_blackout",
        lambda config, pair, when: (
            BlackoutCheck.INSIDE_WINDOW,
            "Entered inside a news window: USD CPI y/y at 2026-10-05 12:30 UTC.",
        ),
    )

    code, output, journal = add(monkeypatch, tmp_path, *taken())

    assert code == 0, output
    assert only_record(journal).blackout_check is BlackoutCheck.INSIDE_WINDOW
    assert "Entered inside a news window" in output


def test_a_correction_keeps_the_check_recorded_at_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A close written days later must not recheck against a different week."""
    from tests.test_cli_journal_add import add, only_record, taken

    monkeypatch.setattr(
        "fbe.cli._entry_blackout",
        lambda config, pair, when: (BlackoutCheck.INSIDE_WINDOW, None),
    )
    add(monkeypatch, tmp_path, *taken())
    monkeypatch.setattr(
        "fbe.cli._entry_blackout",
        lambda config, pair, when: pytest.fail("a correction rechecked the entry"),
    )

    code, output, journal = add(monkeypatch, tmp_path, *taken(**{"--exit": "1.0800"}))

    assert code == 0, output
    assert only_record(journal).blackout_check is BlackoutCheck.INSIDE_WINDOW
