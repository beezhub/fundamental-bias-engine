"""Agreement is a share of pillar weight, and every reader is told so.

`PairBias.agreement` is ``sum of w_pair over agreeing / sum of w_pair over
considered``. It is not the fraction of pillars that agree, and the two
quantities give different answers on the spec's own worked example: USDJPY has
four of seven pillars agreeing, which is 57%, and an agreement of 0.70.

Both renderers printed "70% of pillars agree" for that pair. The direction of
the error flatters the trade, on the line the shortlist offers as the reason to
take it, and it is the line read on a phone during a session. The same reading
sat on `ScoringConfig.min_agreement`, where an owner tightening the threshold
would reason "five of seven" and write a number meaning something else.

The value was right everywhere. Only the noun was wrong, so these tests are
about words, with one exception: the last one pins that the weight share and the
headcount genuinely differ. Without it, a later reader could make the label true
by changing the arithmetic to a headcount, which would discard the fact the
weighting exists to carry, that a heavy pillar dissenting is worse than a light
one dissenting.

The report assertions go through `fbe.report.render_report`, which is the path
a morning run takes. The dashboard has no such entry point yet, so its template
is rendered directly against the real template directory with a hand-built
context, the same approach and for the same reason as `tests/test_blockers.py`.

Nothing here reaches the network.
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
from fbe.config import ScoringConfig
from fbe.report import render_report
from fbe.types import BiasReport, Conviction, Direction, PairBias, TradeIdea

PACKAGE_ROOT = Path(fbe.__file__).resolve().parent
ASOF = date(2026, 3, 2)
GENERATED_AT = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)

FORBIDDEN = "of pillars agree"
"""The exact string both renderers carried, kept as one constant.

The criterion is a grep for it returning nothing across ``src/``, so the test
and the criterion look at the same characters rather than at two hand-copied
approximations of them.
"""

USDJPY_AGREEMENT = 0.7000
"""Section 7.7 of ``docs/scoring-spec.md``, pinned by test_worked_example too."""

USDJPY_PILLARS_AGREEING = 4
USDJPY_PILLARS_CONSIDERED = 7
"""MONETARY, INFLATION, GROWTH and EMPLOYMENT agree; EXTERNAL, POSITIONING and
RISK do not. Counted off the table in section 7.7 rather than computed, because
the point of the test using them is that a headcount is a different quantity."""


def _environment(directory: Path) -> Environment:
    """A Jinja environment over a real template directory.

    ``StrictUndefined`` so a context field the template needs and this file did
    not supply fails loudly rather than rendering empty, which would let a
    "the bad string is absent" assertion pass against a blank page.
    """
    return Environment(
        loader=FileSystemLoader(directory),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def _bias() -> PairBias:
    """One tradeable pair whose agreement is the published USDJPY figure."""
    return PairBias(
        pair="USDJPY",
        base="USD",
        quote="JPY",
        spread=0.7545,
        direction=Direction.LONG,
        conviction=Conviction.LOW,
        asof=ASOF,
        tradeable=True,
        blockers=(),
        base_score=0.81,
        quote_score=-0.61,
        agreement=USDJPY_AGREEMENT,
    )


def _idea() -> SimpleNamespace:
    return SimpleNamespace(
        bias=_bias(),
        rationale="Rates gap is wide.",
        size=None,
        blackout_until=None,
    )


def _base_context() -> dict[str, Any]:
    return {
        "report": SimpleNamespace(
            asof=ASOF,
            generated_at=GENERATED_AT,
            config_digest="abc123",
            shortlist=(_idea(),),
            events=(),
            warnings=(),
        ),
        "diff": None,
        "config": None,
        "grid": {},
        "pillar_order": (),
        "currencies": (),
        "pairs": (_bias(),),
    }


def _render_report() -> str:
    """Render the Markdown report the way ``fbe report`` renders it."""
    return render_report(
        BiasReport(
            asof=ASOF,
            generated_at=GENERATED_AT,
            currencies=(),
            pairs=(_bias(),),
            shortlist=(TradeIdea(bias=_bias(), rationale="Rates gap is wide."),),
            config_digest="abc123",
        )
    )


def _render_dashboard() -> str:
    context = _base_context()
    context["view"] = SimpleNamespace(
        title="FX bias",
        bar_pct=lambda value: 50.0,
        heat=lambda spread: "heat-p1",
        at_pct=lambda when: 50.0,
        span_pct=lambda a, b: 10.0,
        legend=(),
        hour_marks=(),
        blackouts=(),
    )
    environment = _environment(PACKAGE_ROOT / "dashboard" / "templates")
    return environment.get_template("dashboard.html.j2").render(**context)


def _agreement_sentence(rendered: str, *, after: str) -> str:
    """The clause around the agreement percentage in the shortlist.

    ``after`` marks where the shortlist starts, and it is not optional. The
    pair table prints the same figure 28 times as a bare cell, so searching the
    whole document finds ``"70% | yes |"`` from the table and never reaches the
    sentence under test. Returned rather than asserted on in place so a failure
    message shows what the renderer actually said.
    """
    head, separator, tail = rendered.partition(after)
    assert separator, f"no {after!r} in:\n{rendered[:800]}"
    match = re.search(r"70%[^.<\n]*", tail)
    if match is None:
        raise AssertionError(
            f"no agreement percentage after {after!r} in:\n{tail[:800]}"
        )
    return match.group(0).strip()


REPORT_SHORTLIST = "## 3. Tradeable shortlist"
DASHBOARD_CARD = '<p class="meta num">'
"""Where each renderer's shortlist entry begins, for `_agreement_sentence`."""


# --- what the trader reads ---------------------------------------------------


def test_the_report_does_not_call_the_figure_a_share_of_pillars() -> None:
    """The shortlist line, which is the one offered as the reason to trade."""
    sentence = _agreement_sentence(_render_report(), after=REPORT_SHORTLIST)

    assert FORBIDDEN not in sentence, sentence
    assert "weight" in sentence, sentence


def test_the_dashboard_does_not_call_the_figure_a_share_of_pillars() -> None:
    """The same line on the phone, which is where the morning routine happens."""
    sentence = _agreement_sentence(_render_dashboard(), after=DASHBOARD_CARD)

    assert FORBIDDEN not in sentence, sentence
    assert "weight" in sentence, sentence


def test_both_renderers_describe_the_figure_with_the_same_words() -> None:
    """One number rendered twice cannot be allowed to drift into two readings.

    The two templates are owned as one surface and were wrong in exactly the
    same way, which is what a shared string prevents recurring.
    """
    report = _agreement_sentence(_render_report(), after=REPORT_SHORTLIST)
    dashboard = _agreement_sentence(_render_dashboard(), after=DASHBOARD_CARD)

    assert report.rstrip(".") == dashboard.rstrip("."), (report, dashboard)


def test_the_pair_table_says_its_agreement_column_is_a_weight_share() -> None:
    """The table gives the figure 28 times with only a column header to explain it.

    A reader who never opens the shortlist meets the number here first, so the
    header or the note under the table has to carry the same correction.
    """
    table = _pair_table_region(_render_report())

    assert "weight" in table.lower(), table


def _pair_table_region(rendered: str) -> str:
    """The pair table's header row and the prose directly under the table.

    Scoped deliberately tightly. Taking the whole of section 2 let this test
    pass against the unchanged template, because an unrelated sentence about
    coverage two paragraphs earlier contains the word "weight". A test that
    passes before the fix is testing nothing.
    """
    lines = rendered.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("| Pair |"):
            rest = lines[index:]
            break
    else:
        raise AssertionError(f"no pair table in:\n{rendered[:800]}")

    region = []
    for line in rest:
        if line.startswith("## "):
            break
        region.append(line)
    return "\n".join(region)


def test_no_file_under_src_says_of_pillars_agree() -> None:
    """The criterion as written, over the whole package rather than two files.

    Checked by walking `src/` instead of the two known templates, so a third
    renderer added later cannot reintroduce the phrase unnoticed.
    """
    offenders = [
        path.relative_to(PACKAGE_ROOT.parent)
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".j2", ".md", ".html"}
        and FORBIDDEN in path.read_text(encoding="utf-8", errors="ignore")
    ]

    assert not offenders, offenders


# --- what the owner reads when moving the threshold --------------------------


def test_min_agreement_says_what_it_is_compared_against() -> None:
    """The #50 shape: the field names the quantity and where it is defined.

    `max_dispersion`, `min_coverage` and `coverage_demotion` all landed this
    way. `min_agreement` was left behind and is the one whose plain-English
    reading is furthest from what it does.
    """
    doc = _min_agreement_docstring()

    assert "PairBias.agreement" in doc, doc
    assert "w_pair" in doc, doc
    assert "5.3" in doc, doc


def test_min_agreement_says_w_pair_is_the_mean_of_the_two_legs() -> None:
    """Without this, "pillar weight" still reads as the configured weight.

    The quantity is the mean of the two legs' post-staleness effective weights,
    so a stale leg moves it, and a reader who thinks it is the `ScoringConfig`
    weight will be wrong about every pair carrying old data.
    """
    doc = _min_agreement_docstring().lower()

    assert "mean" in doc, doc
    assert "effective" in doc, doc
    assert "staleness" in doc or "post-staleness" in doc, doc


def test_min_agreement_says_plainly_that_it_is_not_a_count_of_pillars() -> None:
    """The criterion asks for this in so many words, and the reason is arithmetic.

    An owner reasoning "at least five of seven" writes 0.71. Against the real
    quantity that excludes every pair whose dissent includes MONETARY at 0.30,
    whatever the other six do, because one heavy dissenter puts the ratio at
    0.70. Saying only what it *is* leaves that inference available.
    """
    doc = _min_agreement_docstring().lower()

    assert "not a count" in doc or "not the fraction of pillars" in doc, doc


def _min_agreement_docstring() -> str:
    """The docstring attached to `ScoringConfig.min_agreement`.

    Read out of the source rather than from ``__doc__``, because an attribute
    docstring on a dataclass field is not kept at runtime.
    """
    source = inspect.getsource(ScoringConfig)
    after = source.split("min_agreement:", 1)[1]
    match = re.search(r'"""(.*?)"""', after, re.DOTALL)
    assert match is not None, f"no docstring after min_agreement in:\n{after[:400]}"
    return match.group(1)


# --- why the label matters ---------------------------------------------------


def test_the_weight_share_and_the_headcount_are_different_numbers() -> None:
    """The defect in one assertion, on the spec's own worked pair.

    This is the test that stops the label being made true by changing the
    arithmetic to a headcount. Weighting is the whole point: MONETARY at 0.30
    dissenting is a materially worse sign than POSITIONING at 0.10 dissenting,
    and `bias.agreement`'s own docstring says a headcount would treat them as
    equal.
    """
    headcount = USDJPY_PILLARS_AGREEING / USDJPY_PILLARS_CONSIDERED

    assert round(headcount, 4) == 0.5714
    assert abs(USDJPY_AGREEMENT - headcount) > 0.12, (USDJPY_AGREEMENT, headcount)
