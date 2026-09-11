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

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fbe.config import Config
    from fbe.report import ReportDiff
    from fbe.types import BiasReport

__all__ = [
    "TEMPLATE_NAME",
    "MAX_RENDERED_BYTES",
    "ALLOWED_SCRIPT_HOSTS",
    "ALLOWED_STYLE_HOSTS",
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
        config: Optional effective config, used for the weights panel.
        template_dir: Override for the template search path. Defaults to
            ``fbe/dashboard/templates``.

    Returns:
        The complete HTML document, with every asset inlined.

    Raises:
        NotImplementedError: Always, until rendering lands.

    """
    raise NotImplementedError(
        "fbe.dashboard.build.render_dashboard is scaffolded; "
        "see docs/roadmap.md Phase 5"
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
        One message per violation. Empty means the page is publishable.

    Raises:
        NotImplementedError: Always, until the checks land.

    """
    raise NotImplementedError(
        "fbe.dashboard.build.check_constraints is scaffolded; "
        "see docs/roadmap.md Phase 5"
    )
