"""The blocker vocabulary, and the two renderers that have to show all of it.

`PairBias.blockers` carries two different kinds of string. Most of them say why
a pair was rejected. Two say that a check did not run, and those do not set
``tradeable``. Both renderers used to print `blockers` only on the untradeable
branch, so an offline run, which is any run without a calendar guard, showed an
unqualified "yes" on all 28 rows while `event:unchecked` sat on every one of
them. A calendar nobody consulted looked exactly like a calendar consulted and
found clear, which is the thing `apply_filters` says it exists to prevent.

The templates are rendered here directly, with a hand-built context and a Jinja
`Environment` pointed at the real template directory. `report.render_report` and
`dashboard.build.render_dashboard` are both still scaffolded, so going through
them would assert nothing. Rendering the template is what actually exercises the
branch this issue is about, and these tests fail against the old templates.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

import fbe
from fbe.bias import BLOCKERS, UNCHECKED_SUFFIX
from fbe.types import Conviction, Direction, PairBias

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
    """The smallest context ``report.md.j2`` renders with.

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
    }


def _render_report(pairs: tuple[PairBias, ...]) -> str:
    environment = _environment(PACKAGE_ROOT / "templates")
    return environment.get_template("report.md.j2").render(**_report_context(pairs))


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


def test_only_the_unchecked_markers_are_non_blocking() -> None:
    """The suffix is the rule, not a hardcoded pair of names.

    A renderer or a reader can decide what a marker means from its name alone,
    which is what lets ADR 0002 rule 4 add one without editing consumers.
    """
    for blocker, blocks in BLOCKERS.items():
        assert blocks is not blocker.endswith(UNCHECKED_SUFFIX), blocker


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


def _render_dashboard(idea: SimpleNamespace) -> str:
    """Render the dashboard around a single shortlist card.

    The ``view`` namespace is whatever the template calls for arithmetic, per
    the contract in its own header comment. Stubbed to fixed numbers here: this
    test is about one label, and the positioning maths has its own owner.
    """
    context: dict[str, Any] = {
        "report": SimpleNamespace(
            asof=ASOF,
            generated_at=GENERATED_AT,
            config_digest="abc123",
            shortlist=(idea,),
            events=(),
            warnings=(),
        ),
        "diff": None,
        "config": None,
        "grid": {},
        "pillar_order": (),
        "currencies": (),
        "pairs": (),
        "view": SimpleNamespace(
            title="FX bias",
            bar_pct=lambda value: 50.0,
            heat=lambda spread: "heat-p1",
            at_pct=lambda when: 50.0,
            span_pct=lambda a, b: 10.0,
            legend=(),
            hour_marks=(),
            blackouts=(),
        ),
    }
    environment = _environment(PACKAGE_ROOT / "dashboard" / "templates")
    return environment.get_template("dashboard.html.j2").render(**context)


def _idea(tradeable: bool, blockers: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
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


# --- the renderers are still stubs, and this pull request left them that way -


@pytest.mark.parametrize(
    "module_name, attribute",
    [
        ("fbe.report", "render_report"),
        ("fbe.report", "build_context"),
        ("fbe.dashboard.build", "render_dashboard"),
    ],
)
def test_the_render_entry_points_are_still_scaffolded(
    module_name: str, attribute: str
) -> None:
    """Guard against fixing the test by implementing the layer under it.

    These tests reach the templates directly, which is the only way to assert
    anything today. That is a workaround for the render path not existing, and
    when it does exist these assertions should move onto it. This test failing
    is the signal to do that, not a reason to delete it.

    Read from the source rather than called, because these take arguments a
    caller would have to invent, and inventing them is how a test starts
    asserting something other than what it says.
    """
    module = pytest.importorskip(module_name)
    source = inspect.getsource(getattr(module, attribute))

    assert "is scaffolded;" in source, (
        f"{module_name}.{attribute} has landed. Move the blocker assertions in "
        "this file onto the real render path."
    )
