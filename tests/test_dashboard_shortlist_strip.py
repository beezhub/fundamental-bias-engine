"""The shortlist cards and the 24-hour strip: issue #261.

Both sections have rendered since #252 landed. What this file is about is the
two things they could not say and one they said ambiguously.

**An empty strip is not a clear strip.** The windows come from
`fbe.calendar_guard.blackout_windows` over ``report.events``, and a run whose
calendar fetch failed carries no events. Drawn as it stands, that page shows 24
hours with nothing shaded, which reads as a clear window in exactly the
situation where nobody knows. `docs/decisions/0002-representing-not-known.md`
rule 3 is the general form, and the guard's own three-valued answer exists for
this case. The report carries the run's answer in `fbe.types.PairBias.blockers`,
as ``event:unchecked`` for a run with no calendar and ``event:unknown: reason``
for one whose calendar could not answer, and those two are different sentences
on the page rather than one.

**A time without its zone is worse than no time.** Every instant on this page is
aware UTC, and the strip printed its hour labels and its event rows as bare
clock times. A reader on their phone in Johannesburg reading "13:00" against a
release at 15:00 SAST stands aside two hours late.

Everything renders from ``tests/fixtures/dashboard_report.json``, the run the
other dashboard tests use, whose two USD events are the overlapping case #198
tests: a release at 12:30 and a speaker at 13:00 merge into one window rather
than two with a gap between them. Nothing here reaches the network.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from fbe.bias import UNCHECKED_SUFFIX, UNKNOWN_SUFFIX
from fbe.calendar_guard import blackout_windows
from fbe.config import DataConfig, ScoringConfig
from fbe.dashboard.build import (
    STRIP_HOURS,
    _view,
    check_constraints,
    render_dashboard,
)
from fbe.report import load_report
from fbe.types import BiasReport

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_report.json"

FORBIDDEN = ("backtest", "backtested", "proven", "win rate", "hit rate", "edge")
"""Language `CLAUDE.md` forbids, checked again on the sections this issue owns."""

UNCHECKED = "event" + UNCHECKED_SUFFIX
UNKNOWN = "event" + UNKNOWN_SUFFIX


@pytest.fixture(scope="module")
def report() -> BiasReport:
    return load_report(FIXTURE)


@pytest.fixture(scope="module")
def page(report: BiasReport) -> str:
    return render_dashboard(report)


def section(page: str, heading: str) -> str:
    """The markup from one ``<h2>`` to the next, so a test reads one panel.

    Asserting a string is somewhere on the page cannot tell the shortlist from
    the footer, and both sections here print times and currencies.
    """
    match = re.search(
        rf"<h2>{heading}</h2>(.*?)(?=<h2>|</div>\s*<footer|\Z)", page, flags=re.S
    )
    assert match is not None, f"no section headed {heading!r}"
    return match.group(1)


def text(markup: str) -> str:
    """One line of text, with runs of whitespace collapsed.

    The template wraps its sentences to 88 columns, so a rendered sentence
    carries newlines and indentation in the middle of itself. The contract is
    the sentence a reader sees, not where the markup broke it, and asserting on
    the raw markup pins the line breaks instead.
    """
    return re.sub(r"\s+", " ", markup).strip()


def cards(page: str) -> list[str]:
    """Every shortlist card, in the order it renders."""
    return re.findall(r'<article class="card">(.*?)</article>', page, flags=re.S)


def strip(page: str) -> str:
    """The strip itself, without the event list under it."""
    match = re.search(r'<div class="strip">(.*?)</div>\s*<table', page, flags=re.S)
    assert match is not None, "no calendar strip rendered"
    return match.group(1)


def marked(report: BiasReport, markers: tuple[str, ...], count: int) -> BiasReport:
    """The same run with ``markers`` on the first ``count`` pairs."""
    pairs = tuple(
        replace(row, blockers=markers) if index < count else row
        for index, row in enumerate(report.pairs)
    )
    return replace(report, pairs=pairs)


# --- criteria 1 to 3: the cards ---------------------------------------------


def test_one_card_per_idea_naming_its_pair_direction_and_conviction(
    report: BiasReport, page: str
) -> None:
    """One card each, and each card naming its own idea.

    The fixture holds a sized idea and an unsized one on purpose, so a page
    that renders the first idea twice, or renders one card for the pair with a
    size, fails here rather than looking like a shortlist.
    """
    rendered = cards(page)

    assert len(rendered) == len(report.shortlist) == 2
    for card, idea in zip(rendered, report.shortlist, strict=True):
        assert idea.bias.pair in card
        assert idea.bias.direction.value in card
        assert idea.bias.conviction.value in card
        assert idea.rationale in card
        # The blackout, on the card that has one and not on the card that does
        # not: the tag is the whole reason a reader looks at the card before
        # the chart, so it cannot be on every card or on none.
        assert ("blackout" in card) is (idea.blackout_until is not None)


def test_a_card_with_no_size_renders_no_size_rather_than_a_zero(
    report: BiasReport, page: str
) -> None:
    """Criterion 2. An unsized idea is a bias, not a ticket for nothing.

    ``0.00 lots`` on a card is the plausible wrong answer: it reads as a ticket
    the sizing refused for being too small, when the truth is that no stop was
    supplied and no ticket was ever computed.
    """
    unsized = [idea for idea in report.shortlist if idea.size is None]
    assert unsized, "the fixture no longer holds an unsized idea"

    card = next(card for card in cards(page) if unsized[0].bias.pair in card)

    assert "lots" not in card
    assert "of balance" not in card
    assert "0.00" not in card


def test_every_money_figure_on_a_card_names_its_currency(
    report: BiasReport, page: str
) -> None:
    """Criterion 3. A money figure without its currency is the notional defect.

    Both money figures on a card are in ``PositionSize.account_currency``, and
    the card prints that code against each rather than once in a heading, so a
    reader cannot carry the wrong unit from one figure to the next.
    """
    sized = [idea for idea in report.shortlist if idea.size is not None]
    assert sized, "the fixture no longer holds a sized idea"

    size = sized[0].size
    assert size is not None
    card = next(card for card in cards(page) if sized[0].bias.pair in card)
    figures = (
        f"{size.realised_risk_amount:.2f}",
        f"{size.risk_amount:.2f}",
    )

    for figure in figures:
        assert re.search(rf"{size.account_currency}\s+{re.escape(figure)}", card), (
            figure
        )


def test_the_card_leads_with_the_risk_the_account_is_carrying(
    report: BiasReport, page: str
) -> None:
    """Criterion 3, second half: realised before intended.

    The rounded lot size exposes a different figure from the one the ladder
    asked for, and the realised one is what the journal and the checklist read.
    A card leading with the intended figure states a risk the account is not
    carrying.
    """
    sized = next(idea for idea in report.shortlist if idea.size is not None)
    size = sized.size
    assert size is not None
    assert size.realised_risk_amount != size.risk_amount, (
        "the fixture's ticket no longer carries the lot-step gap"
    )

    card = next(card for card in cards(page) if sized.bias.pair in card)

    assert card.index(f"{size.realised_risk_amount:.2f}") < card.index(
        f"{size.risk_amount:.2f}"
    )


# --- criterion 4: the strip is a day, measured from the run -----------------


def test_the_strip_starts_at_the_run_and_spans_a_day(report: BiasReport) -> None:
    """Criterion 4, on the two ends the geometry is measured between.

    The criterion says the next 24 hours from the run's as-of time.
    ``BiasReport.asof`` is a date and carries no time, so the only instant the
    run holds is ``generated_at``, which is what `STRIP_HOURS` documents and
    what `_view` uses. An origin of midnight on the as-of date would put a
    morning run's first six hours behind the left edge.

    Every position on the strip is a percentage of that span, so the two ends
    are what every band and tick is drawn against: pinned here rather than
    assumed by the tests that check individual positions.
    """
    view = _view(report, ScoringConfig(), DataConfig())

    assert view.origin == report.generated_at
    assert view.horizon - view.origin == timedelta(hours=STRIP_HOURS)
    assert STRIP_HOURS == 24
    assert view.at_pct(view.origin) == 0.0
    assert view.at_pct(view.horizon) == 100.0


# --- criterion 5: the windows arrive merged ---------------------------------


def test_the_strip_draws_the_windows_the_guard_merged(
    report: BiasReport, page: str
) -> None:
    """Criterion 5, on #198's own overlapping case.

    The fixture's two USD releases are 30 minutes apart, so the guard merges
    them into one window from 12:00 to 14:00. A strip that merged them again,
    or not at all, would draw two bands with a clear half hour between them,
    which is the half hour after a release when the first move reverses.
    """
    windows = list(blackout_windows(report.events, DataConfig()))
    drawn = re.findall(r'<div class="window"[^>]*title="([^"]*)"', strip(page))

    assert len(windows) == 2
    assert windows[0] == (
        report.events[0].scheduled_for.replace(hour=12, minute=0),
        report.events[1].scheduled_for.replace(hour=14, minute=0),
    )
    assert len(drawn) == len(windows)
    assert "12:00 to 14:00" in drawn[0]


# --- criterion 6: every time carries its zone -------------------------------


def test_every_hour_label_on_the_strip_names_its_zone(page: str) -> None:
    """Criterion 6. A bare 13:00 on a page read in Johannesburg is two hours out.

    Every instant on this page is aware UTC. The label has to say so, because
    the page is published and read away from the machine that made it, where
    nothing else on screen fixes the zone.
    """
    labels = re.findall(r'<span class="hour"[^>]*>([^<]*)</span>', strip(page))

    assert len(labels) == 4
    for label in labels:
        assert re.fullmatch(r"\d{2}:\d{2} UTC", label.strip()), label


def test_every_event_row_names_its_zone(report: BiasReport, page: str) -> None:
    """The same rule on the list under the strip, which is the accessible copy.

    A reader who cannot separate the bands reads the table instead, so a zone
    on the strip alone leaves the same ambiguity one element away.
    """
    rows = re.findall(
        r'<tr>\s*<td class="num">([^<]*)</td>', section(page, "Next 24 hours")
    )

    assert len(rows) == len(report.events)
    for printed in rows:
        assert re.fullmatch(r"\w{3} \d{2}:\d{2} UTC", printed.strip()), printed


# --- criterion 7: an empty strip is not a clear strip -----------------------


def test_a_run_with_a_consulted_calendar_carries_no_caveat(
    report: BiasReport, page: str
) -> None:
    """The caveat is not on every page, or nobody reads the one that matters.

    Asserted against the same run carrying one marker, so this cannot pass by
    there being no caveat on any page at all.
    """
    assert "calendar-note" not in page
    assert "calendar-note" in render_dashboard(marked(report, (UNCHECKED,), 1))


def test_a_run_whose_calendar_was_not_checked_says_so(report: BiasReport) -> None:
    """Criterion 7. No calendar means an empty strip, and the page has to say why.

    An offline run carries ``event:unchecked`` on every pair. The strip shades
    nothing, which is correct and says nothing: the reader has to be told the
    difference between a quiet day and a check that never ran.
    """
    page = render_dashboard(marked(report, (UNCHECKED,), 28))
    note = text(section(page, "Next 24 hours"))

    assert "Not checked on this run, on 28 of 28 pairs" in note
    assert "rather than a clear 24 hours" in note


def test_a_run_whose_calendar_could_not_answer_says_why(report: BiasReport) -> None:
    """The other of the two states, carrying the guard's own reason.

    ``event:unknown`` is a fetch that failed or a cached week that does not
    reach the date, and unlike an offline run it asks the reader to open a
    calendar now. One label over both would lose that.
    """
    reason = "the week is not cached"
    page = render_dashboard(marked(report, (f"{UNKNOWN}: {reason}",), 3))
    note = text(section(page, "Next 24 hours"))

    assert f"Calendar unknown on 3 of 28 pairs: {reason}." in note


def test_the_two_states_do_not_render_as_one(report: BiasReport) -> None:
    """A run with no calendar and a run whose calendar failed read differently.

    ADR 0002 rule 4: no guard supplied is a configuration choice, and a guard
    that could not answer is a failure. Only the second asks for action now, so
    a page giving both one sentence has thrown away the distinction the two
    suffixes exist to carry.
    """
    unchecked = text(
        section(render_dashboard(marked(report, (UNCHECKED,), 28)), "Next 24 hours")
    )
    unknown = text(
        section(
            render_dashboard(marked(report, (f"{UNKNOWN}: rate limited",), 28)),
            "Next 24 hours",
        )
    )

    assert unchecked != unknown
    assert "Calendar unknown" not in unchecked
    assert "Not checked on this run" not in unknown


def test_both_states_at_once_are_both_reported(report: BiasReport) -> None:
    """A run can hold one of each, and the page reports both rather than the first.

    A cached week that reaches some dates and not others produces exactly this:
    the pairs whose release is inside the cached range are answered, the ones
    past it are unknown, and an offline leg is unchecked.
    """
    pairs = tuple(
        replace(row, blockers=(UNCHECKED,) if index % 2 else (f"{UNKNOWN}: stale",))
        for index, row in enumerate(report.pairs)
    )
    note = text(
        section(render_dashboard(replace(report, pairs=pairs)), "Next 24 hours")
    )

    assert "Calendar unknown on 14 of 28" in note
    assert "Not checked on this run, on 14 of 28" in note
    assert note.index("Calendar unknown") < note.index("Not checked on this run")


def test_an_unknown_marker_with_no_reason_says_the_reason_is_missing(
    report: BiasReport,
) -> None:
    """A caveat ending in a colon reads as a reason the reader failed to see.

    `fbe.bias.BLOCKERS` says ``event:unknown`` carries its reason after the
    key, and a guard that emitted the bare key would leave the page printing
    "pairs: ." So the absence is named rather than rendered as an empty string,
    which is ADR 0002 rule 3 inside one sentence.
    """
    note = text(
        section(
            render_dashboard(marked(report, ("event" + UNKNOWN_SUFFIX,), 2)),
            "Next 24 hours",
        )
    )

    assert "Calendar unknown on 2 of 28 pairs, with no reason recorded." in note
    assert "pairs: ." not in note


def test_the_count_is_against_the_pairs_the_run_holds(report: BiasReport) -> None:
    """Tests the wire. A filtered run does not hold 28 pairs and must not say so.

    ``fbe bias --majors`` and the conviction and tradeability filters narrow the
    pool the report carries, so 28 is the shape of a full run rather than a
    property of a run. A hardcoded total reads correctly every morning and
    wrongly on exactly the runs a reader is already looking at sideways.
    """
    three = replace(report, pairs=report.pairs[:3])
    note = text(
        section(
            render_dashboard(marked(three, ("event" + UNCHECKED_SUFFIX,), 1)),
            "Next 24 hours",
        )
    )

    assert "Not checked on this run, on 1 of 3 pairs" in note


def test_the_caveat_names_the_reason_once_however_many_pairs_carry_it(
    report: BiasReport,
) -> None:
    """28 pairs carrying one failed fetch is one reason, not 28.

    The reason comes from the guard and is the same string on every pair it
    touched. Printed per pair it fills the panel it was added to and stops
    being read, which is the failure this rule shares with the run conditions
    above the matrix.
    """
    page = render_dashboard(marked(report, (f"{UNKNOWN}: rate limited",), 28))
    note = text(section(page, "Next 24 hours"))

    assert note.count("rate limited") == 1


# --- criteria 8 to 11: the phone, the guard and the claims ------------------


def test_the_strip_section_carries_the_same_events_as_a_readable_list(
    report: BiasReport, page: str
) -> None:
    """Criterion 8. At 400px the bands are thin, so the list is the fallback.

    Every event ticked on the strip is a row in the table under it, and the
    table is the element a narrow screen leaves readable. A strip carrying an
    event the list does not is a value available only by hovering, which a
    phone cannot do.
    """
    note = section(page, "Next 24 hours")
    ticks = re.findall(r'<div class="tick"[^>]*title="([^"]*)"', strip(page))

    assert len(ticks) == len(report.events)
    for event in report.events:
        assert event.title in note
        assert event.currency in note


def test_neither_section_fixes_a_width_a_phone_cannot_hold(page: str) -> None:
    """Criterion 8, on the rendered CSS rather than by eye.

    The three containers in these sections are the elements that decide whether
    the page scrolls sideways: the strip, the event table and the card grid. A
    pixel width on any of them is what puts a scrollbar on a 400px screen. The
    marks inside the strip are two pixels wide on purpose and are not
    containers, which is why this reads the containers by name rather than
    banning ``px`` from the section.

    The card grid goes to two columns only above 720px, so a phone gets one
    column and a card is as wide as the gutter leaves it.
    """
    sheet = "\n".join(re.findall(r"<style>(.*?)</style>", page, flags=re.S))

    for selector in (r"\.strip", r"table\.plain", r"\.cards"):
        match = re.search(selector + r"\s*\{([^}]*)\}", sheet)
        assert match is not None, selector
        assert not re.search(r"(?<!max-)width:\s*\d+px", match.group(1)), selector

    assert re.search(
        r"width:\s*100%", re.search(r"table\.plain\s*\{([^}]*)\}", sheet).group(1)
    )
    columns = re.search(
        r"@media \(min-width: (\d+)px\) \{ \.cards \{ grid-template-columns", sheet
    )
    assert columns is not None, "the card grid has no single-column default"
    assert int(columns.group(1)) > 400


def test_the_page_with_both_sections_is_publishable(page: str) -> None:
    """Criterion 9, through the guard rather than by eye."""
    assert check_constraints(page) == []


@pytest.mark.parametrize("word", FORBIDDEN)
def test_neither_section_claims_a_measured_result(page: str, word: str) -> None:
    """Criterion 10, on these two sections on their own."""
    for heading in ("Shortlist", "Next 24 hours"):
        assert word not in section(page, heading).lower()
