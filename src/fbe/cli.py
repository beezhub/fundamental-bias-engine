r"""Command line surface for the fundamental bias engine.

The commands are organised around the daily routine in the trading plan rather
than around the internals of the engine. Whatever the plan asks for at a given
point in the day should be one command, and work that belongs to different
parts of the day should not be bundled into the same command.

The daily sequence, mapped to the routine in ``docs/reference/``:

1. Morning routine, news and calendar review::

       fbe refresh
       fbe calendar --hours 24

   ``refresh`` pulls the day's data into the cache once so every later command
   reads the same snapshot. ``calendar`` turns the plan's "avoid trading during
   high-impact news" rule into concrete blackout windows with clock times.

2. Pre-market analysis::

       fbe score --pillars
       fbe bias --majors --min-conviction medium --tradeable-only
       fbe report
       fbe dashboard

   ``score`` and ``bias`` are the directional layer. ``report`` writes the
   durable record of the call. ``dashboard`` puts the same content on the phone
   for the hours away from the desk.

3. Trading session, at the moment of entry::

       fbe calendar --pair EURUSD --hours 4
       fbe size EURUSD --entry 1.0850 --stop 1.0812

   The entry itself comes from the trader's trendline and channel rules. The
   engine only says which way to lean, whether the window is clear, and how many
   units the 1-2% risk rule allows for that stop distance.

4. Trade management, with a position open::

       fbe calendar --hours 6

   Worth re-running through the session. Events do not move, but the clock does,
   and a stop that sat outside every blackout at 08:00 can sit inside one by
   14:00.

5. Post-market review and the evening journal::

       fbe journal add EURUSD --direction long --entry 1.0850 --stop 1.0812 \
           --exit 1.0902 --followed-plan
       fbe journal review --days 7

   The review is where the engine gets graded: which bias calls were taken,
   which were skipped, and whether the losses came from the bias or from the
   execution. Those are different failures with different fixes.

``fbe doctor`` sits outside the routine. It is the first command to run when
something looks wrong, before assuming the model is at fault: it checks the
config, the API keys, the cache state and whether each source answers.

Exit codes are part of the contract, so the commands can be chained in a shell
script or a cron job without parsing output:

* ``0``: the command did what it was asked.
* ``1``: the command ran but the result is not usable, for example every source
  failed, or ``doctor --strict`` found a warning.
* ``2``: usage error, raised by the argument parser.
* ``3``: the command refused on a guard rule, for example ``size`` inside a
  blackout window without ``--force``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from fbe.types import Conviction, Direction

__all__ = ["app", "journal_app"]

EXIT_OK = 0
"""Everything worked."""

EXIT_UNUSABLE = 1
"""The command ran but its output should not be traded on."""

EXIT_BLOCKED = 3
"""A guard rule refused the request. Not an error, a decision."""


class OutputFormat(StrEnum):
    """Machine or human rendering for the tabular commands.

    ``table`` is rich text for a terminal. ``json`` and ``csv`` exist so the
    engine can feed a spreadsheet or another script without screen scraping.
    """

    TABLE = "table"
    JSON = "json"
    CSV = "csv"


class Impact(StrEnum):
    """Minimum calendar impact level to report on."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


app = typer.Typer(
    name="fbe",
    help=(
        "Relative-value fundamental bias for G10 FX. Supplies the directional "
        "layer only: entries stay with your trendline and channel rules."
    ),
    no_args_is_help=True,
    add_completion=False,
)

journal_app = typer.Typer(
    name="journal",
    help="Trade journal: the evening step of the daily routine.",
    no_args_is_help=True,
)
app.add_typer(journal_app, name="journal")


DATE_FORMATS = ["%Y-%m-%d"]
"""Accepted ``--asof`` input. One format, so dates in reports, filenames and
journal entries never disagree about what 03/04 means."""


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help=(
                "Path to a config YAML. Defaults to config.yaml at the repo "
                "root when present, otherwise built-in defaults."
            ),
            exists=False,
            dir_okay=False,
        ),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline/--online",
            help=(
                "Read the cache only and never touch the network. Makes a run "
                "reproducible and lets you work through a bad connection."
            ),
        ),
    ] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help=(
                "Increase log detail. Once shows per-source timings, twice "
                "shows every request and every pillar input."
            ),
        ),
    ] = 0,
) -> None:
    """Resolve global options and stash the effective config on the context.

    Every command reads its config from ``ctx.obj`` rather than loading it
    again, so one run of the CLI has exactly one config digest. Commands that
    write a report record that digest, which is what makes an old report
    reproducible.

    Args:
        ctx: Typer context. The resolved `fbe.config.Config` is attached to
            ``ctx.obj``.
        config: Optional path to a config YAML overriding the defaults.
        offline: When true, force `fbe.config.DataConfig.offline`.
        verbose: Repeat count for ``-v``, mapped to a logging level.

    Raises:
        NotImplementedError: Always, until the config layer lands.

    """
    raise NotImplementedError("fbe.cli.main is scaffolded, not implemented")


@app.command()
def doctor(
    ctx: typer.Context,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Seconds to wait for each source reachability probe.",
            min=0.5,
        ),
    ] = 5.0,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero on warnings, not only on hard failures.",
        ),
    ] = False,
    show_keys: Annotated[
        bool,
        typer.Option(
            "--show-keys",
            help=(
                "Print the first four characters of each API key. Off by "
                "default so the output can be pasted anywhere."
            ),
        ),
    ] = False,
) -> None:
    """Check config, credentials, cache and source reachability.

    Run this first whenever the output looks wrong. It answers the four
    questions that explain almost every bad run, in order: is the config valid,
    are the credentials present, is the cache fresh, and are the sources
    answering. Each check prints its own verdict so a single failure does not
    hide the rest.

    Checks performed:
        * `fbe.config.Config.validate` problems, for example pillar weights that
          do not sum to 1.0 or a risk cap above the plan's 2%.
        * Presence of ``FRED_API_KEY`` and any other configured credential.
        * Cache directory writability, entry count, and the age of the oldest
          entry against ``cache_ttl_hours``.
        * `fbe.types.DataSource.available` for every registered source, then a
          cheap live probe unless ``--offline`` is set.
        * Whether ``data/reports`` holds a previous report to diff against.

    Args:
        ctx: Typer context carrying the effective config.
        timeout: Per-probe timeout in seconds.
        strict: Treat warnings as failures for the exit code.
        show_keys: Print truncated key prefixes to confirm which key is loaded.

    Raises:
        NotImplementedError: Always, until the health checks land.

    """
    raise NotImplementedError("fbe.cli.doctor is scaffolded, not implemented")


@app.command()
def refresh(
    ctx: typer.Context,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            "-s",
            help=(
                "Refresh only these sources, repeatable, for example "
                "-s fred -s cftc. Default refreshes every available source."
            ),
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help=(
                "Refetch even when the cached copy is inside its TTL. Use "
                "after a data correction or a source outage."
            ),
        ),
    ] = False,
    since: Annotated[
        datetime | None,
        typer.Option(
            "--since",
            formats=DATE_FORMATS,
            help=(
                "Earliest period to fetch, YYYY-MM-DD. Defaults to the "
                "scoring lookback window."
            ),
        ),
    ] = None,
) -> None:
    """Pull fresh data from the sources into the local cache.

    Refresh is deliberately separate from scoring. Scoring runs many times a day
    and must be instant and repeatable; fetching runs once and is the only step
    that can be slow, rate limited or offline. Splitting them also means a
    network failure at 07:00 leaves yesterday's cache intact and scoreable
    rather than leaving the morning with nothing.

    Args:
        ctx: Typer context carrying the effective config.
        source: Restrict the refresh to these source keys.
        force: Ignore cache TTLs and refetch.
        since: Earliest period to request from each source.

    Raises:
        NotImplementedError: Always, until the source layer lands.

    """
    raise NotImplementedError("fbe.cli.refresh is scaffolded, not implemented")


@app.command()
def score(
    ctx: typer.Context,
    asof: Annotated[
        datetime | None,
        typer.Option(
            "--asof",
            formats=DATE_FORMATS,
            help=(
                "Score the world as it looked on this date, YYYY-MM-DD. "
                "Observations released later are excluded. Defaults to today."
            ),
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--format",
            help="Rendering: table for reading, json or csv for piping.",
        ),
    ] = OutputFormat.TABLE,
    pillars: Annotated[
        bool,
        typer.Option(
            "--pillars/--no-pillars",
            help=(
                "Show the seven pillar scores behind each composite. Off gives "
                "the ranking only."
            ),
        ),
    ] = False,
    currency: Annotated[
        list[str] | None,
        typer.Option(
            "--currency",
            "-C",
            help="Limit output to these currencies, repeatable. Ranks still "
            "come from the full universe.",
        ),
    ] = None,
) -> None:
    """Compute and print the currency ranking with its pillar breakdown.

    Output is one row per G10 currency, ordered strongest to weakest by
    composite score on the -3 to +3 band, with rank, dispersion across pillars
    and data coverage. With ``--pillars`` each pillar score is shown as its own
    column, which is the only way to see whether a composite rests on one
    dominant pillar or on seven that agree.

    Ranks always come from the full universe even when ``--currency`` narrows
    the printed rows, because the scores are cross-sectional: a currency has no
    standing except relative to the others.

    Args:
        ctx: Typer context carrying the effective config.
        asof: Point-in-time cutoff for observations.
        output_format: table, json or csv.
        pillars: Include per-pillar columns.
        currency: Restrict printed rows to these currencies.

    Raises:
        NotImplementedError: Always, until `fbe.scoring` lands.

    """
    raise NotImplementedError("fbe.cli.score is scaffolded, not implemented")


@app.command()
def bias(
    ctx: typer.Context,
    asof: Annotated[
        datetime | None,
        typer.Option(
            "--asof",
            formats=DATE_FORMATS,
            help="Point-in-time cutoff, YYYY-MM-DD. Defaults to today.",
        ),
    ] = None,
    majors: Annotated[
        bool,
        typer.Option(
            "--majors/--all-pairs",
            help=(
                "Restrict to the seven dollar majors. The plan's low-spread "
                "rule points here first on a small account."
            ),
        ),
    ] = False,
    min_conviction: Annotated[
        Conviction,
        typer.Option(
            "--min-conviction",
            help="Drop pairs below this conviction level.",
        ),
    ] = Conviction.NONE,
    tradeable_only: Annotated[
        bool,
        typer.Option(
            "--tradeable-only",
            help=(
                "Hide pairs carrying a blocker such as an imminent "
                "high-impact release on either leg."
            ),
        ),
    ] = False,
    matrix: Annotated[
        bool,
        typer.Option(
            "--matrix/--ranked",
            help=(
                "Print the 8x8 base against quote grid instead of the ranked "
                "list. The grid shows structure, the list shows priority."
            ),
        ),
    ] = False,
    top: Annotated[
        int | None,
        typer.Option(
            "--top",
            "-n",
            help="Show only the strongest N pairs of the ranked list.",
            min=1,
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Rendering: table, json or csv."),
    ] = OutputFormat.TABLE,
) -> None:
    """Print the pair matrix and the ranked directional calls.

    A pair bias is the difference between two currency scores, never a score of
    its own. The ranked view sorts by the size of that spread, so the top of the
    list is where the fundamental disagreement between two economies is widest.
    The matrix view shows the same numbers laid out base against quote, which is
    where you notice that one currency is on the wrong side of every row and the
    real trade is that currency, not the pair.

    Filters compose: ``--majors --min-conviction medium --tradeable-only`` is the
    pre-market shortlist and is what `fbe.cli.report` puts in the shortlist
    section.

    Args:
        ctx: Typer context carrying the effective config.
        asof: Point-in-time cutoff for observations.
        majors: Restrict to `fbe.universe.MAJORS`.
        min_conviction: Minimum `fbe.types.Conviction` to include.
        tradeable_only: Drop pairs with blockers set.
        matrix: Render the grid instead of the ranked list.
        top: Truncate the ranked list.
        output_format: table, json or csv.

    Raises:
        NotImplementedError: Always, until `fbe.bias` lands.

    """
    raise NotImplementedError("fbe.cli.bias is scaffolded, not implemented")


@app.command()
def calendar(
    ctx: typer.Context,
    hours: Annotated[
        int,
        typer.Option(
            "--hours",
            "-H",
            help="Horizon in hours from now. 24 covers the morning review.",
            min=1,
        ),
    ] = 24,
    currency: Annotated[
        list[str] | None,
        typer.Option(
            "--currency",
            "-C",
            help="Only events for these currencies, repeatable.",
        ),
    ] = None,
    pair: Annotated[
        list[str] | None,
        typer.Option(
            "--pair",
            "-p",
            help=(
                "Only events touching either leg of these pairs, repeatable. "
                "The check to run before pressing the button."
            ),
        ),
    ] = None,
    impact: Annotated[
        Impact,
        typer.Option(
            "--impact",
            help="Minimum published impact level to show.",
        ),
    ] = Impact.HIGH,
    blackouts: Annotated[
        bool,
        typer.Option(
            "--blackouts/--events",
            help=(
                "Show the merged blackout windows per pair rather than the "
                "underlying event list."
            ),
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Rendering: table, json or csv."),
    ] = OutputFormat.TABLE,
) -> None:
    """List upcoming releases and the blackout windows they create.

    This is the morning routine step. The plan says to avoid trading during
    high-impact news, which is only actionable once "high impact" has a clock
    time attached to it and a list of which pairs it touches. Each event is
    expanded into a window using the configured minutes before and after, then
    windows on the same pair are merged so the output is a short list of times
    to stand aside rather than a wall of releases.

    Times print in the local timezone with the UTC offset shown, because a
    calendar that is ambiguous about the hour is worse than no calendar.

    Args:
        ctx: Typer context carrying the effective config.
        hours: Horizon in hours from now.
        currency: Restrict to these currencies.
        pair: Restrict to events touching either leg of these pairs.
        impact: Minimum impact level.
        blackouts: Show merged windows instead of raw events.
        output_format: table, json or csv.

    Raises:
        NotImplementedError: Always, until `fbe.calendar_guard` lands.

    """
    raise NotImplementedError("fbe.cli.calendar is scaffolded, not implemented")


@app.command()
def size(
    ctx: typer.Context,
    pair: Annotated[
        str,
        typer.Argument(help="Pair in market convention, for example EURUSD."),
    ],
    entry: Annotated[
        float,
        typer.Option("--entry", "-e", help="Planned entry price."),
    ],
    stop: Annotated[
        float,
        typer.Option(
            "--stop",
            "-s",
            help=(
                "Stop price, placed beyond the trendline or channel as the "
                "plan requires. Distance from entry sets the size."
            ),
        ),
    ],
    conviction: Annotated[
        Conviction | None,
        typer.Option(
            "--conviction",
            help=(
                "Override the engine's conviction for this pair. Use when the "
                "technical setup is better or worse than the bias alone."
            ),
        ),
    ] = None,
    direction: Annotated[
        Direction | None,
        typer.Option(
            "--direction",
            help=(
                "Override the engine's direction. Sizing a trade against the "
                "engine's bias is allowed, but it is printed as a warning."
            ),
        ),
    ] = None,
    balance: Annotated[
        float | None,
        typer.Option(
            "--balance",
            "-b",
            help="Account balance. Defaults to the configured balance.",
            min=0.0,
        ),
    ] = None,
    risk: Annotated[
        float | None,
        typer.Option(
            "--risk",
            "-r",
            help=(
                "Risk fraction for this trade, for example 0.01 for 1%. "
                "Defaults to the conviction-scaled value inside the plan's "
                "1-2% band, and is clamped to that band."
            ),
            min=0.0,
            max=0.02,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=("Size the trade even inside a blackout window. Exits 3 without it."),
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Rendering: table, json or csv."),
    ] = OutputFormat.TABLE,
) -> None:
    """Size a proposed trade from its stop distance.

    This is the command used at the moment of the trade, so it takes the two
    numbers the trader has on the chart, entry and stop, and returns units, lots
    and the money at risk in the account currency. Entry and stop are named
    options rather than positional arguments on purpose: two bare numbers on a
    command line are easy to transpose, and a transposed stop silently doubles
    the risk.

    The risk fraction is derived from conviction inside the plan's 1-2% band and
    is clamped there. Anything the risk module flags, a stop tighter than the
    typical spread, exposure that collides with an open correlated position, a
    pair whose bias points the other way, comes back as a warning attached to
    the `fbe.types.PositionSize` rather than being silently applied.

    Args:
        ctx: Typer context carrying the effective config.
        pair: Pair in market convention.
        entry: Planned entry price.
        stop: Stop price.
        conviction: Optional conviction override.
        direction: Optional direction override.
        balance: Optional account balance override.
        risk: Optional risk fraction override inside the 1-2% band.
        force: Proceed despite an active blackout window.
        output_format: table, json or csv.

    Raises:
        NotImplementedError: Always, until `fbe.risk` lands.

    """
    raise NotImplementedError("fbe.cli.size is scaffolded, not implemented")


@app.command()
def report(
    ctx: typer.Context,
    asof: Annotated[
        datetime | None,
        typer.Option(
            "--asof",
            formats=DATE_FORMATS,
            help="Point-in-time cutoff, YYYY-MM-DD. Defaults to today.",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help=(
                "Directory for the dated Markdown file. Defaults to the "
                "configured reports directory, data/reports."
            ),
            file_okay=False,
        ),
    ] = None,
    compare: Annotated[
        str | None,
        typer.Option(
            "--compare",
            help=(
                "Baseline for the what-changed section: 'last' for the most "
                "recent report on disk, 'none' to skip it, or a path."
            ),
        ),
    ] = "last",
    stdout: Annotated[
        bool,
        typer.Option(
            "--stdout",
            help="Print the Markdown instead of writing a file.",
        ),
    ] = False,
) -> None:
    """Render the full dated Markdown report into the reports directory.

    The report is the audit trail. It carries the as-of date, the config digest
    that produced it, the full ranking, the pair matrix, the shortlist with its
    reasoning, the calendar, the data coverage and every warning, plus a diff
    against the previous run.

    Args:
        ctx: Typer context carrying the effective config.
        asof: Point-in-time cutoff for observations.
        out: Output directory for the dated file.
        compare: Diff baseline, ``last``, ``none`` or a path.
        stdout: Print instead of writing.

    Raises:
        NotImplementedError: Always, until `fbe.report` lands.

    """
    raise NotImplementedError("fbe.cli.report is scaffolded, not implemented")


@app.command()
def dashboard(
    ctx: typer.Context,
    asof: Annotated[
        datetime | None,
        typer.Option(
            "--asof",
            formats=DATE_FORMATS,
            help="Point-in-time cutoff, YYYY-MM-DD. Defaults to today.",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help=(
                "Output HTML file. Defaults to data/reports/dashboard-YYYY-MM-DD.html."
            ),
            dir_okay=False,
        ),
    ] = None,
    compare: Annotated[
        str | None,
        typer.Option(
            "--compare",
            help="Diff baseline, same values as report --compare.",
        ),
    ] = "last",
    open_after: Annotated[
        bool,
        typer.Option(
            "--open/--no-open",
            help="Open the file in the default browser when it is written.",
        ),
    ] = False,
) -> None:
    """Build the self-contained HTML dashboard.

    One file, no external assets, no network calls at view time. That constraint
    exists because the file is meant to be published and opened on a phone
    during the session, where a missing stylesheet or a blocked font request
    would leave the ranking unreadable at exactly the wrong moment. See
    `fbe.dashboard.build.build_dashboard` for the full constraint list.

    Args:
        ctx: Typer context carrying the effective config.
        asof: Point-in-time cutoff for observations.
        out: Output HTML path.
        compare: Diff baseline, ``last``, ``none`` or a path.
        open_after: Open the result in a browser.

    Raises:
        NotImplementedError: Always, until `fbe.dashboard.build` lands.

    """
    raise NotImplementedError("fbe.cli.dashboard is scaffolded, not implemented")


@journal_app.command("add")
def journal_add(
    ctx: typer.Context,
    pair: Annotated[
        str,
        typer.Argument(help="Pair in market convention, for example EURUSD."),
    ],
    direction: Annotated[
        Direction,
        typer.Option("--direction", "-d", help="Side taken on the base currency."),
    ],
    entry: Annotated[
        float,
        typer.Option("--entry", "-e", help="Fill price."),
    ],
    stop: Annotated[
        float,
        typer.Option("--stop", "-s", help="Stop price at entry."),
    ],
    exit_price: Annotated[
        float | None,
        typer.Option(
            "--exit",
            "-x",
            help="Exit price. Omit while the trade is still open.",
        ),
    ] = None,
    lots: Annotated[
        float | None,
        typer.Option("--lots", help="Size traded, in lots."),
    ] = None,
    opened_at: Annotated[
        datetime | None,
        typer.Option(
            "--opened",
            formats=DATE_FORMATS + ["%Y-%m-%d %H:%M"],
            help="When the trade was opened. Defaults to now.",
        ),
    ] = None,
    setup: Annotated[
        str | None,
        typer.Option(
            "--setup",
            help=(
                "Technical setup taken, for example 'channel-low-bounce' or "
                "'trendline-break-retest'."
            ),
        ),
    ] = None,
    followed_plan: Annotated[
        bool,
        typer.Option(
            "--followed-plan/--broke-plan",
            help=(
                "Whether the trade obeyed the plan, independent of whether it "
                "made money. These are the two axes the review scores."
            ),
        ),
    ] = True,
    note: Annotated[
        str | None,
        typer.Option("--note", "-m", help="Free text for the lesson or context."),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", "-t", help="Labels for grouping in review."),
    ] = None,
) -> None:
    """Record one trade in the journal.

    The engine's bias for the pair on that date, its conviction and the config
    digest are attached automatically. That is the point: months later the
    journal should be able to answer whether the losing trades were the ones
    taken against the engine, the ones taken against the plan, or neither.

    Args:
        ctx: Typer context carrying the effective config.
        pair: Pair in market convention.
        direction: Side taken on the base currency.
        entry: Fill price.
        stop: Stop price at entry.
        exit_price: Exit price, omitted while open.
        lots: Size traded.
        opened_at: Open timestamp, defaults to now.
        setup: Technical setup label.
        followed_plan: Whether the trade obeyed the plan.
        note: Free text.
        tag: Labels for grouping.

    Raises:
        NotImplementedError: Always, until `fbe.journal` lands.

    """
    raise NotImplementedError("fbe.cli.journal_add is scaffolded, not implemented")


@journal_app.command("review")
def journal_review(
    ctx: typer.Context,
    days: Annotated[
        int,
        typer.Option(
            "--days",
            "-d",
            help="Window to review, in days back from today.",
            min=1,
        ),
    ] = 7,
    pair: Annotated[
        list[str] | None,
        typer.Option("--pair", "-p", help="Restrict to these pairs, repeatable."),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", "-t", help="Restrict to these tags, repeatable."),
    ] = None,
    open_only: Annotated[
        bool,
        typer.Option(
            "--open-only",
            help="Show only trades with no exit recorded.",
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Rendering: table, json or csv."),
    ] = OutputFormat.TABLE,
) -> None:
    """Summarise journalled trades against the plan and the engine.

    The evening routine step. It reports the plain performance numbers, then the
    two splits that actually change behaviour: trades that followed the plan
    against trades that did not, and trades aligned with the engine's bias
    against trades taken against it. A losing week where every trade followed
    the plan and the bias is a model problem. A winning week full of trades that
    broke the plan is a discipline problem waiting to become a loss.

    Args:
        ctx: Typer context carrying the effective config.
        days: Window in days back from today.
        pair: Restrict to these pairs.
        tag: Restrict to these tags.
        open_only: Show only trades still open.
        output_format: table, json or csv.

    Raises:
        NotImplementedError: Always, until `fbe.journal` lands.

    """
    raise NotImplementedError("fbe.cli.journal_review is scaffolded, not implemented")
