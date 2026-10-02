"""An empty event list on a run that read no calendar is not a clear calendar: #320.

Section 4 of the Markdown report and the dashboard's event table both printed
"No qualifying events in the horizon." whenever `BiasReport.events` was empty.
While `fbe.calendar_guard` is scaffolded, every run reads no calendar, so every
report said the calendar was clear on a day nobody looked at it. `fbe bias`
said the opposite on the same run. The trading plan's rule against trading into
high-impact news is the one the engine claims to enforce, so an unread calendar
shown as a clear one is the wrong answer that looks right.

The information was already on every pair as an ``event:unchecked`` or
``event:unknown`` marker. These tests pin that both templates read it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from fbe.bias import UNCHECKED_SUFFIX, UNKNOWN_SUFFIX
from fbe.dashboard.build import render_dashboard
from fbe.report import build_context, load_report, render_report
from fbe.types import BiasReport

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_report.json"

CLEAR = "No qualifying events in the horizon."

UNCHECKED = "event" + UNCHECKED_SUFFIX
UNKNOWN = "event" + UNKNOWN_SUFFIX + ": the feed returned 503"


def run_with(*blockers_per_pair: tuple[str, ...]) -> BiasReport:
    """The committed run, with no events and each pair's markers replaced.

    One tuple of markers per pair, cycled across the run's pairs, so a test can
    say "every pair unread" or "one pair unread" without restating the run.
    """
    base = load_report(FIXTURE)
    pairs = tuple(
        replace(row, blockers=blockers_per_pair[index % len(blockers_per_pair)])
        for index, row in enumerate(base.pairs)
    )
    return replace(base, pairs=pairs, events=())


def calendar_section(markdown: str) -> str:
    """Section 4 of the Markdown report, up to the next section heading."""
    start = markdown.index("## 4. Calendar and blackouts")
    end = markdown.index("\n## ", start + 1)
    return markdown[start:end]


@pytest.mark.parametrize("marker", [UNCHECKED, UNKNOWN])
def test_the_report_does_not_call_an_unread_calendar_clear(marker: str) -> None:
    section = calendar_section(render_report(run_with(("cost:unchecked", marker))))

    assert CLEAR not in section
    assert "not read" in section
    assert "by hand" in section


def test_the_report_still_says_clear_when_the_calendar_was_read() -> None:
    """The two states must read differently, so the clear one must survive."""
    section = calendar_section(render_report(run_with(("cost:unchecked",))))

    assert CLEAR in section
    assert "not read" not in section


def test_the_report_counts_the_pairs_whose_calendar_was_not_read() -> None:
    """One unread pair in a run is a partial answer, and the count says so."""
    report = run_with(("cost:unchecked", UNCHECKED), ("cost:unchecked",))
    unread = sum(UNCHECKED in row.blockers for row in report.pairs)
    section = calendar_section(render_report(report))

    assert f"{unread} of {len(report.pairs)} pairs" in section


def test_the_count_comes_from_the_context_not_the_template() -> None:
    """Renderers compute nothing: the number is supplied, and both pages read it."""
    report = run_with(("cost:unchecked", UNCHECKED), ("cost:unchecked", UNKNOWN))

    assert build_context(report)["calendar_unread"] == len(report.pairs)


@pytest.mark.parametrize("marker", [UNCHECKED, UNKNOWN])
def test_the_dashboard_does_not_call_an_unread_calendar_clear(marker: str) -> None:
    page = render_dashboard(run_with(("cost:unchecked", marker)))

    assert CLEAR not in page


def test_the_dashboard_still_says_clear_when_the_calendar_was_read() -> None:
    page = render_dashboard(run_with(("cost:unchecked",)))

    assert CLEAR in page
