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
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, UndefinedError

from fbe.config import Config, DataConfig, RiskConfig, ScoringConfig
from fbe.dashboard.build import (
    TEMPLATE_NAME,
    _View,
    _view,
    check_constraints,
    render_dashboard,
)
from fbe.report import (
    CurrencyChange,
    ReportDiff,
    build_context,
    load_report,
    render_report,
)
from fbe.types import BiasReport, Conviction, Direction, PairBias

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
    blocker_counts = ()

    def flags(self, cell: PairBias) -> str:
        return " stub-flag"

    def expansion(self, bias: PairBias) -> SimpleNamespace:
        return SimpleNamespace(
            blockers=(), base_coverage=None, quote_coverage=None, rows=()
        )

    def bar_pct(self, value: float) -> float:
        return 37.25

    def pct(self, fraction: float, places: int = 0) -> str:
        return f"19.{places}%"

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
    assert "stub-flag" in rendered
    assert "left: 11.5%" in rendered
    assert "19.0%" in rendered
    assert "19.2%" in rendered


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

    positive_rule = _rule(sheet, ".rank-bar.pos")
    negative_rule = _rule(sheet, ".rank-bar.neg")

    assert "left: 50%" in positive_rule and "right:" not in positive_rule
    assert "right: 50%" in negative_rule and "left:" not in negative_rule
    assert _colour(positive_rule) != _colour(negative_rule)


def _rule(sheet: str, selector: str) -> str:
    """The declarations inside one CSS rule, and only that rule.

    Asserting a selector and a declaration are both somewhere in the
    stylesheet is four independent searches, not one test of which rule
    carries which: it holds with ``left`` and ``right`` swapped between the
    positive and negative bars, which draws the strongest currency's bar
    pointing the way the weakest one should and still looks like a ranking.
    """
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", sheet)
    assert match is not None, f"no rule for {selector}"
    return match.group(1)


def _colour(rule: str) -> str:
    """The background token a rule paints with."""
    match = re.search(r"background:\s*([^;]+);", rule)
    assert match is not None, rule
    return match.group(1).strip()


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
    assert re.search(r"<button[^>]*data-theme-toggle", page) is not None
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
    assert "overflow-x: auto" in _rule(sheet, ".scroll-x")

    # The rule existing is not the matrix using it, and the matrix is the one
    # element wider than a phone. Outside a scrolling container it is the page
    # that scrolls sideways, which is the criterion this is here for.
    container = re.search(r'<div class="scroll-x">(.*?)</div>', page, flags=re.S)
    assert container is not None
    assert '<table class="matrix">' in container.group(1)


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


def test_the_strip_carries_one_band_per_window_and_one_tick_per_event(
    report: BiasReport, page: str
) -> None:
    """The question the page exists to answer four hours after the desk work.

    Counted against the guard's own merged windows rather than against the
    events, because two releases inside one window are one band and a strip
    drawing two would say the market reopens in between.
    """
    strip = _strip(page)
    view = _view_of(report)

    assert len(re.findall(r'class="window"', strip)) == len(view.blackouts)
    assert len(re.findall(r'class="tick"', strip)) == len(report.events)
    assert len(re.findall(r'class="hour"', strip)) == len(view.hour_marks)
    assert 'class="now"' in strip


def test_every_band_and_tick_sits_where_the_view_put_it(
    report: BiasReport, page: str
) -> None:
    """Positions come from Python, and the strip is where they land."""
    strip = _strip(page)
    view = _view_of(report)

    for window in view.blackouts:
        assert f"left: {view.at_pct(window.start)}%" in strip
        assert f"width: {view.span_pct(window.start, window.end)}%" in strip
    for event in report.events:
        assert f"left: {view.at_pct(event.scheduled_for)}%" in strip

    positions = [float(value) for value in re.findall(r"left: ([\d.]+)%", strip)]
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

    strip = _strip(render_dashboard(far))
    positions = [float(value) for value in re.findall(r"left: ([\d.]+)%", strip)]

    assert positions
    assert max(positions) == pytest.approx(100.0)


def test_the_blackout_minutes_come_from_the_data_config(report: BiasReport) -> None:
    """The only config input to the strip, and the one that sets its width.

    A band drawn from the shipped minutes on a desk that widened them says the
    market reopens before it does, which is the same defect as a window built
    from the listed slice rather than the whole week.
    """
    wide = Config(
        risk=RiskConfig(),
        scoring=ScoringConfig(),
        data=DataConfig(
            calendar_blackout_before_min=120, calendar_blackout_after_min=180
        ),
    )

    default = _view_of(report)
    widened = _view(report, ScoringConfig(), wide.data)

    assert widened.blackouts
    assert len(widened.blackouts) == len(default.blackouts)
    for narrow_window, wide_window in zip(
        default.blackouts, widened.blackouts, strict=True
    ):
        assert (
            wide_window.end - wide_window.start
            > narrow_window.end - narrow_window.start
        )

    first = widened.blackouts[0]
    rendered = render_dashboard(report, config=wide)

    assert f"width: {widened.span_pct(first.start, first.end)}%" in _strip(rendered)


def test_a_blackout_band_names_the_currency_holding_it_open(
    report: BiasReport, page: str
) -> None:
    """A shaded band with no title is a band the reader cannot account for."""
    view = _view_of(report)
    assert view.blackouts

    strip = _strip(page)
    for window in view.blackouts:
        assert window.label in strip

    # Asserted against the events rather than against the label the same view
    # produced: comparing the label to itself holds with the currency dropped
    # from it, and the currency is the whole reason the window is labelled.
    holders = {
        event.currency
        for event in report.events
        for window in view.blackouts
        if window.start <= event.scheduled_for <= window.end
    }
    assert holders
    for code in holders:
        assert any(code in window.label for window in view.blackouts), code


def test_a_naive_instant_on_the_strip_raises(report: BiasReport) -> None:
    """Documented behaviour, pinned. A naive timestamp on a 24 hour axis is a
    timestamp from an unknown zone, and placing it would be a guess."""
    view = _view_of(report)

    with pytest.raises(TypeError):
        view.at_pct(datetime(2026, 6, 30, 12, 0))


def test_the_hour_labels_start_on_a_whole_hour_and_stop_before_the_edge(
    report: BiasReport,
) -> None:
    """A label on the horizon sits on the edge and is clipped, and a label on
    the minute the run happened to start reads as a time that means something."""
    view = _view_of(report)

    assert view.hour_marks
    first, _ = view.hour_marks[0]
    assert first.minute == 0 and first.second == 0
    assert first > view.origin
    assert all(instant < view.horizon for instant, _ in view.hour_marks)
    gaps = {
        (later - earlier).total_seconds()
        for (earlier, _), (later, _) in zip(
            view.hour_marks, view.hour_marks[1:], strict=False
        )
    }
    assert gaps == {6 * 3600}


def _strip(page: str) -> str:
    """The calendar strip's own markup.

    Scoped apart from the document because the stylesheet carries its own
    ``left: 50%`` rules for the ranking, so a position search over the whole
    page finds them whether or not the strip rendered at all.
    """
    match = re.search(r'<div class="strip">(.*?)\n    </div>', page, flags=re.S)
    assert match is not None, "the page carries no calendar strip"
    return match.group(1)


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
    cells = _cells(render_dashboard(report))
    assert len(cells) == 56

    for heat, printed in cells:
        if printed.startswith("+"):
            assert heat.startswith("heat-p") or heat == "", printed
        else:
            assert heat.startswith("heat-n") or heat == "", printed

    assert any(heat.startswith("heat-p") for heat, _ in cells)
    assert any(heat.startswith("heat-n") for heat, _ in cells)


def _matrix(page: str) -> str:
    """The matrix table alone.

    Scoped rather than searched over the whole page because the pair detail
    below it prints its pillar scores in ``num`` cells too, and a helper that
    reads those as matrix cells reports 546 cells for a grid of 56.
    """
    match = re.search(r'<table class="matrix">.*?</table>', page, flags=re.S)
    assert match is not None, "the page carries no matrix"
    return match.group(0)


def _heat_of(classes: str) -> str:
    """The heat step out of one cell's class list.

    A cell carries its marks in the same attribute: ``marked`` when the pair
    holds a marker of its own, ``thin`` when a leg is below full coverage. The
    step is the one class naming a band, and a cell inside the no-view band has
    none.
    """
    steps = [name for name in classes.split() if name.startswith("heat-")]
    assert len(steps) <= 1, classes
    return steps[0] if steps else ""


def _cells(page: str) -> list[tuple[str, str]]:
    """Every matrix cell as ``(heat class, printed number)``.

    The diagonal is excluded: a currency has no bias against itself and the
    template renders it as a placeholder rather than as a number.
    """
    found = re.findall(
        r'<td class="num ?([^"]*)"[^>]*>\s*([+-][\d.]+)\s*</td>', _matrix(page)
    )
    return [(_heat_of(classes), printed) for classes, printed in found]


def test_a_mirrored_cell_carries_the_opposite_colour(report: BiasReport) -> None:
    """The lower triangle is the upper one negated, and the colour follows.

    The template's own comment says re-deriving a mirrored cell would invert
    the lower triangle twice over and every number would still look plausible.
    This is the assertion that comment asks for.
    """
    page = render_dashboard(report)
    classes = {
        f"{base}{quote}": _heat_of(found)
        for found, base, quote in re.findall(
            r'<td class="num ?([^"]*)"\s+title="(\w{3}) vs (\w{3})',
            _matrix(page),
            flags=re.S,
        )
    }

    assert len(classes) == 56
    for pair, heat in classes.items():
        mirrored = classes[pair[3:] + pair[:3]]
        if heat == "":
            assert mirrored == ""
        else:
            assert heat[:6] != mirrored[:6], pair
            assert heat[6:] == mirrored[6:], pair


def test_no_cell_is_coloured_below_the_conviction_beside_it(
    report: BiasReport,
) -> None:
    """The colour is the conviction's ceiling, never less.

    `heat` reads the spread alone; the conviction beside it is that same tier
    after the agreement, coverage, dispersion and calendar caps, and every one
    of those only demotes. A cell painted for the low band and labelled high
    means the two were produced against different thresholds, which is the
    config-drift defect rendered as a picture.
    """
    view = _view_of(report)
    ladder = {
        Conviction.NONE: 0,
        Conviction.LOW: 1,
        Conviction.MEDIUM: 2,
        Conviction.HIGH: 3,
    }
    tiers = {"": 0, "1": 1, "2": 2, "3": 3}

    for row in report.pairs:
        heat = view.heat(row.spread)
        assert tiers[heat[-1] if heat else ""] >= ladder[row.conviction], row.pair


def test_the_band_boundaries_are_the_configured_ones(report: BiasReport) -> None:
    """Which band is which, not merely that the bands are read.

    Reading them in the wrong order still produces some cells at every step
    and still responds to a config change, so a counting test cannot tell.
    """
    view = _view_of(report)
    bands = ScoringConfig()

    assert view.heat(bands.min_spread_low - 0.01) == ""
    assert view.heat(bands.min_spread_low) == "heat-p1"
    assert view.heat(bands.min_spread_medium - 0.01) == "heat-p1"
    assert view.heat(bands.min_spread_medium) == "heat-p2"
    assert view.heat(bands.min_spread_high - 0.01) == "heat-p2"
    assert view.heat(bands.min_spread_high) == "heat-p3"
    assert view.heat(-bands.min_spread_high) == "heat-n3"


def test_every_cell_prints_its_number(report: BiasReport) -> None:
    """No value carried by colour alone, on the panel that is all colour."""
    cells = _cells(render_dashboard(report))

    assert len(cells) == 56
    assert all(printed for _, printed in cells)


def test_the_legend_names_both_ends_and_carries_a_swatch_each(page: str) -> None:
    """A key that vanished leaves a grid of colours with no scale."""
    legend = re.search(r'<div class="legend">(.*?)</div>', page, flags=re.S)
    assert legend is not None

    body = legend.group(1)
    assert body.count('class="swatch') == 7
    assert body.index("quote stronger") < body.index("base stronger")


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


def _view_of(report: BiasReport) -> _View:
    """The view `render_dashboard` builds for a run, for the unit assertions."""
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


# --- the gaps the second review found ----------------------------------------


def test_the_composites_match_the_markdown_report_currency_for_currency(
    report: BiasReport,
) -> None:
    """The criterion names the Markdown report, so the comparison is against it.

    Comparing the page to the `BiasReport` it came from checks the page; it
    does not check that the two views agree, which is the thing a shared
    context is for and the thing that goes wrong quietly when one of them
    starts formatting or rounding on its own.
    """
    markdown = dict(
        re.findall(
            r"^\| *\d+ \| ([A-Z]{3}) \| ([+-][\d.]+) \|", render_report(report), re.M
        )
    )
    page = render_dashboard(report)

    assert len(markdown) == len(report.currencies)
    for row in report.currencies:
        if row.coverage <= 0:
            continue
        assert markdown[row.currency] in _ranking_row(page, row.currency), row.currency


def test_every_thin_currency_prints_its_own_coverage(
    report: BiasReport, page: str
) -> None:
    """Its own, not the first one's.

    Checking a single thin row passes on a page that prints one currency's
    coverage against every thin row, and coverage stated higher than it is
    overstates how much data the call underneath it rests on.
    """
    thin = [row for row in report.currencies if 0 < row.coverage < 1]
    assert len(thin) >= 2, "the fixture no longer carries two thin currencies"

    figures = {row.currency: row.coverage for row in thin}

    assert figures == {"GBP": 0.6, "AUD": 0.86}
    assert "60%" in _ranking_row(page, "GBP")
    assert "86%" in _ranking_row(page, "AUD")
    assert "86%" not in _ranking_row(page, "GBP")


def test_the_printed_ranks_read_down_the_page_in_order(
    report: BiasReport, page: str
) -> None:
    """`rank` comes from the scorer and the order comes from `build_context`.

    Two places, so they can drift, and the symptom is a ranking that reads
    1, 2, 4, 3 while every bar is the right length.

    The unscored currency carries no rank, so the printed sequence is the
    scored rows only and it climbs without being contiguous: the rank the
    scorer gave the unscored one is skipped rather than reassigned, because
    renumbering here would put this page's ranks out of step with the report's.
    """
    rows = re.findall(
        r"<strong>([A-Z]{3})</strong>\s*<span class=\"meta num\">(\d+)</span>", page
    )
    scored = [row for row in report.currencies if row.coverage > 0]
    ranks = [int(rank) for _, rank in rows]

    assert len(rows) == len(scored)
    assert [code for code, _ in rows] == [row.currency for row in scored]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_a_currency_the_run_could_not_score_carries_no_rank(
    report: BiasReport, page: str
) -> None:
    """A rank places a currency among the ones that were measured.

    The row already refuses to draw a bar for the same reason. Leaving the
    rank on it says the run placed this currency fifth of eight, which is a
    measurement the run did not make.
    """
    unscored = [row for row in report.currencies if row.coverage <= 0]
    assert unscored

    row = _ranking_row(page, unscored[0].currency)

    assert str(unscored[0].rank) not in row
    assert "not scored" in row


def test_a_run_whose_config_changed_says_the_scores_are_not_comparable(
    report: BiasReport,
) -> None:
    """A re-weighting moves every currency at once.

    Rendered as a delta table it reads as a market move, and this is the one
    run where a reader most needs the page not to imply one.
    """
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
        config_changed=True,
    )

    since = render_dashboard(report, diff=diff).split("<h2>Since the last run</h2>", 1)[
        1
    ]

    assert "not comparable" in since
    assert "<th>Delta</th>" not in since
    assert "+0.64" not in since


def test_the_delta_is_in_the_delta_column(report: BiasReport) -> None:
    """Not merely somewhere in the row. Three numbers, one of which is the move.

    A page printing "then" where it promises the delta is a page saying the
    currency moved by its own previous score.
    """
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
    # Scoped to the diff section: the events table carries its own USD rows,
    # and a search over the document finds one of those first.
    since = rendered.split("<h2>Since the last run</h2>", 1)[1]
    row = re.search(r"<td>USD</td>(.*?)</tr>", since, flags=re.S)
    assert row is not None

    numbers = [
        value.strip()
        for value in re.findall(
            r'<td class="num">\s*([+-][\d.]+)\s*</td>', row.group(1), flags=re.S
        )
    ]

    assert numbers == ["+1.20", "+1.84", "+0.64"]


def test_the_shortlist_card_leads_with_the_realised_risk(report: BiasReport) -> None:
    """What the rounded lot size actually exposes, not what the rule asked for.

    The two differ on almost every trade at this account size, and the
    intended figure printed as the risk is the cap breach that reads as
    compliance. `fbe.types.PositionSize` sets out the same distinction.
    """
    sized = [idea for idea in report.shortlist if idea.size is not None]
    assert sized, "the fixture no longer carries a sized idea"
    size = sized[0].size
    assert size is not None
    assert size.realised_risk_amount != pytest.approx(size.risk_amount)

    card = _card(render_dashboard(report), sized[0].bias.pair)

    assert f"{size.realised_risk_amount:.2f}" in card
    assert card.index(f"{size.realised_risk_amount:.2f}") < card.index(
        f"{size.risk_amount:.2f}"
    )


def test_the_shortlist_card_carries_the_size_warnings(report: BiasReport) -> None:
    """The marker reaching the dataclass and stopping before the reader is the
    defect this surface has its own copy of."""
    sized = [idea for idea in report.shortlist if idea.size is not None]
    size = sized[0].size
    assert size is not None and size.warnings

    card = _card(render_dashboard(report), sized[0].bias.pair)

    for warning in size.warnings:
        assert warning in card


def test_a_card_under_a_blackout_says_to_stand_aside(report: BiasReport) -> None:
    """The one line on the card that says do not take this now."""
    blacked = [idea for idea in report.shortlist if idea.blackout_until is not None]
    assert blacked, "the fixture no longer carries a blacked-out idea"

    card = _card(render_dashboard(report), blacked[0].bias.pair)

    assert "Stand aside until" in card
    assert "tag block" in card


def _card(page: str, pair: str) -> str:
    """The shortlist card for one pair."""
    cards = re.findall(r'<article class="card">(.*?)</article>', page, flags=re.S)
    matching = [card for card in cards if pair in card]
    assert len(matching) == 1, f"{pair}: {len(matching)} cards"
    return matching[0]


def test_the_template_does_no_arithmetic_of_its_own() -> None:
    """The complement to the stub-view test, which can only see ``view.*``.

    A template doing ``score.coverage * 100`` is arithmetic on a context object
    the stub never touches, so the stub cannot observe it. This reads the
    template instead. Jinja's own loop and filter syntax is left alone; what is
    banned is a number or an operator doing work in the markup.
    """
    source = (TEMPLATE_DIR / TEMPLATE_NAME).read_text(encoding="utf-8")
    expressions = re.findall(r"\{\{(.*?)\}\}", source, flags=re.S)

    for expression in expressions:
        assert "*" not in expression, expression
        assert "/" not in expression, expression
        assert " + " not in expression, expression
        assert " - " not in expression, expression


def test_a_cell_tooltip_names_its_legs_in_the_order_the_row_reads(
    report: BiasReport,
) -> None:
    """The only place the matrix says a direction in words.

    Row currency first, because the cell is the row's composite minus the
    column's and the tooltip is what a reader checks their reading against. The
    legs swapped give a tooltip that agrees with the colour and contradicts the
    number, which is the shape `docs/methodology.md` calls the inverted pair.
    """
    page = render_dashboard(report)
    titles = re.findall(r'title="(\w{3}) vs (\w{3}): (\w+) \1, (\w+) conviction"', page)

    assert len(titles) == 56

    by_pair = {row.pair: row for row in report.pairs}
    for base, quote, direction, conviction in titles:
        row = by_pair.get(f"{base}{quote}") or by_pair[f"{quote}{base}"]
        assert direction in {"long", "short", "neutral"}
        if f"{base}{quote}" in by_pair:
            assert direction == row.direction.value
            assert conviction == row.conviction.value


def test_a_risk_fraction_keeps_the_decimals_a_coverage_share_does_not(
    report: BiasReport,
) -> None:
    """The plan's cap is 1-2%, so a risk figure rounded to the nearest percent
    cannot show a breach of it, while a tenth of a percent of pillar weight
    means nothing and costs a phone screen its legibility.

    Asserted on the rendered card as well as on the view, because the card is
    where the two precisions sit side by side and the stub-view test in this
    file cannot see the real one.
    """
    view = _view_of(report)

    assert view.pct(0.0142, 2) == "1.42%"
    assert view.pct(0.0142) == "1%"
    assert view.pct(0.6) == "60%"

    sized = [idea for idea in report.shortlist if idea.size is not None]
    size = sized[0].size
    assert size is not None

    card = _card(render_dashboard(report), sized[0].bias.pair)

    assert f"{size.realised_risk_fraction * 100:.2f}%" in card
    assert f"{size.risk_fraction * 100:.1f}%" in card
