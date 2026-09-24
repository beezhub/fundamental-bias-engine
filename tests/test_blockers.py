"""The blocker vocabulary, and the two renderers that have to show all of it.

`PairBias.blockers` carries three different kinds of string. Most of them say
why a pair was rejected. Two say that a check did not run at all, and one says
a check ran and could not tell; none of those three set ``tradeable``. Both
renderers used to print `blockers` only on the untradeable branch, so an
offline run, which is any run without a calendar guard, showed an unqualified
"yes" on all 28 rows while `event:unchecked` sat on every one of them. A
calendar nobody consulted looked exactly like a calendar consulted and found
clear, which is the thing `apply_filters` says it exists to prevent. A
calendar the guard tried and failed to consult is the same defect wearing a
different cause, which is what `event:unknown` exists to separate out
(issue #43): a broken fetch and a genuinely quiet week must not look alike
either.

The report assertions go through `fbe.report.render_report`, which is the real
path a morning run takes: the branch under test is in the template, and a test
that reached it another way would stop proving the run reaches it. The
dashboard has no such path yet, so `dashboard.build.render_dashboard` is still
scaffolded and its assertions still render the template directly with a
hand-built context. The guard at the foot of this file says so, and it is the
signal to move them when that entry point lands.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

import fbe
from fbe.bias import BLOCKERS, UNCHECKED_SUFFIX, UNKNOWN_SUFFIX
from fbe.dashboard.build import render_dashboard
from fbe.report import render_report
from fbe.types import BiasReport, Conviction, Direction, PairBias, TradeIdea

PACKAGE_ROOT = Path(fbe.__file__).resolve().parent
SPEC = Path(__file__).resolve().parents[1] / "docs" / "scoring-spec.md"

ASOF = date(2026, 6, 30)
GENERATED_AT = datetime(2026, 6, 30, 17, 0, tzinfo=UTC)


def _environment(directory: Path) -> Environment:
    """A Jinja environment over a real template directory.

    ``StrictUndefined`` so a context field the template needs and the test did
    not supply fails the test instead of rendering as an empty string, which
    would let an assertion pass against a blank table.
    """
    return Environment(
        loader=FileSystemLoader(directory),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _bias(
    pair: str = "EURUSD",
    tradeable: bool = True,
    blockers: tuple[str, ...] = (),
) -> PairBias:
    """A LONG, MEDIUM conviction bias, varying only tradeability and blockers."""
    return PairBias(
        pair=pair,
        base=pair[:3],
        quote=pair[3:],
        spread=1.42,
        direction=Direction.LONG,
        conviction=Conviction.MEDIUM,
        asof=ASOF,
        tradeable=tradeable,
        blockers=blockers,
        base_score=0.81,
        quote_score=-0.61,
        agreement=0.71,
    )


def _report_context(pairs: tuple[PairBias, ...]) -> dict[str, Any]:
    """The smallest context the dashboard template renders with.

    Everything outside the pair table is emptied rather than populated: no
    currencies, no grid, no shortlist, no events, no warnings, no diff, and no
    config, which skips the pillar weights block. The pair table is the subject
    and the rest of the page only has to not raise.
    """
    return {
        "report": SimpleNamespace(
            asof=ASOF,
            generated_at=GENERATED_AT,
            config_digest="abc123",
            shortlist=(),
            events=(),
            warnings=(),
        ),
        "diff": None,
        "config": None,
        "grid": {},
        "pillar_order": (),
        "currencies": (),
        "pairs": pairs,
        # Read by the Markdown template to tell a failed fetch from an offline
        # run. Taken from `fbe.bias` rather than written out, for the reason
        # `build_context` supplies it at all: one copy of the string.
        "unknown_prefix": "event" + UNKNOWN_SUFFIX,
    }


def _render_report(pairs: tuple[PairBias, ...]) -> str:
    """Render the Markdown report the way ``fbe report`` renders it.

    No currencies and no shortlist, so the pair table is the only populated
    section. The grid is built from the same pairs by `fbe.report.build_context`
    rather than emptied, because that is what a run does and a mirrored cell
    carries its own copy of every marker under test.
    """
    return render_report(
        BiasReport(
            asof=ASOF,
            generated_at=GENERATED_AT,
            currencies=(),
            pairs=pairs,
            config_digest="abc123",
        )
    )


def _tradeable_cell(rendered: str, pair: str) -> str:
    """The last column of the pair table's row for `pair`."""
    for line in rendered.splitlines():
        if line.startswith(f"| {pair} |"):
            return line.rstrip().rstrip("|").rsplit("|", 1)[-1].strip()
    raise AssertionError(f"no pair-table row for {pair} in:\n{rendered}")


# --- the spec table and the code agree --------------------------------------


def _spec_blocker_table() -> dict[str, bool]:
    """Parse section 6's blocker table into ``{blocker: blocks}``.

    Parsed rather than restated so the test reads the document a human reads. A
    second hand-written copy here would be a third place for the list to drift.
    """
    section = SPEC.read_text().split("## 6. Hard filters")[1].split("\n## ")[0]
    rows = re.findall(r"^\|\s*`([^`]+)`\s*\|\s*(yes|no)\s*\|", section, re.MULTILINE)
    assert rows, f"no blocker rows parsed from section 6:\n{section[:400]}"
    return {name: blocks == "yes" for name, blocks in rows}


def test_the_spec_table_and_the_code_enumeration_match() -> None:
    """Criterion 5. The list exists twice and the two copies must not drift.

    They had already drifted when this was filed: the spec listed four blockers
    and the code documented seven.
    """
    assert _spec_blocker_table() == dict(BLOCKERS)


def test_only_the_suffixed_markers_are_non_blocking() -> None:
    """The suffix is the rule, not a hardcoded pair of names.

    A renderer or a reader can decide what a marker means from its name alone,
    which is what lets ADR 0002 rule 4 add one without editing consumers.
    `UNKNOWN_SUFFIX` is the second name added under that rule, for a check
    that ran and could not tell, distinct from `UNCHECKED_SUFFIX`'s check that
    did not run at all.
    """
    for blocker, blocks in BLOCKERS.items():
        is_provisional = blocker.endswith(UNCHECKED_SUFFIX) or blocker.endswith(
            UNKNOWN_SUFFIX
        )
        assert blocks is not is_provisional, blocker


def test_unknown_suffix_is_distinct_from_unchecked() -> None:
    """Two different failures need two different names (ADR 0002 rule 4).

    A guard that never ran and a guard that ran and could not tell call for
    different responses from the trader, so the marker must say which one
    happened rather than collapsing both into `event:unchecked`.
    """
    assert UNKNOWN_SUFFIX != UNCHECKED_SUFFIX
    assert "event" + UNKNOWN_SUFFIX in BLOCKERS
    assert "event" + UNKNOWN_SUFFIX != "event" + UNCHECKED_SUFFIX


# --- the Markdown report ----------------------------------------------------


def test_an_unchecked_marker_shows_on_a_tradeable_pair() -> None:
    """Criterion 2, and the defect itself. Fails against the old template.

    The old cell was ``{{ 'yes' if bias.tradeable else blockers|join }}``, which
    printed a bare "yes" here.
    """
    rendered = _render_report((_bias(blockers=("event:unchecked",)),))

    assert "event:unchecked" in rendered
    assert _tradeable_cell(rendered, "EURUSD") == "yes (event:unchecked)"


def test_several_unchecked_markers_all_show() -> None:
    """A fully offline run has no calendar and no cost input, so it has both."""
    rendered = _render_report((_bias(blockers=("cost:unchecked", "event:unchecked")),))

    assert _tradeable_cell(rendered, "EURUSD") == (
        "yes (cost:unchecked, event:unchecked)"
    )


def test_an_unknown_marker_shows_on_a_tradeable_pair_and_carries_its_reason() -> None:
    """Issue #43. A guard that ran and failed is not the same as no guard at all.

    `event:unknown` mirrors `event:unchecked` in that it does not block, but it
    is a different marker with a different cause, and the reason travels with
    it the same way the `event` blocker's reason does.
    """
    reason = "event:unknown: fetch failed for EUR"
    rendered = _render_report((_bias(blockers=(reason,)),))

    assert _tradeable_cell(rendered, "EURUSD") == f"yes ({reason})"


def test_unknown_and_unchecked_markers_both_show_together() -> None:
    """A run whose cost input is missing and whose calendar fetch failed."""
    rendered = _render_report(
        (_bias(blockers=("cost:unchecked", "event:unknown: fetch failed for EUR")),)
    )

    assert _tradeable_cell(rendered, "EURUSD") == (
        "yes (cost:unchecked, event:unknown: fetch failed for EUR)"
    )


def test_a_clean_tradeable_pair_still_reads_yes() -> None:
    """No marker, no brackets. The common case must not gain noise."""
    rendered = _render_report((_bias(),))

    assert _tradeable_cell(rendered, "EURUSD") == "yes"


def test_an_untradeable_pair_still_lists_its_blockers() -> None:
    """The behaviour that already worked, pinned so the fix does not cost it."""
    rendered = _render_report(
        (_bias(tradeable=False, blockers=("coverage", "no_edge")),)
    )

    assert _tradeable_cell(rendered, "EURUSD") == "coverage, no_edge"


def test_an_event_blocker_carrying_its_reason_renders_whole() -> None:
    """`event` is emitted as the guard's reason, not as the literal "event".

    `CalendarGuard` returns a string naming the event, its currency and its
    scheduled time, and `apply_filters` appends that. So the one row of
    `BLOCKERS` that is a kind rather than a literal has to survive the renderer
    intact, colon and spaces included.
    """
    reason = "event: EUR CPI at 09:00 UTC"
    rendered = _render_report((_bias(tradeable=False, blockers=(reason,)),))

    assert _tradeable_cell(rendered, "EURUSD") == reason


def test_a_marker_renders_without_being_named_in_the_template() -> None:
    """ADR 0002 rule 4 will add a marker, and it must render on arrival.

    Deliberately a string no template and no test fixture knows about. If the
    renderer ever starts matching on marker names, this is what catches it.
    """
    rendered = _render_report((_bias(blockers=("event:failed",)),))

    assert _tradeable_cell(rendered, "EURUSD") == "yes (event:failed)"


def test_the_shortlist_does_not_call_an_unchecked_marker_a_blocker() -> None:
    """A shortlisted idea is tradeable, so nothing on it blocked anything."""
    context = _report_context(())
    context["report"].shortlist = (
        SimpleNamespace(
            bias=_bias(blockers=("event:unchecked",)),
            rationale="Rates gap is wide and the pillars agree.",
            size=None,
            blackout_until=None,
        ),
    )
    environment = _environment(PACKAGE_ROOT / "templates")
    rendered = environment.get_template("report.md.j2").render(**context)

    assert "Not checked: event:unchecked" in rendered
    assert "Blockers: event:unchecked" not in rendered


# --- the dashboard ----------------------------------------------------------


def _render_dashboard(idea: TradeIdea) -> str:
    """Render the dashboard the way ``fbe dashboard`` renders it.

    Through `fbe.dashboard.build.render_dashboard` rather than through the
    template with a hand-built context. It was the second for as long as the
    render path was scaffolded, and the tripwire below was what said to change
    it once the path existed.

    The report carries the card's own pair and nothing else, so the shortlist
    is the only populated section and the grid is the one cell that pair makes.
    """
    return render_dashboard(
        BiasReport(
            asof=ASOF,
            generated_at=GENERATED_AT,
            currencies=(),
            pairs=(idea.bias,),
            shortlist=(idea,),
            config_digest="abc123",
        )
    )


def _idea(tradeable: bool, blockers: tuple[str, ...]) -> TradeIdea:
    return TradeIdea(
        bias=_bias(tradeable=tradeable, blockers=blockers),
        rationale="Rates gap is wide and the pillars agree.",
        size=None,
        blackout_until=None,
    )


def _blocker_label(rendered: str) -> str:
    """The heading the dashboard puts above a card's blocker list.

    Pulled out of the surrounding markup rather than matched as a bare
    substring, so the assertions cannot pass or fail on the word "Blocked"
    appearing somewhere else on the page later.
    """
    match = re.search(
        r'<p class="meta">\s*(Not checked on this run|Blocked):\s*</p>', rendered
    )
    if match is None:
        raise AssertionError(f"no blocker-list label rendered in:\n{rendered[:600]}")
    return match.group(1)


def test_the_dashboard_says_an_unchecked_marker_was_not_checked() -> None:
    """Criterion 3. The list rendered before, unlabelled and so unreadable.

    The dashboard never printed "yes", so the defect takes a different shape
    here: markers and real blockers went into one bare ``<ul>`` and a reader had
    no way to tell "we did not look" from "we looked and it failed".
    """
    rendered = _render_dashboard(_idea(True, ("event:unchecked",)))

    assert _blocker_label(rendered) == "Not checked on this run"
    assert "<li>event:unchecked</li>" in rendered


def test_the_dashboard_still_says_blocked_for_a_real_blocker() -> None:
    """The label follows tradeability rather than assuming the shortlist rule."""
    rendered = _render_dashboard(_idea(False, ("coverage",)))

    assert _blocker_label(rendered) == "Blocked"


def test_the_dashboard_says_an_unknown_marker_was_not_checked_too() -> None:
    """A guard that ran and failed reads the same as no guard, on this label.

    Both are "we could not tell you", which is what the label promises; the
    reason each one carries is what tells them apart, and that reason is
    still shown in the list itself.
    """
    reason = "event:unknown: fetch failed for EUR"
    rendered = _render_dashboard(_idea(True, (reason,)))

    assert _blocker_label(rendered) == "Not checked on this run"
    assert f"<li>{reason}</li>" in rendered


# --- both renderers are the real ones now -----------------------------------


def test_the_dashboard_assertions_go_through_the_real_render_path() -> None:
    """The move the tripwire here asked for, pinned so it cannot go back.

    This file used to carry `test_the_render_entry_points_are_still_scaffolded`,
    which failed the day `fbe.dashboard.build.render_dashboard` landed and said
    to move these assertions onto it. They have moved, so the tripwire has done
    its job and this is what replaces it: a later change that reverted
    `_render_dashboard` to rendering the template with a hand-built context
    would keep every dashboard assertion in this file green while proving
    nothing about the page ``fbe dashboard`` writes.
    """
    assert "render_dashboard(" in inspect.getsource(_render_dashboard)


def test_the_report_assertions_go_through_the_real_render_path() -> None:
    """The move above, pinned so it cannot quietly go back.

    A later change that reverted `_render_report` to rendering the template
    with a hand-built context would keep every assertion in this file green
    while proving nothing about what ``fbe report`` writes.
    """
    assert "render_report(" in inspect.getsource(_render_report)
