"""The publishing guard: what makes a dashboard fail silently once published.

Every constraint here has the same shape of failure, and it is the reason this
function exists rather than a linter. A page that breaks one of these does not
error. It renders, with a blank panel where the chart was, or in the wrong
theme, or with no text at all against a background the host painted. The only
place that shows up is on the owner's phone at six in the morning, four hours
after the run that produced it, which is the one moment the page exists for.

So the guard is written to be loud. It reports every violation rather than the
first, because a caller fixing one at a time round-trips once per constraint,
and each message names the constraint and what it found, so a reader does not
have to open the page to act on it.

What this file deliberately does not do is build its documents with
`render_dashboard`. A checker verified against the output of the thing it is
meant to police agrees with it the day they are both wrong, and this checker
exists precisely because the renderer cannot check itself. Every document here
is written out by hand: the publishable one as a committed fixture, and the
violating ones from a builder that changes one part at a time.

Nothing here reaches the network and nothing loads a browser. The subject is a
string.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fbe.dashboard.build import (
    ALLOWED_SCRIPT_HOSTS,
    ALLOWED_STYLE_HOSTS,
    MAX_RENDERED_BYTES,
    check_constraints,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dashboard_publishable.html"
"""A small document satisfying every constraint, written by hand.

Committed rather than generated so that the day the renderer starts emitting a
violation, this file still says what a publishable page looks like.
"""

PALETTE = """
      :root {
        --surface: #ffffff;
        --ink: #14171a;
      }
"""

DARK_MEDIA = """
      @media (prefers-color-scheme: dark) {
        :root:not([data-theme="light"]) {
          --surface: #0f1215;
          --ink: #e9edf1;
        }
      }
"""

DARK_ATTRIBUTE = """
      :root[data-theme="dark"] {
        --surface: #0f1215;
        --ink: #e9edf1;
      }
"""

BODY = """
      body {
        background: var(--surface);
        color: var(--ink);
      }
"""


def document(
    *,
    title: str = "<title>FX fundamental bias</title>",
    palette: str = PALETTE,
    dark_media: str = DARK_MEDIA,
    dark_attribute: str = DARK_ATTRIBUTE,
    body_rule: str = BODY,
    head_extra: str = "",
    body_extra: str = "",
    script: str = "",
) -> str:
    """One document, with exactly the part under test replaced.

    Built from parts rather than by editing a single string, so a test that
    removes the dark media block cannot accidentally remove anything else and
    pass for the wrong reason.
    """
    inline_script = f"<script>{script}</script>" if script else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    {title}
    {head_extra}
    <style>
{palette}
{dark_media}
{dark_attribute}
{body_rule}
    </style>
  </head>
  <body>
    <h1>FX fundamental bias</h1>
    {body_extra}
    {inline_script}
  </body>
</html>
"""


def reported(html: str) -> str:
    """Every message joined, for asserting what a violation says.

    Assert on the offending host or path rather than on an allowed one. The
    reference message recites the allow-list it rejected the URL against, so
    ``"cdnjs.cloudflare.com" in reported(...)`` is true of a page that has no
    script at all, and a test written that way would pass on the recital.
    """
    return " | ".join(check_constraints(html))


# --- the page that is fine ---------------------------------------------------


def test_a_publishable_page_reports_nothing() -> None:
    """The fixture is the positive case, and it is the one that can rot.

    A guard with no passing example drifts into rejecting everything, and
    nobody notices until the renderer it is meant to serve cannot satisfy it.
    """
    html = FIXTURE.read_text(encoding="utf-8")

    assert check_constraints(html) == []


# --- size --------------------------------------------------------------------


def test_a_document_over_the_ceiling_is_reported() -> None:
    """The sandbox refuses it, and refusing it here costs nothing."""
    padding = "<!-- " + "x" * MAX_RENDERED_BYTES + " -->"

    messages = check_constraints(document(body_extra=padding))

    assert any(str(MAX_RENDERED_BYTES) in message for message in messages)


def test_a_document_just_under_the_ceiling_is_not_reported() -> None:
    """The boundary from the other side, so the comparison cannot be inverted.

    Written against `MAX_RENDERED_BYTES` rather than a literal, because a test
    carrying its own copy of the ceiling passes when the two disagree.
    """
    base = len(document().encode("utf-8"))
    padding = "<!-- " + "x" * (MAX_RENDERED_BYTES - base - 200) + " -->"
    html = document(body_extra=padding)

    assert len(html.encode("utf-8")) < MAX_RENDERED_BYTES
    assert check_constraints(html) == []


def test_the_ceiling_is_measured_in_bytes_rather_than_characters() -> None:
    """A page of multi-byte characters is bigger than its length suggests.

    The sandbox counts what it downloads. This document is comfortably under
    the ceiling in characters and over it in bytes, so an implementation
    measuring ``len(html)`` accepts a page the sandbox refuses. Every currency
    name and event title on the real page comes from a feed, so non-ASCII is
    the normal case rather than a contrived one.
    """
    padding = "€" * (MAX_RENDERED_BYTES // 2)
    html = document(body_extra=f"<p>{padding}</p>")

    assert len(html) < MAX_RENDERED_BYTES
    assert len(html.encode("utf-8")) > MAX_RENDERED_BYTES
    assert any(
        str(MAX_RENDERED_BYTES) in message for message in check_constraints(html)
    )


def test_a_document_of_exactly_the_ceiling_is_reported() -> None:
    """The ceiling admits nothing, which is what "under 16MB" means.

    One byte, and the only way to find out which side the comparison falls on
    is the page that does not load. ``docs/interfaces.md`` and the module
    header both say under, so exactly the ceiling is over it.
    """
    comment = "<!-- {} -->"
    base = len(document(body_extra=comment.format("")).encode("utf-8"))
    html = document(body_extra=comment.format("x" * (MAX_RENDERED_BYTES - base)))

    assert len(html.encode("utf-8")) == MAX_RENDERED_BYTES
    assert any(
        str(MAX_RENDERED_BYTES) in message for message in check_constraints(html)
    )


# --- external references -----------------------------------------------------


def test_a_stylesheet_from_an_arbitrary_host_is_reported() -> None:
    link = '<link rel="stylesheet" href="https://example.com/app.css" />'

    assert "example.com" in reported(document(head_extra=link))


def test_an_external_image_is_reported() -> None:
    """Blocked by the sandbox, and it leaves a gap rather than an error."""
    image = '<img src="https://example.com/chart.png" alt="chart" />'

    assert "example.com" in reported(document(body_extra=image))


def test_a_script_from_an_unlisted_cdn_is_reported() -> None:
    script = '<script src="https://unpkg.com/d3@7"></script>'

    assert "unpkg.com" in reported(document(head_extra=script))


@pytest.mark.parametrize("host", ALLOWED_SCRIPT_HOSTS)
def test_a_script_from_an_allowed_host_is_accepted(host: str) -> None:
    """Parametrised over the constant, so adding a host cannot skip a test."""
    script = f'<script src="https://{host}/npm/d3@7/dist/d3.min.js"></script>'

    assert check_constraints(document(head_extra=script)) == []


def test_a_stylesheet_from_google_fonts_is_accepted() -> None:
    link = (
        '<link rel="stylesheet" '
        'href="https://fonts.googleapis.com/css2?family=Inter" />'
    )

    assert check_constraints(document(head_extra=link)) == []


def test_a_font_file_from_the_allowed_host_is_accepted() -> None:
    """The font arrives through CSS rather than through an attribute.

    `@font-face` names its file in a `url()`, so a check that only walked
    `src` and `href` attributes would pass every external font ever embedded,
    including ones from hosts the sandbox blocks.
    """
    face = """
      @font-face {
        font-family: "Inter";
        src: url("https://fonts.gstatic.com/s/inter/v13/inter.woff2");
      }
"""

    assert check_constraints(document(body_rule=BODY + face)) == []


def test_a_font_file_from_an_arbitrary_host_is_reported() -> None:
    """The other half of the case above, which is the one that fails silently."""
    face = """
      @font-face {
        font-family: "Inter";
        src: url("https://cdn.example.com/inter.woff2");
      }
"""

    assert "cdn.example.com" in reported(document(body_rule=BODY + face))


def test_a_data_uri_is_accepted() -> None:
    pixel = (
        '<img alt="" src="data:image/gif;base64,'
        'R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==" />'
    )

    assert check_constraints(document(body_extra=pixel)) == []


def test_a_fragment_link_is_accepted() -> None:
    """Within the page, so nothing is fetched."""
    anchor = '<a href="#coverage">Coverage</a><h2 id="coverage">Coverage</h2>'

    assert check_constraints(document(body_extra=anchor)) == []


def test_a_relative_path_is_reported() -> None:
    """One file means one file. A relative asset resolves to nothing.

    This is the case a developer hits first, because it works when the page is
    opened from disk beside its assets and breaks the moment it is published.
    """
    link = '<link rel="stylesheet" href="styles/app.css" />'

    assert "styles/app.css" in reported(document(head_extra=link))


def test_an_ordinary_link_to_another_site_is_reported_too() -> None:
    """Deliberate, and the surprising half of the rule.

    The constraint is written over every ``src`` and ``href``, so a plain link
    to a source's own page is reported like any other external reference. It
    is pinned here rather than left to the implementation's docstring, because
    the test file is what a later reader checks before deciding this is a bug
    and removing it. Changing the behaviour means changing the constraint.
    """
    anchor = '<a href="https://www.federalreserve.gov/">Fed</a>'

    assert "federalreserve.gov" in reported(document(body_extra=anchor))


def test_a_host_that_only_looks_allowed_is_reported() -> None:
    """The allow-list is matched whole, not as a substring.

    ``cdnjs.cloudflare.com.example`` contains an allowed host and is not one,
    and a containment test is the usual way an allow-list is got around. The
    consequence here is the quiet one: the script does not load and the panel
    it drew is empty.
    """
    script = '<script src="https://cdnjs.cloudflare.com.example/d3.js"></script>'

    assert "cdnjs.cloudflare.com.example" in reported(document(head_extra=script))


def test_an_external_url_carrying_a_fragment_is_still_external() -> None:
    """A fragment is allowed because it stays on the page, not because of "#"."""
    link = '<link rel="stylesheet" href="https://example.com/app.css#panel" />'

    assert "example.com" in reported(document(head_extra=link))


def test_an_allowed_script_host_is_matched_on_the_host_and_not_the_path() -> None:
    """Host-level, and said out loud because two documents read differently.

    ``ALLOWED_SCRIPT_HOSTS`` holds hosts, and the criterion on #251 says a
    script from ``cdnjs.cloudflare.com`` is accepted. The module header says
    ``cdn.jsdelivr.net/npm/``, with a path. This pins what the constant says,
    and the discrepancy is on the issue for the desk rather than settled by
    the guard inventing a rule its own constant does not carry.
    """
    script = '<script src="https://cdn.jsdelivr.net/gh/user/repo@1/x.js"></script>'

    assert check_constraints(document(head_extra=script)) == []


# --- inline script that reaches off the page ---------------------------------


@pytest.mark.parametrize(
    "call",
    [
        'fetch("/api/bias")',
        "new XMLHttpRequest()",
        'new WebSocket("wss://example.com")',
    ],
)
def test_a_call_that_leaves_the_page_is_reported(call: str) -> None:
    """Each separately, because the sandbox blocks all three the same way.

    The page still renders. The panel that needed the data is empty, and an
    empty panel on a bias dashboard reads as no opinion rather than as a
    failure, which is the worst of the three outcomes.
    """
    messages = check_constraints(document(script=f"const x = {call};"))

    assert messages
    assert any(call.split("(")[0].split()[-1] in message for message in messages)


def test_a_script_that_only_touches_the_document_is_accepted() -> None:
    script = 'document.documentElement.dataset.theme = "dark";'

    assert check_constraints(document(script=script)) == []


def test_the_word_fetch_in_prose_is_not_a_violation() -> None:
    """The check is about script, and a page about a data fetch says the word.

    A substring search over the whole document reports the footer sentence
    explaining that a fetch failed, which is exactly the sentence the report is
    supposed to carry.
    """
    prose = "<p>The calendar fetch failed, so the window is unknown.</p>"

    assert check_constraints(document(body_extra=prose)) == []


def test_a_handler_attribute_is_inline_script_too() -> None:
    """The page carries a theme toggle, so this is where its script will be.

    `onclick` is inline script by any reading, and a check that walks only
    `<script>` bodies certifies the page that does its fetching from a button.
    """
    button = "<button onclick=\"fetch('/api/bias')\">Refresh</button>"

    assert "fetch" in reported(document(body_extra=button))


# --- the palette and the two dark blocks -------------------------------------


def test_a_palette_defined_only_inside_a_media_block_is_reported() -> None:
    """The light page then has no tokens at all, and renders unstyled.

    This is the theming mistake that looks correct in a dark-mode browser and
    is discovered by the one person who has their phone set to light.
    """
    messages = check_constraints(document(palette=""))

    assert any(":root" in message for message in messages)


def test_a_bare_root_block_with_no_custom_property_is_reported() -> None:
    """A `:root` block that sets no token is not a palette.

    Checked because the obvious implementation looks for the selector and
    stops there, which passes a page whose tokens are all defined in the dark
    blocks and whose light mode is blank.
    """
    empty = """
      :root {
        font-size: 16px;
      }
"""

    assert check_constraints(document(palette=empty)) != []


def test_a_page_with_no_preference_media_override_is_reported() -> None:
    messages = check_constraints(document(dark_media=""))

    assert any("prefers-color-scheme" in message for message in messages)


def test_a_page_with_no_explicit_toggle_override_is_reported() -> None:
    """An explicit toggle has to win in both directions.

    Without this block the header's theme button appears to do nothing on a
    phone whose system theme is already dark, which reads as a broken control
    rather than as a missing rule.
    """
    messages = check_constraints(document(dark_attribute=""))

    assert any('[data-theme="dark"]' in message for message in messages)


def test_a_bare_root_inside_a_media_block_does_not_count_as_the_palette() -> None:
    """ "Bare" means outside the at-rules, not merely "written without a suffix".

    This is the document the criterion is about and it is the one an obvious
    implementation accepts: the selector is exactly ``:root``, and it is still
    invisible to every reader whose system is not dark.
    """
    nested = """
      @media (prefers-color-scheme: dark) {
        :root {
          --surface: #0f1215;
          --ink: #e9edf1;
        }
      }
"""

    messages = check_constraints(document(palette=nested))

    assert any(":root" in message for message in messages)


def test_another_media_query_does_not_satisfy_the_dark_requirement() -> None:
    """The page is required to be responsive, so it has other media queries.

    An implementation that looks for any ``@media`` carrying a token passes
    every page with a width breakpoint and no dark override, which is the
    wrong implementation someone would actually write.
    """
    responsive = """
      @media (max-width: 420px) {
        :root {
          --gutter: 12px;
        }
      }
"""

    messages = check_constraints(document(dark_media=responsive))

    assert any("prefers-color-scheme" in message for message in messages)


@pytest.mark.parametrize("block", ["dark_media", "dark_attribute"])
def test_a_dark_block_that_defines_no_token_is_reported(block: str) -> None:
    """A selector with no custom property in it is not an override.

    The same hole the bare palette is checked for, on the two blocks that
    override it. A page carrying ``color-scheme: dark`` and no tokens has the
    shape of a themed page and the colours of an unthemed one.
    """
    hollow_media = """
      @media (prefers-color-scheme: dark) {
        :root:not([data-theme="light"]) {
          color-scheme: dark;
        }
      }
"""
    hollow_attribute = """
      :root[data-theme="dark"] {
        color-scheme: dark;
      }
"""
    hollow = {"dark_media": hollow_media, "dark_attribute": hollow_attribute}

    assert check_constraints(document(**{block: hollow[block]})) != []


# --- the body background -----------------------------------------------------


def test_a_body_with_no_background_is_reported_with_the_reason() -> None:
    """The message has to carry why, because the page looks fine locally.

    A transparent body inherits whatever the host paints behind it, so the
    page renders dark text on a dark ground for exactly the readers who did
    not choose it.
    """
    no_background = """
      body {
        color: var(--ink);
      }
"""

    messages = [
        message
        for message in check_constraints(document(body_rule=no_background))
        if "background" in message
    ]

    assert messages
    assert any("transparent" in message or "host" in message for message in messages)


def test_a_body_rule_that_is_absent_entirely_is_reported() -> None:
    assert any(
        "background" in message for message in check_constraints(document(body_rule=""))
    )


def test_a_background_on_another_selector_does_not_satisfy_the_body_rule() -> None:
    """The real page declares a background on every card and on the heatmap.

    An implementation searching the whole stylesheet for the word passes a
    page whose ``body`` sets only a colour, which is the page that renders its
    text onto whatever the host painted.
    """
    elsewhere = """
      body {
        color: var(--ink);
      }

      .card {
        background: var(--surface-raised);
      }
"""

    assert any(
        "background" in message
        for message in check_constraints(document(body_rule=elsewhere))
    )


def test_a_body_background_defined_only_in_a_media_block_is_reported() -> None:
    """Same rule as the palette: no colour gets its only definition there.

    A reader whose system is light then gets a transparent body, which is the
    exact failure the criterion names, arriving through the block that was
    supposed to be an override.
    """
    conditional = """
      body {
        color: var(--ink);
      }

      @media (prefers-color-scheme: dark) {
        body {
          background: var(--surface);
        }
      }
"""

    assert any(
        "background" in message
        for message in check_constraints(document(body_rule=conditional))
    )


# --- the title ---------------------------------------------------------------


def test_a_missing_title_is_reported() -> None:
    assert any("title" in message for message in check_constraints(document(title="")))


def test_a_title_inside_an_svg_does_not_title_the_page() -> None:
    """The charts are drawn as inline SVG, so the two are certain to meet.

    `<title>` inside `<svg>` is the accessible name of the chart and does
    nothing for the tab. Counting it leaves the owner with an untitled tab
    among the others, which is what this constraint exists to prevent, and
    with a guard that said the page was fine.
    """
    chart = '<svg role="img"><title>Currency ranking</title></svg>'

    messages = check_constraints(document(title="", body_extra=chart))

    assert any("title" in message for message in messages)


def test_an_empty_title_is_reported() -> None:
    """A tab reading "index" is the same failure as no tab name at all."""
    assert any(
        "title" in message
        for message in check_constraints(document(title="<title></title>"))
    )


def test_a_toggle_override_nested_in_the_preference_query_is_reported() -> None:
    """The explicit toggle has to win in both directions, so it cannot nest.

    An override inside `prefers-color-scheme: dark` cannot beat that
    preference, so a reader on a light system presses the button and nothing
    happens, which is the failure the message for this check describes. The
    selector is present and the page is still broken, which is why the check
    has to look outside the at-rules rather than anywhere.
    """
    nested = ""
    both_inside = """
      @media (prefers-color-scheme: dark) {
        :root:not([data-theme="light"]) {
          --surface: #0f1215;
        }

        :root[data-theme="dark"] {
          --surface: #0f1215;
        }
      }
"""

    messages = check_constraints(
        document(dark_media=both_inside, dark_attribute=nested)
    )

    assert any('[data-theme="dark"]' in message for message in messages)


# --- a document the parser could not finish ----------------------------------


def test_a_page_cut_off_inside_a_script_is_refused_rather_than_cleared() -> None:
    """The one answer a guard must never give for a page it could not read.

    `HTMLParser` buffers a script body until it sees the closing tag, so a
    document that ends inside one leaves that script and everything after it
    unparsed. Before this was handled the page came back with no violations at
    all: the `fetch` was invisible, and so was every reference after it. A
    renderer that raises part way through a write produces exactly this file.
    """
    truncated = (
        "<!doctype html><html><head><title>Bias</title><style>"
        ":root { --surface: #fff; }"
        '@media (prefers-color-scheme: dark) { :root { --surface: #111; } }'
        ':root[data-theme="dark"] { --surface: #111; }'
        "body { background: var(--surface); }"
        "</style></head><body><script>fetch(\"https://evil.example/x\")"
    )

    messages = check_constraints(truncated)

    assert messages
    assert any("unclosed" in message for message in messages)


def test_a_complete_page_is_not_reported_as_truncated() -> None:
    """The other side, so the check cannot be a constant."""
    assert not any(
        "unclosed" in message
        for message in check_constraints(FIXTURE.read_text(encoding="utf-8"))
    )


# --- every violation, not the first ------------------------------------------


def test_three_violations_come_back_as_three_messages() -> None:
    """The criterion that makes this a checker rather than a validator.

    Returning the first means a caller fixes one, re-runs, finds the next, and
    round-trips once per constraint. Three separate causes here, chosen so no
    one of them could plausibly produce the other two.
    """
    html = document(
        title="",
        body_rule="",
        head_extra='<link rel="stylesheet" href="https://example.com/app.css" />',
    )

    messages = check_constraints(html)

    assert len(messages) >= 3
    assert any("title" in message for message in messages)
    assert any("background" in message for message in messages)
    assert any("example.com" in message for message in messages)


def test_every_message_names_the_constraint_and_what_it_found() -> None:
    """A message a reader can act on without opening the page.

    Both halves, because the title is a promise about both. The constraint
    half is the prose, and a bare label such as "External reference" fails the
    length assertion. The "what it found" half is the offending artefact
    itself: a message naming the rule and not the URL sends the reader back to
    the page to find out which one, at six in the morning, which is the moment
    this whole function exists to avoid.
    """
    html = document(
        title="",
        body_rule="",
        head_extra='<img src="https://example.com/chart.png" alt="" />',
        script='fetch("/api")',
    )

    messages = check_constraints(html)
    joined = " | ".join(messages)

    assert messages
    for found in ("example.com", "fetch", "title", "background"):
        assert found in joined, found
    for message in messages:
        assert len(message) > 30, message
        assert message[0].isupper() or message[0] == "`", message
        assert message.rstrip().endswith("."), message


def test_the_result_is_a_list_rather_than_a_generator() -> None:
    """A caller counts it and then prints it, which a generator makes wrong.

    An exhausted generator reports no violations on the second read, and the
    second read is the one in the error message.
    """
    messages = check_constraints(document(title=""))

    assert isinstance(messages, list)
    assert messages == check_constraints(document(title=""))


# --- degenerate input --------------------------------------------------------


def test_an_empty_document_is_reported_rather_than_accepted() -> None:
    """The degenerate input, which must never read as publishable.

    An empty string satisfies no constraint, and a checker written as a series
    of "if present and wrong" tests returns an empty list for it, which reads
    as a clean bill of health for a blank page.
    """
    assert check_constraints("") != []


@pytest.mark.parametrize("host", ALLOWED_STYLE_HOSTS)
def test_the_allowed_style_hosts_are_each_accepted(host: str) -> None:
    """Parametrised over the constant for the same reason as the scripts."""
    link = f'<link rel="stylesheet" href="https://{host}/css2?family=Inter" />'

    assert check_constraints(document(head_extra=link)) == []
