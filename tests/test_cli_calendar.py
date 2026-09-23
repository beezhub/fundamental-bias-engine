"""``fbe calendar``: the morning's releases, and the windows they close.

This is the page a person reads before deciding the morning is quiet, which is
what makes an empty one dangerous. `fbe.datasources.calendar` raises on every
failure rather than returning nothing, precisely so this command can tell a
quiet week from a fetch that came back as a rate-limit page, and the tests here
hold that apart: a failed fetch says so and exits, and never renders as a
calendar with no events in it.

The other half is division of labour. Which releases matter and where a window
starts and ends are `fbe.calendar_guard`'s decisions, not this renderer's. A
second copy of either in ``cli.py`` would drift from the guard the bias layer
uses, and the two would disagree on a Friday afternoon with nobody watching. So
the window tests cross-check against `fbe.calendar_guard.blackout_windows`
itself, and one of them replaces that function to prove the command calls it
rather than reproducing it.

No test reaches the network. The feed URL is served by `respx`, the same way
``tests/test_calendar_source.py`` does it, so the real source, the real parser
and the real filtering all run against a payload written here.

Times are asserted in a fixed zone. The local zone is set to
``Africa/Johannesburg`` for every test, so ``+02:00`` in an assertion is a fact
about the rendering rather than about the machine the suite runs on.
"""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fbe.calendar_guard import blackout_windows
from fbe.cli import app
from fbe.config import DataConfig
from fbe.datasources.calendar import FEED_URL
from fbe.types import CalendarEvent

runner = CliRunner()

EXIT_OK = 0
"""The command did what it was asked, per the table in `docs/interfaces.md`."""

EXIT_UNUSABLE = 1
"""It ran and the result should not be traded on. A failed calendar fetch."""

EXIT_USAGE = 2
"""Usage error from the parser. An unknown currency or a malformed pair."""

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
"""A Wednesday at 10:00 UTC, which is 12:00 in the zone these tests render in."""

DENIED_BODY = (
    "<html><head><title>Request Denied</title></head>"
    "<body><h1>Request Denied</h1><p>You have exceeded the download limit."
    "</p></body></html>"
)
"""What the publisher returns past its rate limit, per ``docs/data-sources.md``.

Served with a 200, which is the part that matters: the failure arrives as a page
rather than as a status code, so a source that only checked the status would
parse this as a week with no events in it.
"""


def row(title: str, country: str, when: str, impact: str = "High") -> dict[str, str]:
    """One feed row, in the feed's own shape and capitalisation."""
    return {
        "title": title,
        "country": country,
        "date": when,
        "impact": impact,
        "forecast": "",
        "previous": "",
    }


WEEK = [
    row("Bank Holiday", "JPY", "2026-09-16T11:00:00+00:00", impact="Holiday"),
    row("Retail Sales m/m", "EUR", "2026-09-16T11:00:00+00:00"),
    row("CPI y/y", "USD", "2026-09-16T13:30:00+00:00"),
    row("Fed Chair Powell Speaks", "USD", "2026-09-16T14:00:00+00:00"),
    row("Nationwide HPI m/m", "GBP", "2026-09-16T16:00:00+00:00", impact="Medium"),
    row("Employment Change", "AUD", "2026-09-17T01:30:00+00:00"),
    row("GDP q/q", "NZD", "2026-09-18T22:45:00+00:00"),
]
"""One week, chosen so that each criterion has a row that isolates it.

* The JPY holiday is ``Holiday``, which `fbe.datasources.calendar` gives no
  severity at all, so no impact floor admits it.
* USD CPI at 13:30 and the speaker at 14:00 are #198's merge case: 13:00-14:30
  and 13:30-15:00 become one window.
* The GBP row is ``Medium`` and its title matches no keyword in
  `fbe.calendar_guard.HIGH_IMPACT_KEYWORDS`, so it is the one row that a lower
  floor lists while the guard still gives it no window. That separates "what is
  shown" from "what closes the market", which are different questions answered
  by different modules.
* The EUR window is 10:30-12:00 and stops short of the USD pair's 13:00, so the
  merge assertion is about the two USD rows rather than about everything
  touching everything.
* The NZD row on Friday is outside every horizon tested here and exists to set
  the source's horizon, so a 24-hour run is fully covered and a 96-hour run is
  not.
"""

HIGH_IN_HORIZON = (
    "Retail Sales m/m",
    "CPI y/y",
    "Fed Chair Powell Speaks",
    "Employment Change",
)
"""The high-impact titles inside a 24-hour horizon from `NOW`, in time order."""


@pytest.fixture(autouse=True)
def johannesburg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the local zone, so a rendered offset is not the machine's opinion."""
    monkeypatch.setenv("TZ", "Africa/Johannesburg")
    time.tzset()
    yield
    time.tzset()


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the command's idea of now.

    The horizon is measured from it, so without this every assertion about
    which events are inside the window would depend on the day the suite ran.
    """
    monkeypatch.setattr("fbe.cli._now", lambda: NOW)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """A config pointing every directory at ``tmp_path``."""
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n"
    )
    return path


def serve(rows: list[dict[str, str]]) -> respx.Router:
    """Route the feed URL to a payload, with no network underneath."""
    router = respx.mock(assert_all_called=False)
    router.get(FEED_URL).mock(return_value=httpx.Response(200, text=json.dumps(rows)))
    return router


def deny() -> respx.Router:
    """Route the feed URL to the publisher's rate-limit page."""
    router = respx.mock(assert_all_called=False)
    router.get(FEED_URL).mock(return_value=httpx.Response(200, text=DENIED_BODY))
    return router


def run(config: Path, *args: str, rows: list[dict[str, str]] | None = None) -> str:
    """Invoke the command against a served week and return its output."""
    with serve(WEEK if rows is None else rows):
        result = runner.invoke(
            app, ["--config", str(config), "calendar", *args], catch_exceptions=False
        )
    assert result.exit_code == EXIT_OK, result.output
    return result.output


def data_lines(output: str) -> list[str]:
    """Output lines carrying a rendered time, which is where the offsets are."""
    return [line for line in output.splitlines() if ":" in line and "+" in line]


def expected_windows(titles: tuple[str, ...]) -> tuple[tuple[datetime, datetime], ...]:
    """The windows the guard derives for these titles, computed independently.

    Built from `fbe.calendar_guard.blackout_windows` rather than from the
    command's own output, so a test comparing the two is comparing the renderer
    against the module that owns the arithmetic.
    """
    events = tuple(
        CalendarEvent(
            currency=entry["country"],
            title=entry["title"],
            impact=entry["impact"],
            scheduled_for=datetime.fromisoformat(entry["date"]).astimezone(UTC),
        )
        for entry in WEEK
        if entry["title"] in titles
    )
    return tuple(blackout_windows(events, DataConfig()))


# --- the event list ----------------------------------------------------------


def test_the_events_inside_the_horizon_are_listed_in_time_order(
    config_file: Path,
) -> None:
    """Criterion 1. Every high-impact release in the window, earliest first."""
    output = run(config_file, "--hours", "24")

    positions = [output.index(title) for title in HIGH_IN_HORIZON]
    assert positions == sorted(positions), output
    assert "GDP q/q" not in output


def test_each_row_carries_the_currency_the_impact_and_the_time(
    config_file: Path,
) -> None:
    """Criterion 1, the rest of it. A title alone is not actionable."""
    output = run(config_file, "--hours", "24")

    line = next(line for line in output.splitlines() if "CPI y/y" in line)
    assert "USD" in line
    assert "High" in line
    assert "15:30" in line


def test_every_printed_time_carries_its_utc_offset(config_file: Path) -> None:
    """Criterion 2. A calendar ambiguous about the hour is worse than none.

    Asserted on the rendered lines rather than on the underlying values,
    because the value being aware is not what a reader at 06:00 acts on.
    """
    lines = data_lines(run(config_file, "--hours", "24"))

    assert lines
    for line in lines:
        assert "+02:00" in line, line


def test_the_local_zone_is_the_one_the_operator_is_in(config_file: Path) -> None:
    """The offset is read from the environment, not written into the code."""
    output = run(config_file, "--hours", "24")

    assert "15:30" in output, output
    assert "13:30" not in output.split("Horizon")[0], output


# --- the windows, which the guard owns ---------------------------------------


def test_the_release_and_the_speaker_render_as_one_window(config_file: Path) -> None:
    """Criterion 3, against #198's own case.

    CPI at 13:30 and a speaker at 14:00 give 13:00-14:30 and 13:30-15:00, which
    merge. Two rows here would invite a reader to trade the half hour between
    them, which is the gap the merge exists to close.
    """
    output = run(config_file, "--hours", "24", "--blackouts")

    assert "15:00" in output and "17:00" in output, output
    assert "16:30" not in output, output
    assert len(expected_windows(HIGH_IN_HORIZON)) == 3


def test_the_windows_are_the_guard_s_windows(config_file: Path) -> None:
    """Criterion 9. Every window on the page came from `blackout_windows`.

    Cross-checked rather than recomputed: the expectation is built by calling
    the guard in the test, so a renderer that derived its own intervals from
    the same events would have to reproduce the merge, the asymmetric minutes
    and the high-impact filter exactly to pass.
    """
    output = run(config_file, "--hours", "24", "--blackouts")

    for opens, closes in expected_windows(HIGH_IN_HORIZON):
        assert opens.astimezone().strftime("%H:%M") in output
        assert closes.astimezone().strftime("%H:%M") in output


def test_the_command_calls_the_guard_rather_than_merging_its_own(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 9, the other direction. Replace the guard, move the page.

    A command holding its own interval arithmetic would ignore this and print
    the real windows, which is the drift this asserts against.
    """
    sentinel = (
        (
            datetime(2026, 9, 16, 20, 0, tzinfo=UTC),
            datetime(2026, 9, 16, 21, 0, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr("fbe.cli.blackout_windows", lambda events, config: sentinel)

    output = run(config_file, "--hours", "24", "--blackouts")

    assert "22:00" in output and "23:00" in output, output
    assert "15:00 - 17:00" not in output, output


def test_a_listed_event_the_guard_does_not_window_says_so(config_file: Path) -> None:
    """The Medium row is shown and closes nothing, and the page says which.

    Two different questions, answered by two different modules: the impact floor
    decides what is worth reading, and `fbe.calendar_guard.is_high_impact`
    decides what stops a trade. A page that implied the second from the first
    would black out a housing-price index.
    """
    output = run(config_file, "--hours", "24", "--impact", "medium")

    line = next(line for line in output.splitlines() if "Nationwide HPI" in line)
    assert "18:00" in line
    assert "-" in line.split("18:00")[1]


# --- the filters -------------------------------------------------------------


def test_a_currency_filter_narrows_the_list(config_file: Path) -> None:
    """Criterion 4, first half."""
    output = run(config_file, "--hours", "24", "--currency", "USD")

    assert "CPI y/y" in output
    assert "Retail Sales m/m" not in output
    assert "Employment Change" not in output


def test_a_pair_filter_matches_the_base_leg(config_file: Path) -> None:
    """Criterion 4, and the failure it names.

    A filter that only matched the quote currency would hide the EUR release on
    EURUSD, which is half of what the pair does.
    """
    output = run(config_file, "--hours", "24", "--pair", "EURUSD")

    assert "Retail Sales m/m" in output
    assert "CPI y/y" in output
    assert "Employment Change" not in output


def test_a_pair_filter_matches_the_quote_leg(config_file: Path) -> None:
    """The other leg of the same criterion, stated separately."""
    output = run(config_file, "--hours", "24", "--pair", "AUDUSD")

    assert "Employment Change" in output
    assert "CPI y/y" in output
    assert "Retail Sales m/m" not in output


def test_the_impact_floor_defaults_to_high(config_file: Path) -> None:
    """Criterion 5. The default is the plan's own threshold."""
    default = run(config_file, "--hours", "24")
    lowered = run(config_file, "--hours", "24", "--impact", "medium")

    assert "Nationwide HPI" not in default
    assert "Nationwide HPI" in lowered


def test_a_holiday_row_is_not_a_low_impact_event(config_file: Path) -> None:
    """Criterion 5's second half. A market closure is not a release.

    `fbe.datasources.calendar.IMPACT_SEVERITY` leaves ``Holiday`` out on
    purpose, so no floor admits it. Asserted at the lowest floor, where a
    severity of nothing would otherwise be easiest to mistake for low.
    """
    output = run(config_file, "--hours", "24", "--impact", "low")

    assert "Bank Holiday" not in output


def test_an_unknown_currency_is_refused(config_file: Path) -> None:
    """A typo that silently matched nothing would read as a quiet morning."""
    with serve(WEEK):
        result = runner.invoke(
            app, ["--config", str(config_file), "calendar", "--currency", "XAU"]
        )

    assert result.exit_code == EXIT_USAGE
    assert "XAU" in result.output


@pytest.mark.parametrize("pair", ["EURUS", "EURXAU", "eurusdd"])
def test_a_pair_that_is_not_a_g10_pair_is_refused(config_file: Path, pair: str) -> None:
    """Length alone is not a check: ``EURXAU`` splits as cleanly as ``EURUSD``."""
    with serve(WEEK):
        result = runner.invoke(
            app, ["--config", str(config_file), "calendar", "--pair", pair]
        )

    assert result.exit_code == EXIT_USAGE


def test_a_lowercase_pair_is_answered_rather_than_refused(config_file: Path) -> None:
    """The one input shape to be forgiving about, since nothing is ambiguous."""
    output = run(config_file, "--hours", "24", "--pair", "eurusd")

    assert "Retail Sales m/m" in output


# --- the machine formats -----------------------------------------------------


def test_json_carries_the_same_events_and_windows_as_the_table(
    config_file: Path,
) -> None:
    """Criterion 6. Both views of one run, or the two will drift."""
    payload = json.loads(run(config_file, "--hours", "24", "--format", "json"))

    assert [event["title"] for event in payload["events"]] == list(HIGH_IN_HORIZON)
    windows = [
        (
            datetime.fromisoformat(window["opens"]),
            datetime.fromisoformat(window["closes"]),
        )
        for window in payload["windows"]
    ]
    assert tuple(windows) == expected_windows(HIGH_IN_HORIZON)


def test_json_carries_the_windows_even_in_the_event_view(config_file: Path) -> None:
    """``--blackouts`` is a table choice, not a filter on what the run knows."""
    events_view = json.loads(run(config_file, "--hours", "24", "--format", "json"))
    windows_view = json.loads(
        run(config_file, "--hours", "24", "--blackouts", "--format", "json")
    )

    assert events_view["windows"] == windows_view["windows"]
    assert events_view["events"] == windows_view["events"]


def test_csv_carries_the_same_events_and_windows_as_the_table(
    config_file: Path,
) -> None:
    """Criterion 6 for the flat format, parsed rather than pattern matched."""
    rows = list(
        csv.DictReader(
            io.StringIO(run(config_file, "--hours", "24", "--format", "csv"))
        )
    )

    titles = [row["title"] for row in rows if row["kind"] == "event"]
    windows = [row for row in rows if row["kind"] == "window"]
    assert titles == list(HIGH_IN_HORIZON)
    assert len(windows) == len(expected_windows(HIGH_IN_HORIZON))
    assert windows[0]["starts"] == expected_windows(HIGH_IN_HORIZON)[0][0].isoformat()


# --- what the page says when it cannot say anything --------------------------


def test_a_failed_fetch_says_so_and_exits_unusable(config_file: Path) -> None:
    """Criterion 7. The one failure that must never render as a quiet week.

    An empty calendar clears every blackout at once, and this is the command a
    person reads before deciding the morning is quiet.

    The category and the publisher's own reason are both asserted, not just
    the word "fetch". The second line of the report carries that word too,
    so a laxer assertion passes on a page that has stopped saying what went
    wrong.
    """
    with deny():
        result = runner.invoke(
            app, ["--config", str(config_file), "calendar", "--hours", "24"]
        )

    assert result.exit_code == EXIT_UNUSABLE
    assert "fetch_failed" in result.output
    assert "rate limit page" in result.output


def test_a_failed_fetch_is_not_rendered_as_an_empty_calendar(
    config_file: Path,
) -> None:
    """The same run, asserted from the other side: no clear verdict anywhere.

    The positive assertions are load-bearing. Against the scaffolded command
    this test passed on its negatives alone, because output nothing wrote
    contains neither phrase, and a test that passes on an empty page is exactly
    the trap this page is about.
    """
    with deny():
        result = runner.invoke(
            app, ["--config", str(config_file), "calendar", "--hours", "24"]
        )

    assert result.exit_code == EXIT_UNUSABLE
    assert "fetch_failed" in result.output
    assert "no events" not in result.output.lower()
    assert "clear" not in result.output.lower()


def test_a_quiet_week_is_not_rendered_as_a_failure(config_file: Path) -> None:
    """The opposite fact, which has to render differently.

    A week the publisher really has nothing in is data. Without this test, a
    command could pass the one above by calling every empty answer a failure.
    """
    with serve([row("GDP q/q", "NZD", "2026-09-18T22:45:00+00:00")]):
        result = runner.invoke(
            app, ["--config", str(config_file), "calendar", "--hours", "24"]
        )

    assert result.exit_code == EXIT_OK
    assert "fetch failed" not in result.output.lower()


def test_coverage_short_of_the_horizon_says_how_far_it_reaches(
    config_file: Path,
) -> None:
    """Criterion 8. The Friday-run-asked-about-Monday case, named on the page.

    The feed publishes one week at a time, so a horizon past its last event is
    not a fault and not clear either. The page has to name the moment the
    coverage ends, or the reader cannot tell which part of the answer to trust.
    """
    output = run(config_file, "--hours", "96")

    assert "2026-09-18" in output, output
    assert "coverage" in output.lower(), output


def test_a_fully_covered_horizon_says_nothing_about_coverage(
    config_file: Path,
) -> None:
    """The other half. A warning printed every run is a warning nobody reads."""
    output = run(config_file, "--hours", "24")

    assert "coverage" not in output.lower(), output


def test_the_impact_floor_is_handed_to_the_source(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 9 for the impact rule: the command does not apply it itself.

    Recorded at the boundary rather than inferred from the output, because a
    command that filtered by impact after the fact would print the same page
    and diverge the moment `fbe.datasources.calendar.IMPACT_SEVERITY` changes.

    The two calls are the design and not an accident, so both are asserted. The
    first is the list a person reads, at the floor they asked for. The second is
    what the windows are derived from, at the lowest floor and over a wider
    span, because the floor governs what is worth reading and
    `fbe.calendar_guard.is_high_impact` governs what closes the market. A
    Medium-rated CPI print is the case that separates them: the listing at
    ``--impact high`` leaves it out and the guard would still black it out, so
    deriving windows from the listed slice would lose the window.
    """
    calls: list[dict[str, object]] = []

    class Recorder:
        def __init__(self, config: object) -> None:
            self.config = config

        def events(
            self,
            currencies: object,
            start: datetime,
            end: datetime,
            min_impact: str = "High",
        ) -> tuple[CalendarEvent, ...]:
            calls.append(
                {
                    "currencies": sorted(currencies),  # type: ignore[arg-type]
                    "min_impact": min_impact,
                    "start": start,
                    "end": end,
                }
            )
            return ()

        def horizon(self) -> datetime:
            return NOW + timedelta(days=7)

    monkeypatch.setattr("fbe.cli.CalendarSource", Recorder)
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_file),
            "calendar",
            "--hours",
            "6",
            "--impact",
            "medium",
            "--currency",
            "USD",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_OK
    listing, for_windows = calls
    assert listing == {
        "currencies": ["USD"],
        "min_impact": "medium",
        "start": NOW,
        "end": NOW + timedelta(hours=6),
    }
    assert for_windows["min_impact"] == "low"
    assert for_windows["start"] < NOW
    assert for_windows["end"] > NOW + timedelta(hours=6)


def test_a_window_held_open_past_the_horizon_is_not_cut_short(
    config_file: Path,
) -> None:
    """The defect the second fetch exists to prevent, as a test.

    A four-hour horizon from 12:00 local reaches 16:00 and holds the 15:30 CPI
    print but not the 16:00 speaker. Their windows merge, so the real blackout
    runs to 17:00. Deriving windows from the listed slice alone printed
    ``15:00 - 16:30``, which tells a reader the market reopens half an hour
    before it does: a plausible number, wrong in the dangerous direction, and
    invisible without this case.
    """
    output = run(config_file, "--hours", "4")

    line = next(line for line in output.splitlines() if "CPI y/y" in line)
    assert line.rstrip().endswith("15:00 - 17:00"), line


def test_a_window_named_by_a_release_outside_the_horizon_says_which(
    config_file: Path,
) -> None:
    """A window with no visible cause reads as a rendering fault.

    The speaker that holds the 15:00-17:00 window open is outside a four-hour
    horizon, so the event table does not list it. The window view still names
    it, because "why is this closed" is the question that row answers.
    """
    output = run(config_file, "--hours", "4", "--blackouts")

    line = next(line for line in output.splitlines() if "15:00" in line)
    assert "Fed Chair Powell Speaks" in line, line


def test_a_window_opening_exactly_at_the_horizon_end_is_reported(
    config_file: Path,
) -> None:
    """The boundary of the selection, which a strict comparison would drop.

    Three hours from 12:00 local reaches 15:00, and the CPI window opens at
    exactly 15:00. Dropping it would tell a reader the next three hours are
    clear right up to the instant the market shuts, which is true and useless:
    what they are deciding is whether to open a trade now that runs into it.
    """
    output = run(
        config_file,
        "--hours",
        "3",
        "--blackouts",
        rows=[row("CPI y/y", "USD", "2026-09-16T13:30:00+00:00")],
    )

    assert "15:00" in output, output
    assert "16:30" in output, output


def test_a_release_on_a_window_s_closing_instant_is_inside_it(
    config_file: Path,
) -> None:
    """Containment is inclusive at both ends, as `is_blacked_out` has it.

    The EUR window closes at 14:00 local and the housing print is at exactly
    14:00. Reporting it as outside would put a release on the page with no
    blackout beside it at the one moment the two answers differ, and the guard
    the bias layer uses would disagree with the page the trader read.
    """
    output = run(
        config_file,
        "--hours",
        "6",
        "--impact",
        "medium",
        rows=[
            row("Retail Sales m/m", "EUR", "2026-09-16T11:00:00+00:00"),
            row(
                "Nationwide HPI m/m",
                "GBP",
                "2026-09-16T12:00:00+00:00",
                impact="Medium",
            ),
        ],
    )

    line = next(line for line in output.splitlines() if "Nationwide HPI" in line)
    assert line.rstrip().endswith("12:30 - 14:00"), line


def test_csv_carries_the_coverage_caveat_when_there_is_one(
    config_file: Path,
) -> None:
    """The caveat reaches the machine format too, or it does not exist there.

    The table prints it and the JSON carries it. A consumer reading three
    window rows out of CSV and nothing else would conclude the horizon was
    covered, which is ADR 0002 rule 3: a marker that is not rendered does not
    exist.
    """
    short = list(
        csv.DictReader(
            io.StringIO(run(config_file, "--hours", "96", "--format", "csv"))
        )
    )
    covered = list(
        csv.DictReader(
            io.StringIO(run(config_file, "--hours", "24", "--format", "csv"))
        )
    )

    caveat = [row for row in short if row["kind"] == "coverage"]
    assert [row["impact"] for row in caveat] == ["beyond_horizon"]
    assert "2026-09-18" in caveat[0]["title"]
    assert not [row for row in covered if row["kind"] == "coverage"]
