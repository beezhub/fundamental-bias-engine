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
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

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
    document = _Document.of(html)
    return [
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
        has_title: Whether a ``<title>`` element is present at all, which is a
            different fact from whether it carries text.
        title: The text inside it.

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

    @classmethod
    def of(cls, html: str) -> _Document:
        """Parse one document, tolerating markup a browser would tolerate."""
        document = cls()
        _Collector(document).feed(html)
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value for name, value in attrs if value is not None}
        for attribute in ("src", "href"):
            if attribute in values:
                self._document.references.append((tag, attribute, values[attribute]))
        if tag in {"style", "title"} or (tag == "script" and "src" not in values):
            self._capturing = tag
        if tag == "title":
            self._document.has_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == self._capturing:
            self._capturing = None

    def handle_data(self, data: str) -> None:
        if self._capturing == "style":
            self._document.css += data
        elif self._capturing == "script":
            self._document.scripts.append(data)
        elif self._capturing == "title":
            self._document.title += data


def _size_violations(html: str) -> list[str]:
    """Measure the page the way the host measures it.

    Args:
        html: The rendered document.

    Returns:
        One message when the page is over `MAX_RENDERED_BYTES`, else none.

    Measured in UTF-8 bytes rather than in characters, because bytes are what
    the host downloads. A page of currency names and event titles from a feed
    is not pure ASCII, and ``len(html)`` understates it by up to a third.

    """
    size = len(html.encode("utf-8"))
    if size <= MAX_RENDERED_BYTES:
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
    link the guard asks about.

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

    """
    messages: list[str] = []
    css = document.css
    bare = _blocks(_without_at_rules(css), _BARE_ROOT)
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
        _CUSTOM_PROPERTY.search(block) for block in _blocks(css, _DARK_ATTRIBUTE)
    ):
        messages.append(
            'No dark override under `[data-theme="dark"]`: the explicit '
            "toggle then does nothing on a phone whose system theme already "
            "matches, which reads as a broken control."
        )
    if not any(_BODY_BACKGROUND.search(block) for block in _blocks(css, _BODY_RULE)):
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
