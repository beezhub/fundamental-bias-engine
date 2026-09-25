"""Build the single-file HTML dashboard.

The dashboard is the same run as the Markdown report, laid out for a phone. It
exists because most of the trading day happens away from the terminal: the
pre-market work produces a bias, and four hours later the only question is what
the bias was and whether the window is clear. That has to survive being opened
on a phone with one bar of signal.

Publishing constraints
----------------------
The output is intended to be published as a hosted artifact page, and that
sandbox enforces the following. They are not style preferences: a violation
fails silently at view time, leaving a blank panel rather than an error.

* One file. No external assets except scripts from ``cdnjs.cloudflare.com`` or
  ``cdn.jsdelivr.net/npm/``, and stylesheets from ``fonts.googleapis.com`` with
  the font files they pull from ``fonts.gstatic.com``. Everything else is
  blocked: external images, stylesheets from any other host, ``fetch``, ``XHR``
  and WebSocket. Inline all CSS and JS; embed any image as a ``data:`` URI.
  Since the whole page is generated from one report, prefer no external script
  at all and draw the charts as inline SVG.
* Any web font needs a real fallback stack, because the font request can fail
  while the page still renders.
* Theme-aware in three states. Define the full light palette as CSS custom
  properties on bare ``:root``. Redefine only the tokens that change inside
  ``@media (prefers-color-scheme: dark)``, guarded as
  ``:root:not([data-theme="light"])``. Redefine them again under
  ``:root[data-theme="dark"]`` so an explicit toggle wins in both directions.
  Never give a colour its only definition inside a media or ``[data-theme]``
  block. Give ``body`` an explicit background token: the host paints its own
  ground behind a transparent body, and a transparent body inherits the wrong
  theme.
* Responsive to 400px with at least a 16px side gutter and no horizontal page
  scroll. Set the gutter once as side padding on one wrapper and give that
  wrapper vertical padding with ``padding-block``, never a ``padding``
  shorthand that zeroes the sides. The pair matrix is wider than a phone, so it
  goes in its own ``overflow-x: auto`` container and scrolls on its own.
* Under 16MB rendered, ``data:`` URIs included. A G10 report is a few tens of
  kilobytes of numbers, so the only way to approach the limit is by embedding
  raster images. Do not embed raster images.

Layout
------
Single column, top to bottom in the order the trading day needs it:

1. Header: as-of date, generation time, config digest, and a theme toggle that
   stamps ``data-theme`` on the root.
2. Currency ranking as a horizontal bar ranking, strongest at the top, bars
   growing left and right from a centre zero line. Diverging by sign, not
   sequential: the sign is the whole message.
3. The 28-pair bias matrix as a heatmap, base down the rows and quote across
   the columns, cell colour by spread. Diverging scale centred on zero with a
   neutral grey midpoint, so a near-zero cell reads as "no view" rather than as
   a weak signal. Cells arrive from `fbe.report._grid` already oriented for the
   row they sit in, mirrored halves included, so ``view.heat`` colours the
   spread exactly as given. Re-deriving or negating a mirrored cell here would
   invert the lower triangle twice over, and every number on screen would still
   look plausible.
4. Shortlist as cards, one per `fbe.types.TradeIdea`: pair, direction,
   conviction, the reasoning, the size if one is attached, and the blackout if
   there is one. Cards rather than a table because this is the part that gets
   read on a phone at arm's length.
5. Calendar strip: a horizontal time axis for the next 24 hours with the
   blackout windows shaded and each event marked, so "is the window clear" is
   answered by looking rather than by reading.
6. Warnings and coverage, then the run-to-run diff, in the footer. Both matter
   and neither should be the first thing on the screen.

Colour and layout choices follow the repository's data visualisation
conventions when this is implemented: a diverging pair with a neutral grey
midpoint for signed values, never a rainbow and never a hue at the midpoint;
categorical hues assigned in fixed order and never cycled; text in text tokens
rather than in a series colour; dark mode chosen against the dark surface
rather than flipped automatically; and a legend plus a table view so no value
is carried by colour alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from fbe.bias import UNCHECKED_SUFFIX, UNKNOWN_SUFFIX
from fbe.calendar_guard import blackout_windows, is_high_impact
from fbe.config import Config, DataConfig, ScoringConfig
from fbe.report import build_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fbe.report import ReportDiff
    from fbe.types import BiasReport, CalendarEvent, PairBias

__all__ = [
    "TEMPLATE_NAME",
    "MAX_RENDERED_BYTES",
    "ALLOWED_SCRIPT_HOSTS",
    "ALLOWED_STYLE_HOSTS",
    "STRIP_HOURS",
    "render_dashboard",
    "build_dashboard",
    "check_constraints",
]

TEMPLATE_NAME = "dashboard.html.j2"
"""Template file inside ``fbe/dashboard/templates``."""

MAX_RENDERED_BYTES = 16 * 1024 * 1024
"""Hard ceiling the sandbox enforces on the rendered page."""

ALLOWED_SCRIPT_HOSTS = (
    "cdnjs.cloudflare.com",
    "cdn.jsdelivr.net",
)
"""The only hosts a ``<script src>`` may point at. Everything else is blocked
without a visible error, which is worse than being blocked loudly."""

ALLOWED_STYLE_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
)
"""The only hosts a stylesheet or font file may come from."""

STRIP_HOURS = 24
"""Hours the calendar strip spans, starting at the run's generation time.

Local to this view and not a threshold: it is how much of the day fits on a
phone at arm's length, and `fbe.calendar_guard` decides what is actually in a
blackout. Changing it changes the picture and no decision.
"""

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
"""Where `TEMPLATE_NAME` lives, resolved from this module rather than from the
working directory, so an installed package finds its own template."""

_STRIP_MARK_HOURS = 6
"""Spacing of the hour labels along the strip. Four labels over 24 hours is
what fits at 400px without the text colliding."""


@dataclass(frozen=True, slots=True)
class _Window:
    """One blackout window, positioned and labelled for the strip.

    Attributes:
        start: Window opening, aware UTC, from `fbe.calendar_guard`.
        end: Window closing, aware UTC.
        label: What the reader sees on hover: the currencies whose releases
            hold it open, and the window's own clock times.

    A plain ``(start, end)`` tuple is what the guard returns. The label is added
    here because a shaded band with no title is a band the reader cannot
    account for, and guessing at it from the events underneath is arithmetic
    the template would have to do.

    """

    start: datetime
    end: datetime
    label: str


@dataclass(frozen=True, slots=True)
class _CalendarNote:
    """What the run can say about the calendar the strip is drawn from.

    Attributes:
        unchecked: Pairs carrying an ``event:unchecked`` marker, meaning no
            calendar was consulted for them at all.
        unknown: Pairs carrying an ``event:unknown`` marker, meaning a guard
            ran and could not answer: a failed fetch, or a cached week that
            does not reach the date.
        reasons: The distinct reasons behind the unknown markers, in the order
            first seen. Distinct because one failed fetch is one reason on 28
            pairs, and printed 28 times it fills the panel and stops being
            read.
        total: Pairs in the run, so a count is read against something.

    Both counts zero is the only state in which the strip stands on its own. An
    empty strip otherwise is a check that did not run rather than a clear 24
    hours, and those are opposite facts: `docs/decisions/0002-representing-not-known.md`
    rule 3 for why an unrendered marker does not exist, and rule 4 for why the
    two states are counted apart rather than together. The dashboard reads them
    off `fbe.types.PairBias.blockers` because a `fbe.types.BiasReport` carries
    the run's pairs and not the `fbe.calendar_guard.CalendarCoverage` behind
    them.

    """

    unchecked: int
    unknown: int
    reasons: tuple[str, ...]
    total: int


@dataclass(frozen=True, slots=True)
class _View:
    """Everything the template needs that is arithmetic rather than a field.

    Attributes:
        title: The document title, which is the page's only ``<title>``.
        widest: Largest absolute composite in the run, the scale the ranking
            bars are drawn against. Zero when every currency scored zero, which
            `bar_pct` reads as "draw nothing" rather than dividing by it.
        bands: Spread magnitudes at which the heatmap changes step, ascending.
        legend: Ordered ``(css class, label)`` pairs for the heatmap key.
        origin: Left edge of the calendar strip, aware UTC.
        horizon: Right edge, `STRIP_HOURS` later.
        blackouts: Windows to shade, already merged by the guard.
        hour_marks: ``(instant, label)`` pairs for the strip's hour ticks.
        calendar: What the run can say about the calendar behind the strip,
            which is not the same question as what the strip draws.

    Every method here returns a finished value. The template does no
    arithmetic, because arithmetic in a template cannot be unit tested and a
    bar drawn at the wrong width still looks like a bar.

    """

    title: str
    widest: float
    bands: tuple[float, float, float]
    legend: tuple[tuple[str, str], ...]
    origin: datetime
    horizon: datetime
    blackouts: tuple[_Window, ...]
    hour_marks: tuple[tuple[datetime, str], ...]
    calendar: _CalendarNote

    def bar_pct(self, value: float) -> float:
        """Half-width of a ranking bar, as a percentage of the track.

        Args:
            value: A currency composite on the ``-3..+3`` band.

        Returns:
            A percentage in ``[0, 50]``, because the bar grows from the centre
            line and the track's own half is 50% of it. The widest composite in
            the run fills its half exactly, so the bars are read against each
            other rather than against an absolute scale that would make a quiet
            day look like a flat one.

            ``0.0`` when the run's widest composite is zero. Dividing by it
            would raise, and a bar of some default width would say the currency
            scored something.

        """
        if self.widest <= 0.0:
            return 0.0
        return round(50.0 * abs(value) / self.widest, 2)

    def heat(self, spread: float) -> str:
        """CSS class for a heatmap cell, by the size of its spread.

        Args:
            spread: ``composite(base) - composite(quote)`` for the cell, signed
                as the grid handed it over. Positive means the row currency is
                the stronger one.

        Returns:
            ``"heat-p1"`` to ``"heat-p3"`` for a positive spread, ``"heat-n1"``
            to ``"heat-n3"`` for a negative one, and ``""`` for a spread inside
            the band where the engine grades a pair `fbe.types.Conviction.NONE`.
            The empty class leaves the cell on the neutral grey, so a near-zero
            cell reads as no view rather than as a weak signal, which is the
            whole reason the scale is diverging rather than sequential.

        The steps are the spread bands from `fbe.config.ScoringConfig`, the
        same ones `fbe.bias.conviction_for` grades on, so the colour is the
        conviction's **ceiling** rather than its equal. It is read from the
        spread alone, while the conviction beside it is that tier after the
        agreement, coverage, dispersion and calendar caps have been applied,
        and every one of those only demotes. So a cell coloured for the high
        band can be labelled medium, and that is the engine working; a cell
        coloured for the low band and labelled high is impossible and means
        one of the two was produced against different thresholds.

        The palette defines a fourth step either side, ``heat-p4`` and
        ``heat-n4``, and nothing assigns it: a fourth step needs a fourth
        threshold, the config carries three, and a number invented here to
        fill the gap is a number nothing defends.

        """
        low, medium, high = self.bands
        width = abs(spread)
        if width < low:
            return ""
        side = "p" if spread > 0 else "n"
        if width < medium:
            return f"heat-{side}1"
        if width < high:
            return f"heat-{side}2"
        return f"heat-{side}3"

    def pct(self, fraction: float, places: int = 0) -> str:
        """Render a fraction as a percentage.

        Args:
            fraction: A value in ``[0, 1]``, such as a coverage share, the
                fraction of pillar weight that agrees, or a risk fraction.
            places: Decimal places. Zero for a share of weight, where a tenth
                of a percent means nothing and the extra digits cost a phone
                screen its legibility. Two for the realised risk fraction,
                where the plan's cap is 1-2% and a figure rounded to the
                nearest percent cannot show a breach of it. One for the
                intended fraction beside it, which is a figure the ladder
                chose rather than one the account is carrying, so its second
                decimal would only compete for attention with the realised
                one it is printed to be compared against.

        Returns:
            The figure with its sign of measurement attached, for example
            ``"60%"`` or ``"1.42%"``.

        Here rather than in the template because ``value * 100`` written into
        markup is arithmetic no test can reach, and a coverage figure printed
        one point high overstates how much data the call underneath it rests
        on. The precision stays at the call site, because it is a statement
        about what the number means rather than a calculation.

        """
        return f"{fraction * 100:.{places}f}%"

    def at_pct(self, when: datetime) -> float:
        """Where an instant sits along the calendar strip.

        Args:
            when: An aware instant. Naive input would compare against an aware
                origin and raise, which is the right failure: a naive timestamp
                on this strip is a timestamp from an unknown zone.

        Returns:
            A percentage in ``[0, 100]``, clamped at both ends. An event before
            the origin or past the horizon is pinned to the edge rather than
            drawn off the strip, where it would be invisible and read as no
            event at all.

        """
        span = (self.horizon - self.origin).total_seconds()
        if span <= 0.0:
            return 0.0
        offset = (when - self.origin).total_seconds()
        return round(min(100.0, max(0.0, 100.0 * offset / span)), 3)

    def span_pct(self, start: datetime, end: datetime) -> float:
        """Width of a window on the strip, in the same percentage units.

        Args:
            start: Window opening, aware.
            end: Window closing, aware.

        Returns:
            The clamped distance between the two positions, so a window that
            runs past the horizon is drawn to the edge and not beyond it.
            Never negative.

        """
        return round(max(0.0, self.at_pct(end) - self.at_pct(start)), 3)


def _view(
    report: BiasReport,
    scoring: ScoringConfig,
    data: DataConfig,
) -> _View:
    """Derive the geometry and the colour steps for one run.

    Args:
        report: The run being rendered.
        scoring: Supplies the conviction bands the heat steps follow.
        data: Supplies the blackout minutes `fbe.calendar_guard` applies.

    Returns:
        The `_View` the template reads. The windows come from
        `calendar_guard.blackout_windows` rather than being derived here: which
        releases close the market and how wide a window is are that module's
        rules, and a second copy of either would drift from the one the bias
        layer actually consults.

    """
    origin = report.generated_at
    horizon = origin + timedelta(hours=STRIP_HOURS)
    widest = max((abs(row.composite) for row in report.currencies), default=0.0)
    return _View(
        title=f"G10 fundamental bias, {report.asof:%d %b %Y}",
        widest=widest,
        bands=(
            scoring.min_spread_low,
            scoring.min_spread_medium,
            scoring.min_spread_high,
        ),
        legend=(
            ("heat-n3", "quote stronger by more than the high band"),
            ("heat-n2", "quote stronger, medium band"),
            ("heat-n1", "quote stronger, low band"),
            ("", "inside the band the engine calls no view"),
            ("heat-p1", "base stronger, low band"),
            ("heat-p2", "base stronger, medium band"),
            ("heat-p3", "base stronger by more than the high band"),
        ),
        origin=origin,
        horizon=horizon,
        blackouts=_windows(report.events, data),
        hour_marks=_hour_marks(origin, horizon),
        calendar=_calendar_note(report.pairs),
    )


def _calendar_note(pairs: Sequence[PairBias]) -> _CalendarNote:
    """Read what the run's pairs say about the calendar behind them.

    Args:
        pairs: The run's pairs in market convention. Counting the grid instead
            would count every pair twice and report 56 of 56 for a run of 28.

    Returns:
        The `_CalendarNote`. A pair carrying several markers of one kind counts
        once for it: the figure answers how many pairs are affected, and a pair
        with two calendar reasons is still one pair.

        The reasons come from the ``event:unknown`` markers, whose format is
        ``"event:unknown: <reason>"``, and are deduplicated in first-seen
        order. One failed fetch is one reason however many pairs it touched.
        An unknown marker carrying no reason contributes to ``unknown`` and
        nothing to ``reasons``, and the page says the reason was not recorded
        rather than printing an empty one: a caveat with a blank reason reads
        as a caveat whose reason the reader missed.

        A pair carrying markers of both kinds counts in both figures. Each is
        "pairs carrying at least one marker of this kind", and the two states
        are different questions rather than two halves of one.

    Nothing here decides whether a window exists. It decides whether an empty
    strip is an answer, which is the question `fbe.calendar_guard.coverage_gap`
    answers for a live run and which a rendered report can only answer from the
    markers its pairs carry.

    """
    unknown_prefix = "event" + UNKNOWN_SUFFIX
    unchecked_marker = "event" + UNCHECKED_SUFFIX
    unchecked = 0
    unknown = 0
    reasons: list[str] = []
    for row in pairs:
        blind = [marker for marker in row.blockers if marker.startswith(unknown_prefix)]
        if blind:
            unknown += 1
            for marker in blind:
                reason = marker[len(unknown_prefix) :].lstrip(": ").strip()
                if reason and reason not in reasons:
                    reasons.append(reason)
        # Prefixed rather than equal. `apply_filters` emits this kind as the
        # key itself, so equality is correct today, and a variant carrying a
        # reason would then be counted as nothing at all: an undercount is the
        # quiet failure, and a marker nobody rendered does not exist.
        if any(marker.startswith(unchecked_marker) for marker in row.blockers):
            unchecked += 1
    return _CalendarNote(
        unchecked=unchecked,
        unknown=unknown,
        reasons=tuple(reasons),
        total=len(pairs),
    )


def _windows(events: Sequence[CalendarEvent], data: DataConfig) -> tuple[_Window, ...]:
    """Label the guard's merged blackout windows for the strip.

    Args:
        events: The run's calendar events, as fetched.
        data: Config supplying the blackout minutes.

    Returns:
        One `_Window` per merged window, in the guard's order. The label names
        the currencies whose high-impact releases fall inside it, so a reader
        hovering a shaded band learns which leg it applies to. A window with no
        such release cannot arise, since the guard builds windows only from
        them, and the label then says so rather than being empty.

    """
    windows = []
    for start, end in blackout_windows(events, data):
        currencies = sorted(
            {
                event.currency
                for event in events
                if is_high_impact(event) and start <= event.scheduled_for <= end
            }
        )
        named = ", ".join(currencies) if currencies else "no release in range"
        windows.append(
            _Window(
                start=start,
                end=end,
                label=f"{named}: {start:%H:%M} to {end:%H:%M} {start:%Z}",
            )
        )
    return tuple(windows)


def _hour_marks(
    origin: datetime, horizon: datetime
) -> tuple[tuple[datetime, str], ...]:
    """Hour labels along the strip, from the origin to the horizon.

    Args:
        origin: Left edge of the strip.
        horizon: Right edge.

    Returns:
        ``(instant, label)`` pairs every `_STRIP_MARK_HOURS`, the first on the
        next whole hour after the origin so the labels sit on round times
        rather than on whatever minute the run happened to start. The horizon
        itself carries no label: it would sit on the edge and be clipped.

    """
    first = (origin + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    marks = []
    cursor = first
    while cursor < horizon:
        marks.append((cursor, f"{cursor:%H:%M %Z}"))
        cursor += timedelta(hours=_STRIP_MARK_HOURS)
    return tuple(marks)


def render_dashboard(
    report: BiasReport,
    *,
    diff: ReportDiff | None = None,
    config: Config | None = None,
    template_dir: Path | None = None,
) -> str:
    """Render the dashboard to a single HTML string.

    Shares `fbe.report.build_context` with the Markdown report, so the two views
    show the same numbers in the same order. Anything the template needs that
    the Markdown report does not, the SVG geometry for the bars, the heatmap
    cell colours and the calendar strip positions, is derived here rather than
    computed in the template, because arithmetic inside a template is arithmetic
    nobody can test.

    Args:
        report: The run to render.
        diff: Optional diff against the previous run.
        config: Optional effective config. This page reads the scoring section
            for the heatmap's steps and the data section for the blackout
            minutes, and renders no weights panel: `fbe.report.build_context`
            supplies ``pillar_order`` for the Markdown report's weights table
            and this template does not read it.
        template_dir: Override for the template search path. Defaults to
            ``fbe/dashboard/templates``.

    Returns:
        The complete HTML document, with every asset inlined. Run it through
        `check_constraints` before writing it anywhere: the constraints it
        checks fail silently at view time.

    Raises:
        jinja2.UndefinedError: When the template reads a name the context does
            not carry. ``StrictUndefined`` is deliberate, as it is in
            `fbe.report.render_report`: Jinja's default renders an unknown name
            as an empty string, so a renamed key would empty a panel and the
            page would still look like a dashboard.
        jinja2.TemplateNotFound: When ``template_dir`` holds no
            `TEMPLATE_NAME`.
        ValueError: From `fbe.report.build_context`, when a pair has a leg
            outside `fbe.universe.G10` or the run holds one pair twice.

    Autoescaping is on, which is the one place this renderer differs from the
    Markdown one. Every string on this page came from a feed: an event title,
    a blocker, a size warning. Rendered raw into HTML, a stray angle bracket in
    a publisher's headline silently swallows the rest of a panel.

    ``config`` is optional because a report can be rendered without the config
    that produced it, from the sidecar alone. When it is absent the shipped
    `fbe.config.ScoringConfig` and `fbe.config.DataConfig` defaults are used
    for the heat steps and the blackout minutes. That is a fallback to the
    contract rather than to an invented number, and it is worth knowing about:
    a run whose config moved those bands, rendered without that config, colours
    its cells on the shipped bands while the convictions beside them came from
    the moved ones.

    """
    scoring = config.scoring if config is not None else ScoringConfig()
    data = config.data if config is not None else DataConfig()
    directory = template_dir if template_dir is not None else _TEMPLATE_DIR
    environment = Environment(
        loader=FileSystemLoader(directory),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=True,
    )
    context = build_context(report, diff=diff, config=config)
    return environment.get_template(TEMPLATE_NAME).render(
        **context, view=_view(report, scoring, data)
    )


def build_dashboard(
    report: BiasReport,
    out_path: Path,
    *,
    diff: ReportDiff | None = None,
    config: Config | None = None,
) -> Path:
    """Render the dashboard and write it as one self-contained file.

    Renders, then runs `check_constraints` on the result and refuses to write a
    page that violates a publishing constraint. Failing here is cheap; finding
    out from a blank panel on a phone during a session is not.

    Args:
        report: The run to render.
        out_path: File to write. Parent directories are created if missing.
        diff: Optional diff against the previous run.
        config: Optional effective config.

    Returns:
        The path written, for the caller to print or open.

    Raises:
        NotImplementedError: Always, until rendering lands.
        ValueError: Once implemented, when `check_constraints` reports a
            violation.

    """
    raise NotImplementedError(
        "fbe.dashboard.build.build_dashboard is scaffolded; see docs/roadmap.md Phase 5"
    )


def check_constraints(html: str) -> list[str]:
    """Check rendered HTML against the publishing constraints.

    Checks performed:
        * Rendered size against `MAX_RENDERED_BYTES`.
        * Every ``src`` and ``href`` is inline, a ``data:`` URI, a fragment, or
          on an allowed host.
        * No ``fetch``, ``XMLHttpRequest`` or ``WebSocket`` in inline script.
        * A bare ``:root`` block defines the palette tokens, and the dark
          overrides appear under both ``prefers-color-scheme`` and
          ``[data-theme="dark"]``.
        * ``body`` sets an explicit background token.
        * A ``<title>`` is present.

    Args:
        html: The rendered document.

    Returns:
        One message per violation, in a list rather than a generator: a caller
        counts them and then prints them, and an exhausted generator reports
        nothing the second time. Empty means the page is publishable.

    Known limits. The first group is reported loudly, so the failure is a page
    that will not write rather than one that renders blank:

        * Hosts are compared exactly, so a differing case or an explicit
          ``:443`` is reported. Neither comes out of a generator.
        * A comment inside script is still script, so a line saying the page
          fetches nothing is reported.
        * A ``}`` inside a CSS comment ends a block early, and an ``@media``
          inside one truncates the stylesheet from that point, so a rule
          carrying either reads as empty.

    The second group is not detected, which is the expensive direction, and is
    listed so that a page passing this check is not read as more than it is:

        * ``srcset`` is not walked. ``image-set()`` is caught only where it
          wraps a ``url()``, which is how a stylesheet normally writes it.
        * A background set through a ``style=`` attribute rather than in a
          stylesheet is not seen.
        * ``http:`` on an allowed host is accepted here and blocked by the
          browser as mixed content.
        * The allow-lists are matched on host, so any path on an allowed host
          is accepted. This module's header and ``docs/interfaces.md`` name
          ``cdn.jsdelivr.net/npm/``, with a path, while `ALLOWED_SCRIPT_HOSTS`
          holds a bare host. Host-only is what the constant says and what this
          function implements rather than encoding the rule in a second place;
          the discrepancy is open on issue #251.

    """
    document = _Document.of(html)
    return [
        *_truncation_violations(document),
        *_size_violations(html),
        *_reference_violations(document),
        *_script_violations(document),
        *_theme_violations(document),
        *_title_violations(document),
    ]


_OFF_PAGE_CALLS: tuple[str, ...] = ("fetch", "XMLHttpRequest", "WebSocket")
"""Inline-script names the sandbox blocks, each leaving the page rendering.

Named here rather than inline so the message and the check read the same list,
and so a fourth one is added in one place.
"""

_CSS_URL = re.compile(r"""url\(\s*(?P<quote>['"]?)(?P<target>[^'")]*)(?P=quote)\s*\)""")
"""``url(...)`` inside a style block, with or without quotes.

A font or an image referenced from CSS never appears as an attribute, so a
check that walked only ``src`` and ``href`` would pass every externally hosted
font ever embedded.
"""

_CUSTOM_PROPERTY = re.compile(r"--[\w-]+\s*:")
"""One custom property declaration, which is what makes a block a palette."""


class _Document:
    """What the checks need from one rendered page, gathered in a single pass.

    Attributes:
        references: ``(tag, attribute, value)`` for every ``src`` and ``href``
            in the markup, in document order.
        css: Every inline ``<style>`` body, concatenated.
        scripts: Every inline ``<script>`` body. A ``<script src>`` contributes
            a reference instead, since its body is not in this document.
        has_title: Whether a document ``<title>`` is present at all, which is a
            different fact from whether it carries text. An SVG ``<title>``
            does not count: it names a chart for a screen reader and does
            nothing for the tab, and the page this module describes is drawn
            in inline SVG, so the two are certain to meet.
        title: The text inside it.
        unterminated: The element the document ended inside, if any. A page
            cut off mid-``<script>`` is not a page with no script in it, and
            without this the two are the same value.

    Parsed with `html.parser` rather than by pattern matching. The checks are
    about elements and their attributes, and a regular expression over markup
    reads an attribute inside a comment, misses one spread over two lines, and
    is confident about both.

    """

    def __init__(self) -> None:
        self.references: list[tuple[str, str, str]] = []
        self.css: str = ""
        self.scripts: list[str] = []
        self.has_title: bool = False
        self.title: str = ""
        self.unterminated: str | None = None

    @classmethod
    def of(cls, html: str) -> _Document:
        """Parse one document, tolerating markup a browser would tolerate.

        Closed rather than only fed. ``HTMLParser`` buffers the body of a
        ``<script>`` or ``<style>`` until it sees the closing tag, so a
        document that ends inside one leaves that body unparsed and every
        element after it unseen. The page then reports no violations, which is
        the one answer a guard must never give for a document it could not
        finish reading.
        """
        document = cls()
        collector = _Collector(document)
        collector.feed(html)
        collector.close()
        document.unterminated = collector.capturing
        return document

    def css_urls(self) -> list[tuple[str, str, str]]:
        """``url(...)`` targets in the stylesheet, shaped like a reference."""
        return [
            ("style", "url()", match.group("target"))
            for match in _CSS_URL.finditer(self.css)
        ]


class _Collector(HTMLParser):
    """Fill a `_Document` from the markup.

    Args:
        document: The document to fill. Mutated rather than returned, because
            `HTMLParser` drives the traversal and has nowhere to put a result.

    """

    def __init__(self, document: _Document) -> None:
        super().__init__(convert_charrefs=True)
        self._document = document
        self._capturing: str | None = None
        self._svg_depth = 0

    @property
    def capturing(self) -> str | None:
        """The element still open when the parse ended, or ``None``."""
        return self._capturing

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value for name, value in attrs if value is not None}
        for attribute in ("src", "href"):
            if attribute in values:
                self._document.references.append((tag, attribute, values[attribute]))
        # An event handler is inline script written in an attribute, and the
        # page carries a theme toggle, so this is where its script will be.
        self._document.scripts.extend(
            value for name, value in values.items() if name.startswith("on")
        )
        if tag == "svg":
            self._svg_depth += 1
        in_svg = self._svg_depth > 0
        if tag in {"style"} or (tag == "script" and "src" not in values):
            self._capturing = tag
        elif tag == "title" and not in_svg:
            self._capturing = tag
            self._document.has_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg" and self._svg_depth:
            self._svg_depth -= 1
        if tag == self._capturing:
            self._capturing = None

    def handle_data(self, data: str) -> None:
        if self._capturing == "style":
            # Separated, so a selector at the end of one block and a brace at
            # the start of the next cannot read as one rule.
            self._document.css += data + "\n"
        elif self._capturing == "script":
            self._document.scripts.append(data)
        elif self._capturing == "title":
            self._document.title += data


def _truncation_violations(document: _Document) -> list[str]:
    """Refuse a document the parser could not finish reading.

    Args:
        document: The parsed page.

    Returns:
        One message when the markup ended inside an element whose body is
        read here, else none.

    A page cut off mid-``<script>``, because the renderer raised part way
    through the write or the disk filled, has that script and everything after
    it unparsed. Without this the result is an empty list, which is the same
    answer a clean page gets, and
    ``docs/decisions/0002-representing-not-known.md`` rule 4 is precisely that
    a source that failed must be able to say so rather than returning nothing.

    """
    if document.unterminated is None:
        return []
    return [
        f"The document ends inside an unclosed `<{document.unterminated}>`, so "
        "the rest of it was never read. Nothing below that point has been "
        "checked, and a partly written page is refused rather than reported "
        "as clean."
    ]


def _size_violations(html: str) -> list[str]:
    """Measure the page the way the host measures it.

    Args:
        html: The rendered document.

    Returns:
        One message when the page is at or over `MAX_RENDERED_BYTES`, else
        none.

    Measured in UTF-8 bytes rather than in characters, because bytes are what
    the host downloads. A page of currency names and event titles from a feed
    is not pure ASCII, and ``len(html)`` understates it by up to a third.

    The comparison is strict, so a page of exactly `MAX_RENDERED_BYTES` is
    reported. ``docs/interfaces.md`` and this module's own header both say
    "under 16MB", and a ceiling that admits its own value is the off-by-one
    discovered by the page that does not load.

    """
    size = len(html.encode("utf-8"))
    if size < MAX_RENDERED_BYTES:
        return []
    return [
        f"Rendered size is {size} bytes against the {MAX_RENDERED_BYTES} "
        "ceiling the sandbox enforces, measured as UTF-8 because that is what "
        "the host downloads. Embedded raster images are the only way a report "
        "of G10 numbers gets near it."
    ]


def _allowed_hosts(tag: str) -> tuple[str, ...]:
    """Name the hosts this element may point at.

    Args:
        tag: The element carrying the reference, or ``"style"`` for a CSS
            ``url()``.

    Returns:
        The permitted hosts, empty when the element may not reach off the page
        at all.

    Scripts and stylesheets have separate allow-lists because the sandbox
    treats them separately, and everything else, an image above all, has none:
    an external image is blocked and leaves a gap rather than an error.

    """
    if tag == "script":
        return ALLOWED_SCRIPT_HOSTS
    if tag in {"link", "style"}:
        return ALLOWED_STYLE_HOSTS
    return ()


def _reference_violations(document: _Document) -> list[str]:
    """Check every reference the page makes against what the sandbox serves.

    Args:
        document: The parsed page.

    Returns:
        One message per reference that is neither inline, a ``data:`` URI, a
        fragment, nor on a host allowed for that element.

    The rule is applied to every ``src`` and ``href``, which includes an
    ordinary link to another site. That is what the constraint says and it
    errs loudly: a page that cannot be published is a worse outcome than a
    link the guard asks about. ``tests/test_dashboard_constraints.py`` pins
    that case so it reads as a decision rather than as an oversight.

    The host is compared against the allow-list whole. A substring test would
    accept ``cdnjs.cloudflare.com.example``, which is the usual way an
    allow-list is got around, and that page would then fail the quiet way.

    """
    messages: list[str] = []
    for tag, attribute, value in document.references + document.css_urls():
        target = value.strip()
        if target.startswith(("#", "data:")):
            continue
        allowed = _allowed_hosts(tag)
        host = urlsplit(target).netloc
        if host and host in allowed:
            continue
        permitted = ", ".join(allowed) if allowed else "nothing off the page"
        messages.append(
            f"External reference: `<{tag}>` {attribute} points at {target!r}. "
            f"The page is published as one file, so this must be inline, a "
            f"`data:` URI, a fragment, or on one of: {permitted}."
        )
    return messages


def _script_violations(document: _Document) -> list[str]:
    """Check inline script for calls that leave the page.

    Args:
        document: The parsed page.

    Returns:
        One message per blocked call found, in the order listed by
        `_OFF_PAGE_CALLS`.

    Only script bodies are searched, never the whole document. The report's own
    footer says a calendar fetch failed, and a substring search over the page
    reports that sentence as a violation.

    A comment inside script is still script here, so a line saying the page
    does not fetch anything is reported. That is a false rejection and it
    fails loudly, which is the right side to be wrong on for a check whose
    real failures are all silent.

    """
    body = "\n".join(document.scripts)
    return [
        f"Inline script calls `{call}`, which the sandbox blocks. The page "
        "still renders and the panel that needed the data is empty, which "
        "reads as no opinion rather than as a failure."
        for call in _OFF_PAGE_CALLS
        if re.search(rf"\b{call}\b", body)
    ]


def _blocks(css: str, selector: re.Pattern[str]) -> list[str]:
    """Bodies of every rule whose selector matches, braces balanced.

    Args:
        css: The stylesheet.
        selector: Pattern matched against the text preceding a ``{``.

    Returns:
        The text inside each matching block, nested blocks included.

    Written out rather than taken from a CSS parser because the package has no
    third-party dependency here, and because only three questions are asked of
    the stylesheet: does this selector exist, what is inside it, and is it
    nested in an ``@media``.

    """
    bodies: list[str] = []
    for match in selector.finditer(css):
        opening = css.find("{", match.end() - 1)
        if opening == -1:
            continue
        depth = 0
        for index in range(opening, len(css)):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(css[opening + 1 : index])
                    break
    return bodies


def _without_at_rules(css: str) -> str:
    """Strip every ``@media`` and ``@supports`` block from the stylesheet.

    Args:
        css: The stylesheet.

    Returns:
        What is left, which is where a bare selector has to be found.

    A palette whose only definition sits inside a media block leaves the light
    page with no tokens at all, and that page renders unstyled rather than
    erroring. Removing the conditional blocks first is what makes "bare" a
    checkable property rather than a reading of the source order.

    """
    out = css
    for rule in ("@media", "@supports"):
        while True:
            start = out.find(rule)
            if start == -1:
                break
            opening = out.find("{", start)
            if opening == -1:
                out = out[:start]
                break
            depth = 0
            end = len(out)
            for index in range(opening, len(out)):
                if out[index] == "{":
                    depth += 1
                elif out[index] == "}":
                    depth -= 1
                    if depth == 0:
                        end = index + 1
                        break
            out = out[:start] + out[end:]
    return out


_BARE_ROOT = re.compile(r"(?<![\w\]\)]):root\s*\{")
_DARK_MEDIA = re.compile(r"@media[^{]*prefers-color-scheme\s*:\s*dark[^{]*\{")
_DARK_ATTRIBUTE = re.compile(r"""\[data-theme\s*=\s*['"]?dark['"]?\][^{]*\{""")
_BODY_RULE = re.compile(r"(?:^|[,{}\s])body\s*\{")
_BODY_BACKGROUND = re.compile(r"\bbackground(?:-color)?\s*:")


def _theme_violations(document: _Document) -> list[str]:
    """Check the three theme states and the body's ground.

    Args:
        document: The parsed page.

    Returns:
        One message per missing piece: the bare palette, either dark override,
        or the body background.

    Every one of these renders. None of them errors. A palette defined only
    inside a media block gives the light reader an unstyled page; a missing
    override gives them the wrong theme; a transparent body inherits whatever
    the host painted behind it.

    Three of the four are looked for outside the at-rules: the palette, the
    body background and the explicit toggle. A declaration whose only
    definition sits inside a media block is absent for every reader the query
    does not match, and the published contract says no colour may be defined
    that way. The toggle is the case that reads as an exception and is not:
    an override nested inside the preference query cannot win against that
    preference, so a reader on a light system presses the toggle and nothing
    happens, which is the failure its own message describes. Only the
    preference override is looked for inside, because inside is where it
    belongs.

    """
    messages: list[str] = []
    css = document.css
    bare_css = _without_at_rules(css)
    bare = _blocks(bare_css, _BARE_ROOT)
    if not any(_CUSTOM_PROPERTY.search(block) for block in bare):
        messages.append(
            "No palette on a bare `:root`: the light tokens must be defined "
            "outside any media or `[data-theme]` block. A palette defined "
            "only inside one leaves the page with no tokens at all, and it "
            "renders unstyled rather than failing."
        )
    if not any(_CUSTOM_PROPERTY.search(block) for block in _blocks(css, _DARK_MEDIA)):
        messages.append(
            "No dark override under `prefers-color-scheme: dark`: a reader "
            "whose system is dark gets the light palette against the host's "
            "dark ground."
        )
    if not any(
        _CUSTOM_PROPERTY.search(block) for block in _blocks(bare_css, _DARK_ATTRIBUTE)
    ):
        messages.append(
            'No dark override under `[data-theme="dark"]`: the explicit '
            "toggle then does nothing on a phone whose system theme already "
            "matches, which reads as a broken control."
        )
    if not any(
        _BODY_BACKGROUND.search(block) for block in _blocks(bare_css, _BODY_RULE)
    ):
        messages.append(
            "No explicit background on `body`: the host paints its own ground "
            "behind a transparent body, so the page inherits the wrong theme "
            "and the text can land on a surface of the same colour."
        )
    return messages


def _title_violations(document: _Document) -> list[str]:
    """Check the page names itself.

    Args:
        document: The parsed page.

    Returns:
        One message when there is no ``<title>``, or when it is empty.

    An untitled tab on a phone is the one the owner cannot find among the
    others at six in the morning, which is the only moment this page exists
    for.

    """
    if document.has_title and document.title.strip():
        return []
    missing = "is empty" if document.has_title else "is missing"
    return [
        f"The `<title>` {missing}: the page is opened on a phone alongside "
        "other tabs, and an untitled one cannot be found among them."
    ]
