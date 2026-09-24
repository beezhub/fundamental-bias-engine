"""``render_dashboard``: one run, laid out for a phone, saying what it does not know.

The dashboard and the Markdown report are two views of one run, so the failure
worth guarding against here is not an exception, it is drift. Both render from
`fbe.report.build_context`, and the tests below drive both from one context so
that a view which recomputed anything shows up as a disagreement rather than as
a page that merely looks plausible.

The second failure this file exists for is quieter still. Every constraint the
hosting sandbox enforces fails silently at view time: a blocked asset leaves a
blank panel, a palette defined only inside a media query leaves the page in the
wrong theme, and both reach the owner's phone looking like a page with nothing
to say. So the publishable assertion is `check_constraints` run on the real
output rather than an eye over the markup.

The third is the one the repository's own decision record is about. A currency
the run could not score and a currency the model scored at the middle of its
band are opposite facts, and a bar drawn at zero says the second when the first
is true. `docs/decisions/0002-representing-not-known.md` is the general form.

Everything renders from ``tests/fixtures/dashboard_report.json``, a committed
`fbe.types.BiasReport` sidecar read back through `fbe.report.load_report`, so
the page under test is built from a report that survived a round trip rather
than from one assembled in memory. Nothing here reaches the network.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, UndefinedError

from fbe.config import Config, DataConfig, RiskConfig, ScoringConfig
from fbe.dashboard.build import TEMPLATE_NAME, check_constraints, render_dashboard
from fbe.report import (
    CurrencyChange,
    ReportDiff,
    build_context,
    load_report,
    render_report,
)
from fbe.types import BiasReport, Conviction, Direction

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_report.json"
"""The committed run every test renders.

Committed rather than built in a helper because issue #253 renders the matrix
from the same one: two fixtures drifting apart would let the two halves of one
page be tested against different runs.
"""

TEMPLATE_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "fbe" / "dashboard" / "templates"
)

FORBIDDEN = (
    "backtest",
    "backtested",
    "proven",
    "win rate",
    "hit rate",
    "edge",
    "outperform",
)
"""Language `CLAUDE.md` forbids anywhere in the repository.

Listed here rather than imported because there is nothing to import: the rule
lives in prose, and this is the only thing that enforces it on the page a
person actually reads.
"""


@pytest.fixture(scope="module")
def report() -> BiasReport:
    return load_report(FIXTURE)


@pytest.fixture(scope="module")
def page(report: BiasReport) -> str:
    return render_dashboard(report)


def config_with(**scoring: float) -> Config:
    """A config whose spread bands are exactly the ones given."""
    return Config(
        risk=RiskConfig(),
        scoring=ScoringConfig(**scoring),
        data=DataConfig(),
    )


def styles(html: str) -> str:
    """Everything between the page's ``<style>`` tags."""
    blocks = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    assert blocks, "the page carries no stylesheet"
    return "\n".join(blocks)


# --- one publishable document ------------------------------------------------


def test_the_rendered_page_is_publishable(page: str) -> None:
    """`check_constraints` on the real output, not an eye over the markup.

    Every constraint it checks fails silently when it is broken, so a page that
    renders is not evidence of anything. This is the assertion that says the
    page reaching the phone is the page that was checked.
    """
    assert check_constraints(page) == []


def test_the_page_is_one_html_document(page: str) -> None:
    """One doctype, one html element, and no external asset beyond the hosts."""
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert page.count("<html") == 1
    assert page.rstrip().endswith("</html>")


def test_the_document_carries_exactly_one_title(page: str) -> None:
    """The tab's name. An SVG title is a chart's accessible name, not this."""
    titles = re.findall(r"<title>(.*?)</title>", page, flags=re.S)

    assert len(titles) == 1
    assert titles[0].strip()


# --- the same numbers as the Markdown report ---------------------------------


def test_the_ranking_order_matches_the_markdown_report(report: BiasReport) -> None:
    """Both views, one context, so neither can reorder the other's run.

    Asserted as the order the currency codes appear in each document rather
    than as two sorted lists, because two lists sorted the same way agree by
    construction and would pass while a template that reordered its own rows
    still drifted.
    """
    codes = [row.currency for row in build_context(report)["currencies"]]

    dashboard = re.findall(r"<strong>([A-Z]{3})</strong>", render_dashboard(report))
    markdown = re.findall(r"^\| *\d+ \| ([A-Z]{3}) \|", render_report(report), re.M)

    assert dashboard == codes
    assert markdown == codes


def test_every_composite_on_the_page_is_the_score_from_the_report(
    report: BiasReport, page: str
) -> None:
    """Each currency's own number, not a recomputation that rounds elsewhere.

    A view that re-derived the composite from the pillars would print a
    plausible number for the wrong reason, and the fixture's pillar scores are
    deliberately not the composite so that case fails here.
    """
    for row in report.currencies:
        if row.coverage <= 0:
            continue
        assert f"{row.composite:+.2f}" in page, row.currency


def test_the_config_digest_is_in_the_header(report: BiasReport, page: str) -> None:
    """An old page has to be tie-able to the weights that produced it.

    Without it a stored dashboard is a set of numbers with no way to tell
    whether a re-weighting has happened since, which is the question
    ``--compare`` exists to answer on the report side.
    """
    header = page.split("<h2>", 1)[0]

    assert report.config_digest in header


# --- no arithmetic in the template -------------------------------------------


class _StubView:
    """A view whose geometry is sentinels, so the template cannot compute it.

    Every method returns a value no real run would produce. If the rendered
    page carries them, the template took what it was handed; if it carries
    anything else, the template did its own arithmetic, which is arithmetic
    nobody can unit test.
    """

    title = "stub title"
    legend = (("heat-p1", "stub legend"),)
    blackouts = ()
    hour_marks = ()

    def bar_pct(self, value: float) -> float:
        return 37.25

    def heat(self, spread: float) -> str:
        return "heat-n2"

    def at_pct(self, when: datetime) -> float:
        return 11.5

    def span_pct(self, start: datetime, end: datetime) -> float:
        return 4.25


def test_the_template_renders_the_geometry_it_is_handed(report: BiasReport) -> None:
    """Bar widths, cell colours and strip positions all come from Python."""
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=True,
    )
    rendered = environment.get_template(TEMPLATE_NAME).render(
        **build_context(report), view=_StubView()
    )

    assert "<title>stub title</title>" in rendered
    assert "width: 37.25%" in rendered
    assert "heat-n2" in rendered
    assert "left: 11.5%" in rendered


def test_a_context_key_the_template_needs_and_does_not_get_raises(
    report: BiasReport,
) -> None:
    """``StrictUndefined``, for the reason `render_report` gives at the same call.

    Jinja's default renders an unknown name as an empty string, so a renamed
    context key would empty a panel and leave a page that still looks like a
    dashboard.
    """
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        autoescape=True,
    )

    with pytest.raises(UndefinedError):
        environment.get_template(TEMPLATE_NAME).render(**build_context(report))


# --- the ranking says what it knows and what it does not ---------------------


def test_a_currency_the_run_could_not_score_gets_no_bar(
    report: BiasReport, page: str
) -> None:
    """Zero coverage is not a score of zero, and a bar at zero says it is.

    The fixture's JPY scored on no data at all. Drawn at the centre line it
    would be indistinguishable from a currency the model placed at the middle
    of its band, which is the opposite fact and the one a reader would act on.
    """
    unscored = [row for row in report.currencies if row.coverage <= 0]
    assert unscored, "the fixture no longer carries an unscored currency"

    row = _ranking_row(page, unscored[0].currency)

    assert "rank-bar" not in row
    assert "not scored" in row


def test_a_currency_below_full_coverage_prints_its_coverage(
    report: BiasReport, page: str
) -> None:
    """The figure, not only a fade. A 60% score is worth less than a 100% one.

    Phase 5 asks for thin coverage to stay visible, and an opacity change alone
    is not visible: it reads as a styling choice rather than as a fact about
    the data behind the number.
    """
    thin = [row for row in report.currencies if 0 < row.coverage < 1]
    assert thin, "the fixture no longer carries a thin currency"

    row = _ranking_row(page, thin[0].currency)

    assert f"{thin[0].coverage * 100:.0f}%" in row


def test_every_bar_prints_its_number_beside_it(report: BiasReport, page: str) -> None:
    """No value carried by colour alone, and none carried by width alone."""
    for row in report.currencies:
        if row.coverage <= 0:
            continue
        rendered = _ranking_row(page, row.currency)
        assert "rank-bar" in rendered
        assert f"{row.composite:+.2f}" in rendered


def test_the_bars_grow_from_a_centre_line_and_are_coloured_by_sign(
    report: BiasReport, page: str
) -> None:
    """Diverging, because the sign is the message.

    A sequential ramp would make the strongest and the weakest currency the two
    ends of one scale, which is true of the magnitude and false of the meaning.
    """
    scored = [row for row in report.currencies if row.coverage > 0]
    positive = [row for row in scored if row.composite > 0]
    negative = [row for row in scored if row.composite < 0]
    assert positive and negative

    assert "rank-bar pos" in _ranking_row(page, positive[0].currency)
    assert "rank-bar neg" in _ranking_row(page, negative[0].currency)

    sheet = styles(page)
    assert ".rank-track::before" in sheet
    assert ".rank-bar.pos" in sheet and "left: 50%" in sheet
    assert ".rank-bar.neg" in sheet and "right: 50%" in sheet


def _ranking_row(page: str, currency: str) -> str:
    """The one ranking row for a currency, markup included."""
    rows = re.findall(r'<div class="rank-row[^"]*">(.*?)\n    </div>', page, flags=re.S)
    matching = [row for row in rows if f"<strong>{currency}</strong>" in row]
    assert len(matching) == 1, f"{currency}: {len(matching)} rows"
    return matching[0]


# --- the palette, the theme and the phone ------------------------------------


def test_the_palette_is_defined_on_bare_root_and_overridden_in_both_ways(
    page: str,
) -> None:
    """Checked through `check_constraints`, which owns the rule, plus the toggle.

    The guard cannot see whether the toggle exists, only whether the blocks it
    would drive are present, so the button is asserted here.
    """
    assert check_constraints(page) == []
    assert "data-theme-toggle" in page
    assert 'setAttribute("data-theme"' in page


def test_the_gutter_is_set_once_and_the_page_never_scrolls_sideways(
    page: str,
) -> None:
    """At 400px the text needs a gutter, and a shorthand can silently zero it.

    ``padding`` with two values sets the sides; with one it sets all four; and
    a later ``padding: 20px 0`` on the same wrapper wipes the gutter out with
    nothing to show for it on any screen wide enough to test by eye.
    """
    sheet = styles(page)
    wrapper = re.search(r"\.wrap\s*\{(.*?)\}", sheet, flags=re.S)
    assert wrapper is not None

    body = wrapper.group(1)
    assert "padding-inline: var(--gutter)" in body
    assert "padding-block:" in body
    assert "padding:" not in body
    assert re.search(r"--gutter:\s*16px", sheet)
    assert sheet.count("padding-inline: var(--gutter)") == 1
    assert ".scroll-x" in sheet and "overflow-x: auto" in sheet


# --- the footer --------------------------------------------------------------


def test_the_warnings_and_coverage_render_in_the_footer(
    report: BiasReport, page: str
) -> None:
    """Both matter and neither is the first thing on the screen."""
    assert report.warnings
    footer = page.split("<h2>Coverage and warnings</h2>", 1)
    assert len(footer) == 2

    for warning in report.warnings:
        assert warning in footer[1]


def test_a_run_with_no_baseline_says_so_rather_than_printing_an_empty_section(
    page: str,
) -> None:
    """An empty diff table reads as a run that changed nothing, which is a
    claim about the market rather than about the absence of a baseline."""
    since = page.split("<h2>Since the last run</h2>", 1)[1]

    assert "No baseline run to compare against." in since
    assert "<th>Delta</th>" not in since


def test_a_diff_renders_its_currency_moves(report: BiasReport) -> None:
    """And the deltas come from the diff, not from subtracting on the page."""
    diff = ReportDiff(
        previous_asof=date(2026, 6, 29),
        current_asof=report.asof,
        currencies=(
            CurrencyChange(
                currency="USD",
                previous_composite=1.20,
                current_composite=1.84,
                delta=0.64,
            ),
        ),
        pairs=(),
        shortlist_added=(),
        shortlist_removed=(),
        config_changed=False,
    )

    rendered = render_dashboard(report, diff=diff)

    assert "+0.64" in rendered
    assert "No baseline run to compare against." not in rendered


# --- nothing on the page claims a result nobody measured ---------------------


@pytest.mark.parametrize("word", FORBIDDEN)
def test_the_page_claims_no_measured_result(page: str, word: str) -> None:
    """The standing instruction in `CLAUDE.md`, enforced on the rendered page.

    Nothing in this project has been validated against out-of-sample returns,
    so a page that said otherwise would be the one file in the repository a
    person acts on while it says something untrue.
    """
    assert word not in page.lower()


def test_no_number_on_the_page_is_presented_as_a_historical_result(
    page: str,
) -> None:
    """The closest the layout comes is the run-to-run diff, and it is labelled
    as a comparison of two runs rather than as performance.

    Asserted over the prose rather than the whole document: the inline script
    carries an early ``return``, and a search over the markup would read
    control flow as a claim about returns.
    """
    prose = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    prose = re.sub(r"<style>.*?</style>", "", prose, flags=re.S)

    assert "Since the last run" in prose
    assert "return" not in prose.lower()
    assert "performance" not in prose.lower()


# --- the heat steps follow the conviction bands ------------------------------


def test_the_heat_steps_come_from_the_configured_bands(report: BiasReport) -> None:
    """So a cell's colour and the conviction beside it cannot disagree.

    Moving the bands in config moves the colours with them. A scale with its
    own thresholds would keep colouring a pair as strong after the desk had
    decided that width was not worth trading.
    """
    wide = config_with(min_spread_low=0.1, min_spread_medium=0.2, min_spread_high=0.3)
    narrow = config_with(min_spread_low=2.0, min_spread_medium=4.0, min_spread_high=5.0)

    hot = _matrix(render_dashboard(report, config=wide))
    cool = _matrix(render_dashboard(report, config=narrow))

    assert hot.count("heat-p3") + hot.count("heat-n3") > 0
    assert cool.count("heat-p3") + cool.count("heat-n3") == 0
    assert hot.count("heat-p3") != cool.count("heat-p3")


def _matrix(page: str) -> str:
    """The matrix table's own markup.

    Counted apart from the whole page because the stylesheet defines every heat
    class by name, so a count over the document finds each one whether or not a
    single cell carries it.
    """
    table = re.search(r'<table class="matrix">(.*?)</table>', page, flags=re.S)
    assert table is not None
    return table.group(1)


def test_a_spread_inside_the_no_view_band_carries_no_heat_class(
    report: BiasReport,
) -> None:
    """Neutral grey, so a near-zero cell reads as no view.

    A faint tint at the midpoint is the failure this avoids: it says the engine
    leaned slightly, when what happened is that it did not lean at all.
    """
    flat = replace(
        report,
        pairs=tuple(
            replace(
                row,
                spread=0.0,
                direction=Direction.NEUTRAL,
                conviction=Conviction.NONE,
            )
            for row in report.pairs
        ),
    )

    cells = _matrix(render_dashboard(flat))

    assert "heat-p" not in cells
    assert "heat-n" not in cells


# --- the strip is positioned in Python ---------------------------------------


def test_the_calendar_strip_places_events_inside_it(
    report: BiasReport, page: str
) -> None:
    """Percentages, clamped, so an event outside the horizon pins to the edge
    rather than being drawn where nobody can see it and read as no event."""
    positions = [float(value) for value in re.findall(r"left: ([\d.]+)%", page)]

    assert positions
    assert all(0.0 <= value <= 100.0 for value in positions)


def test_an_event_past_the_horizon_is_pinned_to_the_edge(report: BiasReport) -> None:
    """Rather than drawn off the strip, where it would vanish silently."""
    far = replace(
        report,
        events=tuple(
            replace(event, scheduled_for=datetime(2026, 7, 9, 12, 0, tzinfo=UTC))
            for event in report.events
        ),
    )

    rendered = render_dashboard(far)
    positions = [float(value) for value in re.findall(r"left: ([\d.]+)%", rendered)]

    assert positions
    assert max(positions) == pytest.approx(100.0)


# --- the gaps a mutation sweep found -----------------------------------------


def test_the_bars_are_drawn_against_the_widest_score_in_the_run(
    report: BiasReport, page: str
) -> None:
    """Relative, so a quiet day is legible rather than flat.

    An absolute scale would draw every bar near the centre on a day where no
    currency reached the end of the band, and the ranking would read as a
    universe with no differences in it. The widest score fills its half of the
    track and the rest are read against it.
    """
    scored = [row for row in report.currencies if row.coverage > 0]
    widest = max(scored, key=lambda row: abs(row.composite))
    widths = {
        row.currency: float(
            re.search(r"width: ([\d.]+)%", _ranking_row(page, row.currency)).group(1)
        )
        for row in scored
    }

    assert widths[widest.currency] == pytest.approx(50.0)
    for row in scored:
        expected = 50.0 * abs(row.composite) / abs(widest.composite)
        assert widths[row.currency] == pytest.approx(expected, abs=0.01), row.currency


def test_a_run_where_every_score_is_zero_draws_no_bar_rather_than_raising(
    report: BiasReport,
) -> None:
    """The scale has no width, so there is nothing to draw against.

    Dividing by it would raise in the middle of rendering, and a default width
    would draw eight identical bars for a run that found nothing.
    """
    flat = replace(
        report,
        currencies=tuple(replace(row, composite=0.0) for row in report.currencies),
    )

    rendered = render_dashboard(flat)
    widths = [
        float(match.group(1))
        for row in flat.currencies
        if row.coverage > 0
        for match in [
            re.search(r"width: ([\d.]+)%", _ranking_row(rendered, row.currency))
        ]
        if match is not None
    ]

    assert check_constraints(rendered) == []
    assert widths
    assert all(width == 0.0 for width in widths)


def test_the_heat_class_carries_the_sign_of_the_spread(report: BiasReport) -> None:
    """Positive means the row currency is stronger, and the colour says which.

    A scale that dropped the sign would paint both halves of the matrix the
    same, and every mirrored cell would agree with its twin while saying the
    opposite thing.
    """
    cells = _matrix(render_dashboard(report))
    positive = [row for row in report.pairs if row.spread > 0]
    negative = [row for row in report.pairs if row.spread < 0]
    assert positive and negative

    assert "heat-p" in cells
    assert "heat-n" in cells


def test_a_window_that_closes_before_it_opens_takes_no_width(
    report: BiasReport,
) -> None:
    """Clamped at zero rather than drawn backwards.

    A negative width is not a shorter band, it is a band the browser refuses to
    draw at all, so the blackout would vanish from the strip and the strip
    would say the window is clear.
    """
    view = _view_of(report)

    assert view.span_pct(view.horizon, view.origin) == 0.0
    assert view.span_pct(view.origin, view.horizon) == pytest.approx(100.0)


def _view_of(report: BiasReport):
    """The view `render_dashboard` builds for a run, for the unit assertions."""
    from fbe.dashboard.build import _view

    return _view(report, ScoringConfig(), DataConfig())


def test_text_from_a_feed_is_escaped_rather_than_rendered(
    report: BiasReport,
) -> None:
    """Every string on this page came from a feed, and this one is HTML.

    A publisher's headline carrying an angle bracket, rendered raw, swallows
    the rest of the panel silently: the page still loads and the section is
    simply not there.
    """
    loud = replace(
        report,
        warnings=("GBP <b>coverage</b> fell to 60%",),
    )

    rendered = render_dashboard(loud)

    assert "GBP &lt;b&gt;coverage&lt;/b&gt; fell to 60%" in rendered
    assert "<b>coverage</b>" not in rendered


def test_render_dashboard_refuses_a_template_reading_a_name_it_is_not_given(
    report: BiasReport, tmp_path: Path
) -> None:
    """``StrictUndefined`` on the environment `render_dashboard` builds itself.

    Asserted through the public function rather than through an environment the
    test constructs, because it is `render_dashboard`'s own setting that decides
    whether a renamed context key empties a panel or stops the render.
    """
    (tmp_path / TEMPLATE_NAME).write_text(
        "<!doctype html><title>x</title>{{ view.not_a_name }}", encoding="utf-8"
    )

    with pytest.raises(UndefinedError):
        render_dashboard(report, template_dir=tmp_path)


def test_the_title_names_the_run_it_belongs_to(report: BiasReport, page: str) -> None:
    """A tab called "dashboard" is a tab nobody can tell from yesterday's."""
    title = re.search(r"<title>(.*?)</title>", page, flags=re.S).group(1)

    assert f"{report.asof:%d %b %Y}" in title
    assert "G10" in title
