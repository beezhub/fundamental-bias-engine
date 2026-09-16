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

import csv
import io
import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import httpx
import typer

from fbe import config as config_module
from fbe import report as report_module
from fbe.bias import CONVICTION_RANK, apply_filters, build_pair_biases
from fbe.datasources import ALL_SOURCES
from fbe.datasources.cache import DiskCache
from fbe.datasources.collect import (
    CollectionResult,
    SourceOutcome,
    SourceStatus,
    collect,
    lookback_start,
)
from fbe.pillars import default_pillars
from fbe.scoring import score_currencies
from fbe.types import Conviction, CurrencyScore, Direction, PairBias, PillarName
from fbe.universe import G10, MAJORS

if TYPE_CHECKING:
    from fbe.config import Config

__all__ = ["app", "journal_app", "GlobalOptions"]

EXIT_OK = 0
"""Everything worked."""

EXIT_UNUSABLE = 1
"""The command ran but its output should not be traded on."""

EXIT_BLOCKED = 3
"""A guard rule refused the request. Not an error, a decision."""

LABEL_WIDTH = 16
STATUS_WIDTH = 10
"""Column widths for the ``doctor`` verdict lines, matching the layout
published in ``docs/interfaces.md``. A continuation line leaves the label
column blank, so a check that has several things to say still reads as one
check."""

SOURCE_WIDTH = 14
"""Column width for the ``refresh`` per-source lines. Wide enough for
``forexfactory``, which is the longest source key in ``ALL_SOURCES``."""

COUNT_WIDTH = 10
"""Column width for the series and observation counts on a ``refresh`` line, so
the two numbers stay in their columns when one source returns far more than
another."""

RANK_WIDTH = 3
CODE_WIDTH = 5
COMPOSITE_WIDTH = 11
DISPERSION_WIDTH = 6
COVERAGE_WIDTH = 6
PILLAR_WIDTH = 7
"""Column widths for the ``score`` ranking, matching the layout published in
``docs/interfaces.md``. The composite field is wide because it carries an
explicit sign: a bias table where the reader has to work out which way a number
points is the one place a sign must never be implied."""

PILLAR_ABBREVIATIONS: Mapping[PillarName, str] = {
    PillarName.MONETARY: "Mon",
    PillarName.INFLATION: "Inf",
    PillarName.GROWTH: "Gro",
    PillarName.EMPLOYMENT: "Emp",
    PillarName.EXTERNAL: "Ext",
    PillarName.POSITIONING: "Pos",
    PillarName.RISK: "Rsk",
}
"""Three-letter column headings for ``score --pillars``, as published.

Spelled out rather than sliced off the enum because ``risk`` slices to ``Ris``
and the published table says ``Rsk``. A slice would also silently collide the
day two pillars share their first three letters."""

PAIR_WIDTH = 8
DIRECTION_WIDTH = 7
CONVICTION_WIDTH = 8
SPREAD_WIDTH = 8
LEG_WIDTH = 8
AGREEMENT_WIDTH = 7
"""Column widths for the ``bias`` ranked list, matching the layout published in
``docs/interfaces.md``. The spread and both legs are wide because each carries
an explicit sign, and a bias table where the reader has to work out which way a
number points is the one place a sign must never be implied."""

NO_BLOCKERS = "-"
"""What the Notes column prints when a pair carries nothing at all.

Distinct from `ABSENT_CELL` on purpose. ``n/a`` says a number could not be
formed; this says the list of reasons to stand aside is genuinely empty, which
is a finding rather than a gap. An empty cell would say neither and would read
as a column the renderer forgot."""

ABSENT_CELL = "n/a"
"""What a pillar with no data prints, in the table and in the machine formats.

`BasePillar.missing_score` gives an absent pillar a score of ``0.0`` with
``raw`` and ``z`` both ``None``, so the number is there to print and means the
opposite of what it looks like. ``n/a`` is the marker
``src/fbe/templates/report.md.j2`` already uses for the same distinction."""

COVERAGE_EPSILON = 1e-9
"""Absorbs binary representation when coverage is turned into a percentage.

Coverage is floored rather than rounded, so a run at 99.6% never prints as
complete: the column exists to say that part of the pillar weight had no data,
and the one value a reader treats as needing no further thought is 100%.
Flooring alone would understate instead, because ``0.29 * 100`` is
``28.999999999999996``, so the product is nudged by less than any real coverage
difference before the floor."""

MINUTES_PER_HOUR = 60

KEY_PREFIX_LENGTH = 4
"""Characters of a credential ``--show-keys`` prints. Enough to tell which key
is loaded, not enough to use one."""

CREDENTIAL_FIELDS: tuple[tuple[str, str], ...] = (("FRED_API_KEY", "fred_api_key"),)
"""Credentials ``doctor`` reports on, as the name the vendor documents paired
with the `fbe.config.DataConfig` field holding it. A source that gains a
credential adds a row here, so that an absent key is named rather than showing
up later as an unexplained coverage gap."""


class CheckStatus(StrEnum):
    """One verdict in the ``doctor`` output.

    ``OK`` is nothing to do. ``WARN`` is usable but worth knowing, such as a
    cache past its TTL. ``FAIL`` is a run whose output should not be traded on.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CheckLine:
    """One printed line of the ``doctor`` report.

    Attributes:
        label: Check name, or the empty string for a continuation line under
            the check above it.
        status: This line's verdict.
        detail: What was found, in plain words.

    """

    label: str
    status: CheckStatus
    detail: str

    def render(self) -> str:
        """Return the line in the published column layout."""
        return (
            f"{self.label:<{LABEL_WIDTH}}"
            f"{self.status.value:<{STATUS_WIDTH}}"
            f"{self.detail}"
        )


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    """The global flags, parked on the Typer context for every command.

    Attributes:
        config_path: Value of ``--config``, or ``None`` for the default lookup.
        offline: Value of ``--offline``.
        verbose: Repeat count of ``-v``.

    """

    config_path: Path | None = None
    offline: bool = False
    verbose: int = 0


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
            "--offline",
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
    """Capture the global flags onto the Typer context.

    Only capture happens here. Loading the config is deferred to
    `_effective_config` so that ``fbe <command> --help`` stays usable when the
    config file is broken or missing: a tool you cannot ask for help is a poor
    tool to debug with.

    Args:
        ctx: Typer context. The parsed flags are attached to ``ctx.obj``.
        config: Optional path to a config YAML overriding the defaults.
        offline: When true, force `fbe.config.DataConfig.offline`.
        verbose: Repeat count for ``-v``, mapped to a logging level.

    """
    ctx.obj = GlobalOptions(config_path=config, offline=offline, verbose=verbose)


_CONFIG_META_KEY = "fbe.cli.effective_config"
"""Where the resolved config is parked on the context.

``Context.meta`` is shared with every nested context, so the group callback
and each command see the same entry. ``ctx.obj`` would not do: it holds a
frozen `GlobalOptions`, and the point is one config per invocation rather than
one per command.
"""


def _effective_config(ctx: typer.Context) -> Config:
    """Resolve the effective config once per run and cache it on the context.

    Every command reads its config through here rather than loading it again,
    so one invocation of the CLI has exactly one config digest. Commands that
    write a report record that digest, which is what makes an old report
    reproducible.

    ``--offline`` is applied here and is one-way, as `main` documents it:
    passing it forces `fbe.config.DataConfig.offline` true, and omitting it
    leaves whatever the file or the defaults said. There is deliberately no
    way to force online from the command line, because the flag exists to make
    a run reproducible and a flag that can undo that is a flag that will.

    This resolves and does not refuse. A config that `fbe.config.Config.validate`
    would reject still loads, because ``fbe doctor`` reports those problems as
    its first check and a resolver that raised would hide the very thing the
    operator ran ``doctor`` to see.

    Args:
        ctx: Typer context carrying a `GlobalOptions` in ``ctx.obj``.

    Returns:
        The effective `fbe.config.Config`, with ``--offline`` applied. The same
        object on every call within one invocation, not an equal copy.

    Raises:
        RuntimeError: When the context carries no `GlobalOptions`, which means
            the group callback did not run. Falling back to the defaults here
            would silently ignore ``--config`` and ``--offline``.
        fbe.config.ConfigError: When the config file or an ``FBE_`` variable
            cannot be read. Raised from `fbe.config.load_config`, and left to
            reach the operator rather than being turned into a default.

    """
    cached = ctx.meta.get(_CONFIG_META_KEY)
    if isinstance(cached, config_module.Config):
        return cached
    options = ctx.obj
    if not isinstance(options, GlobalOptions):
        raise RuntimeError(
            "the global options are missing from the context, so --config and "
            "--offline would be ignored"
        )
    resolved = config_module.load_config(options.config_path)
    if options.offline:
        resolved = replace(resolved, data=replace(resolved.data, offline=True))
    ctx.meta[_CONFIG_META_KEY] = resolved
    return resolved


def _check_config(config: Config) -> list[CheckLine]:
    """Report every `fbe.config.Config.validate` problem.

    Args:
        config: The effective config.

    Returns:
        One ``ok`` line, or one ``fail`` line per problem. Every problem is
        printed rather than only the first, because they are independent and an
        operator fixing them one run at a time is an operator running this five
        times.

    """
    problems = config.validate()
    if not problems:
        weights = sum(config.scoring.weights.values())
        # risk_per_trade_max is a fraction of account balance, 0.02 for the
        # plan's 2%. The published layout prints a percentage, so it is scaled
        # here and nowhere else.
        cap_pct = config.risk.risk_per_trade_max * 100
        return [
            CheckLine(
                "config",
                CheckStatus.OK,
                f"weights sum to {weights:.3f}, risk cap {cap_pct:.1f}%",
            )
        ]
    return [
        CheckLine("config" if index == 0 else "", CheckStatus.FAIL, problem)
        for index, problem in enumerate(problems)
    ]


def _check_credentials(config: Config, show_keys: bool) -> list[CheckLine]:
    """Say which credentials are configured, without printing one.

    Args:
        config: The effective config.
        show_keys: Print the first `KEY_PREFIX_LENGTH` characters of each key
            that is present.

    Returns:
        One line per credential in `CREDENTIAL_FIELDS`. An absent key is a
        warning rather than a failure: the source it belongs to becomes a
        recorded coverage gap, and an offline run needs no key at all.

    """
    lines: list[CheckLine] = []
    for index, (name, field_name) in enumerate(CREDENTIAL_FIELDS):
        # Direct access, not getattr with a default: a renamed field must be a
        # loud contract break, not a key reported absent on a machine that has
        # it configured.
        value = getattr(config.data, field_name)
        if value:
            shown = f" ({value[:KEY_PREFIX_LENGTH]}...)" if show_keys else ""
            lines.append(
                CheckLine(
                    "credentials" if index == 0 else "",
                    CheckStatus.OK,
                    f"{name} present{shown}",
                )
            )
        else:
            lines.append(
                CheckLine(
                    "credentials" if index == 0 else "",
                    CheckStatus.WARN,
                    f"{name} absent: that source will report as unavailable",
                )
            )
    return lines


def _check_cache(config: Config) -> list[CheckLine]:
    """Report cache writability, entry count and the age of the oldest entry.

    Args:
        config: The effective config.

    Returns:
        A ``fail`` line when the cache directory cannot be written, since
        nothing can be fetched into it. Testing that writes and removes one
        empty probe file, and creates the directory if it is absent, which is
        the one thing this command does to the disk.

        Otherwise one line reporting the entry count and the oldest age in
        completed hours against ``DataConfig.cache_ttl_hours``, warning when
        the oldest is past it. An
        empty cache is reported as zero entries rather than omitted: a run with
        nothing cached is exactly what an operator is often looking for.

    """
    directory = config.data.cache_dir
    ttl = config.data.cache_ttl_hours
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".doctor-write-probe"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        return [
            CheckLine(
                "cache",
                CheckStatus.FAIL,
                f"{directory} is not writable: {error}",
            )
        ]

    stats = DiskCache(config.data).stats()
    entries = int(sum(source["entries"] for source in stats.values()))
    ages = [
        source["oldest_hours"] for source in stats.values() if "oldest_hours" in source
    ]
    unreadable = int(sum(source.get("unreadable", 0.0) for source in stats.values()))

    if not ages:
        detail = f"{entries} entries (ttl {ttl}h)"
        status = CheckStatus.OK
    else:
        oldest = max(ages)
        # Rounded up, and the verdict below compares the exact figure. Up
        # rather than down because this is a staleness number: a cache printed
        # as younger than it is, is the false comfort that sends an operator
        # looking at the model instead of the data. Rounding to nearest, or
        # down, also prints the same figure either side of the TTL, so two
        # lines showing "12h" would carry opposite verdicts.
        detail = f"{entries} entries, oldest {math.ceil(oldest)}h (ttl {ttl}h)"
        if oldest > ttl:
            detail += ": run fbe refresh"
            status = CheckStatus.WARN
        else:
            status = CheckStatus.OK

    lines = [CheckLine("cache", status, detail)]
    if unreadable:
        lines.append(
            CheckLine(
                "",
                CheckStatus.WARN,
                f"{unreadable} unreadable entries: clear the cache to refetch",
            )
        )
    return lines


def _probe(source: object, timeout: float) -> tuple[CheckStatus, str]:
    """Make one cheap request to a source, to see whether it answers.

    Args:
        source: An instantiated source carrying a ``base_url``.
        timeout: Seconds to wait, applied to the request itself.

    Returns:
        The verdict and the detail to print. A probe that fails is a warning
        rather than a failure: one dead source is a recorded coverage gap, not
        a reason to refuse the whole run.

    """
    name = getattr(source, "name", "unknown")
    url = getattr(source, "base_url", "")
    if not url:
        return CheckStatus.WARN, f"{name} names no base URL to probe"

    # httpx logs the request line at INFO with the full URL, and a source is
    # free to carry a credential in its base URL. `BaseDataSource` pins this
    # when it builds its own client; this call does not go through that client.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    started = time.monotonic()
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.TimeoutException:
        return CheckStatus.WARN, f"{name} unreachable (timeout after {timeout}s)"
    except Exception as error:  # noqa: BLE001
        # Not just httpx.HTTPError: httpx.InvalidURL is not one of those, and a
        # misconfigured base URL must be a reported warning rather than a
        # traceback out of the command that exists to diagnose it.
        return CheckStatus.WARN, f"{name} unreachable ({type(error).__name__})"

    elapsed = (time.monotonic() - started) * 1000
    if response.is_success:
        return CheckStatus.OK, f"{name} {elapsed:.0f}ms"
    if response.status_code in (401, 403):
        # The commonest real failure, and it is a credential problem wearing a
        # network problem's clothes. Saying so is the whole point of doctor.
        return (
            CheckStatus.WARN,
            f"{name} refused the request (HTTP {response.status_code}): "
            "check the credential, not the network",
        )
    return CheckStatus.WARN, f"{name} answered HTTP {response.status_code}"


def _check_sources(config: Config, timeout: float) -> list[CheckLine]:
    """Report availability for every source, then probe the usable ones.

    Args:
        config: The effective config.
        timeout: Seconds per probe.

    Returns:
        One comma-joined line naming every source that answered, then one line
        per source that did not, which is the layout the worked example in
        ``docs/interfaces.md`` prints. A source whose ``available`` is still
        scaffolded is reported as unavailable, which is the honest answer and
        is why this command can land before the sources do.

        A ``fail`` line when every source that was actually probed failed,
        since ``docs/interfaces.md`` counts that among the conditions for exit
        1. Every source being scaffolded is not that and stays a warning:
        nothing failed, the phase has not landed yet.

        Nothing here raises. A dead source is what the operator ran this to
        find out about.

    """
    reached: list[str] = []
    problems: list[tuple[CheckStatus, str]] = []
    probed = 0

    for source_class in ALL_SOURCES:
        name = getattr(source_class, "name", source_class.__name__)
        try:
            source = source_class(config.data)
        except Exception as error:  # noqa: BLE001
            # A constructor that raises is a programming error rather than an
            # operational one, so it fails rather than warns, and it says what
            # happened instead of only naming the exception type.
            problems.append(
                (
                    CheckStatus.FAIL,
                    f"{name} could not be constructed: {type(error).__name__}: {error}",
                )
            )
            continue

        try:
            usable = source.available()
        except NotImplementedError:
            problems.append((CheckStatus.WARN, f"{name} scaffolded, not yet built"))
            continue
        except Exception as error:  # noqa: BLE001
            problems.append(
                (
                    CheckStatus.WARN,
                    f"{name} could not report availability: "
                    f"{type(error).__name__}: {error}",
                )
            )
            continue

        if not usable:
            problems.append((CheckStatus.WARN, f"{name} unavailable, not configured"))
            continue
        if config.data.offline:
            reached.append(f"{name} available, offline so no probe")
            continue

        probed += 1
        status, detail = _probe(source, timeout)
        if status is CheckStatus.OK:
            reached.append(detail)
        else:
            problems.append((status, detail))

    lines: list[CheckLine] = []
    if reached:
        lines.append(CheckLine("sources", CheckStatus.OK, ", ".join(reached)))
    if probed and not reached:
        lines.append(
            CheckLine(
                "sources" if not lines else "",
                CheckStatus.FAIL,
                f"all {probed} probed sources failed, so nothing can be fetched",
            )
        )
    for status, detail in problems:
        lines.append(CheckLine("sources" if not lines else "", status, detail))
    if not lines:
        lines.append(
            CheckLine("sources", CheckStatus.WARN, "no sources are registered")
        )
    return lines


def _check_reports(config: Config) -> list[CheckLine]:
    """Say whether a previous report exists and whether its digest still matches.

    Reads only the ``config_digest`` out of the newest JSON sidecar rather than
    reconstructing a report through `fbe.report.load_report`, which is
    scaffolded until Phase 5. The filename convention comes from
    `fbe.report.SIDECAR_GLOB` so it is not restated here. The two keys this
    reads, ``asof`` and ``config_digest``, are the only shape it assumes of the
    sidecar, and Phase 5 should keep both at the top level or update this.

    Args:
        config: The effective config.

    Returns:
        An ``ok`` line saying there is nothing to diff against when the
        directory is empty, which is the normal state of a fresh checkout. A
        digest that no longer matches is a warning, because the weights have
        moved since and the two runs are not comparable.

    """
    directory = config.data.reports_dir
    if directory.exists() and not directory.is_dir():
        return [
            CheckLine(
                "reports",
                CheckStatus.FAIL,
                f"{directory} is not a directory, so no report can be written",
            )
        ]
    sidecars = sorted(directory.glob(report_module.SIDECAR_GLOB))
    if not sidecars:
        return [CheckLine("reports", CheckStatus.OK, "no previous report to diff")]

    newest = sidecars[-1]
    try:
        payload = json.loads(newest.read_text(encoding="utf-8"))
        recorded = str(payload["config_digest"])
    except (OSError, ValueError, TypeError, KeyError) as error:
        return [
            CheckLine(
                "reports",
                CheckStatus.WARN,
                f"{newest.name} could not be read ({type(error).__name__})",
            )
        ]

    asof = str(payload.get("asof", newest.stem))
    if recorded == config.digest():
        return [
            CheckLine(
                "reports", CheckStatus.OK, f"last report {asof}, config digest matches"
            )
        ]
    return [
        CheckLine(
            "reports",
            CheckStatus.WARN,
            f"last report {asof} used digest {recorded}, not comparable",
        )
    ]


@app.command(
    help=(
        "Check config, credentials, cache and source reachability. Run this "
        "first when the output looks wrong: it answers the four questions "
        "behind almost every bad run, in order, and prints a verdict per "
        "check."
    ),
)
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
        typer.Exit: With `EXIT_UNUSABLE` when a check fails outright, or when a
            check warns and ``--strict`` is set. A warning on its own exits
            `EXIT_OK`, because a stale cache or an unconfigured source is a run
            worth looking at rather than a run that could not happen.

    """
    failures = 0
    warnings = 0

    def emit(lines: Sequence[CheckLine]) -> None:
        """Print one check's lines now and count their verdicts.

        Printed as each check finishes rather than collected and printed at the
        end, so a check that raises still leaves the verdicts already known on
        the screen. This is the command an operator runs when everything else
        is broken, and it has to be the last thing to withhold what it knows.
        """
        nonlocal failures, warnings
        for line in lines:
            typer.echo(line.render())
            if line.status is CheckStatus.FAIL:
                failures += 1
            elif line.status is CheckStatus.WARN:
                warnings += 1

    try:
        config = _effective_config(ctx)
    except config_module.ConfigError as error:
        # An unknown key or an unreadable file is the commonest config mistake
        # there is, and it is precisely what doctor is run to find. Letting it
        # reach the operator as a traceback, from the one command meant to be
        # usable when nothing else is, would be the worst moment for it.
        #
        # This is the one case where the later checks do not run. They each
        # need a setting the file was supposed to supply, and checking a cache
        # directory the operator did not choose would report on somewhere they
        # are not looking. The `validate` path below is different: that config
        # loaded, so every other check has real settings to work from.
        emit([CheckLine("config", CheckStatus.FAIL, str(error))])
        emit(
            [
                CheckLine(
                    "",
                    CheckStatus.FAIL,
                    "no other check can run until the config file loads",
                )
            ]
        )
        typer.echo(_summarise(failures, warnings))
        raise typer.Exit(EXIT_UNUSABLE) from error

    emit(_check_config(config))
    emit(_check_credentials(config, show_keys))
    emit(_check_cache(config))
    emit(_check_sources(config, timeout))
    emit(_check_reports(config))

    typer.echo(_summarise(failures, warnings))

    if failures or (warnings and strict):
        raise typer.Exit(EXIT_UNUSABLE)


def _summarise(failures: int, warnings: int) -> str:
    """Return the closing line counting what was found.

    Args:
        failures: Lines at `CheckStatus.FAIL`.
        warnings: Lines at `CheckStatus.WARN`.

    Returns:
        A count, and nothing about the quality of the engine's results.
        ``doctor`` reports plumbing, and this is the line an operator reads
        first when they already doubt the output.

    """
    if not failures and not warnings:
        return "All checks passed."
    parts = []
    if failures:
        parts.append(f"{failures} failure{'s' if failures != 1 else ''}")
    if warnings:
        parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    return ", ".join(parts) + "."


@app.command(
    help=(
        "Pull fresh data from the sources into the local cache. Kept separate "
        "from scoring, so a failed fetch at 07:00 leaves yesterday's cache "
        "intact and still scoreable."
    ),
)
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
        typer.BadParameter: With exit code 2 when ``--source`` names a source
            that does not exist, when ``--since`` is in the future, or when
            ``--force`` is combined with a global ``--offline``. Each would
            otherwise produce an empty run that looked like a successful one.
        typer.Exit: With `EXIT_UNUSABLE` when the run reconciled no
            observations at all, which is every source failing and coverage
            collapsing both, per ``docs/interfaces.md``.

    """
    config = _effective_config(ctx)

    if force and config.data.offline:
        raise typer.BadParameter(
            "--force drops the cached copies so they can be fetched again, and "
            "an offline run cannot fetch them. Together they would empty the "
            "cache and refill none of it. Drop one of the two.",
            param_hint="--force",
        )

    today = date.today()
    try:
        start = (
            since.date()
            if since is not None
            else lookback_start(today, config.scoring.lookback_years)
        )
    except ValueError as error:
        # A negative lookback is a config mistake, and nothing validates it.
        # Reaching the operator as a traceback out of the first command of the
        # morning would be the worst moment for it.
        raise typer.BadParameter(
            f"scoring.lookback_years is unusable: {error}",
            param_hint="--since",
        ) from error
    if start > today:
        raise typer.BadParameter(
            f"--since {start} is in the future, so no source could return "
            "anything for the window",
            param_hint="--since",
        )

    try:
        result = collect(
            config.data,
            start=start,
            end=today,
            sources=ALL_SOURCES,
            selected=source,
            force=force,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--source") from error

    _render_refresh(result, config, today)

    if not result.usable:
        raise typer.Exit(EXIT_UNUSABLE)


def _render_refresh(result: CollectionResult, config: Config, asof: date) -> None:
    """Print what a collection did, in the layout ``docs/interfaces.md`` shows.

    Args:
        result: What the collection produced.
        config: The effective config, read for the cache directory only.
        asof: The date the coverage gaps were aged against.

    """
    for outcome in result.outcomes:
        typer.echo(_render_outcome(outcome))
    for line in _render_gaps(result.gaps, asof):
        typer.echo(line)
    typer.echo(_render_cache(config))


def _render_outcome(outcome: SourceOutcome) -> str:
    """Return one source's line.

    Args:
        outcome: What that source did.

    Returns:
        For a completed source, its series and observation counts and the time
        inside its fetch. For anything else, the word and the reason, because a
        source that was skipped and a source that returned nothing are
        different facts and an operator chasing a missing currency needs to
        know which one they have.

    """
    label = outcome.source.ljust(SOURCE_WIDTH)
    if outcome.status is SourceStatus.COMPLETED:
        series = f"{outcome.series:,} series".ljust(COUNT_WIDTH + 7)
        observations = f"{outcome.observations:,} observations".ljust(COUNT_WIDTH + 13)
        return f"{label}{series}{observations}{outcome.elapsed_seconds:.1f}s"
    return f"{label}{outcome.status.value} ({outcome.detail})"


def _render_gaps(gaps: Mapping[str, tuple[str, ...]], asof: date) -> list[str]:
    """Return the coverage gap block.

    Args:
        gaps: Indicator key to the currencies with no usable ref, exactly as
            `fbe.datasources.registry.stale_refs` returned it.
        asof: The date the refs were aged against.

    Returns:
        One heading line and one line per indicator, every indicator the
        registry reported and not a sample of them. An empty mapping still
        prints its heading: a silent gap block and a healthy registry would
        otherwise look identical, and that is the difference this command
        exists to show.

    """
    if not gaps:
        return [f"Coverage: no gaps, aged at {asof}."]
    lines = [f"Coverage gaps, aged at {asof}:"]
    for indicator, currencies in sorted(gaps.items()):
        lines.append(f"  {indicator.ljust(SOURCE_WIDTH + 8)}{', '.join(currencies)}")
    return lines


def _render_cache(config: Config) -> str:
    """Return the closing cache summary line.

    Args:
        config: The effective config, read for the cache directory.

    Returns:
        The total entry count and the age of the newest entry across every
        source. A cache holding nothing says so rather than reporting zero
        entries newest zero minutes old, which reads as just fetched.

    """
    stats = DiskCache(config.data).stats()
    entries = int(sum(source["entries"] for source in stats.values()))
    ages = [
        source["newest_hours"] for source in stats.values() if "newest_hours" in source
    ]
    if not entries or not ages:
        return "Cache: empty."
    newest = min(ages)
    age = f"{newest * MINUTES_PER_HOUR:.0f}m" if newest < 1.0 else f"{newest:.1f}h"
    return f"Cache: {entries:,} entries, newest {age} old."


@app.command(
    help=(
        "Rank the G10 currencies by composite score, strongest first, with "
        "dispersion across pillars and data coverage. Add --pillars for the "
        "seven scores behind each composite."
    ),
)
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

    Every printed number is read from a `fbe.types.CurrencyScore` field and
    none is computed here. Composites are on the ``-3`` to ``+3`` band and
    carry an explicit sign, positive meaning fundamentally strong relative to
    the rest of the universe; coverage is a whole percentage, floored. A pillar
    with no usable data prints ``n/a`` rather than its score of ``0.0``, since
    that number means no opinion was formed rather than an opinion of neutral,
    and a currency with no usable data anywhere prints its row with 0% and the
    reasons, then exits `EXIT_UNUSABLE`.

    Raises:
        typer.BadParameter: With exit code 2 when ``--currency`` names a code
            outside `fbe.universe.G10`, when ``--asof`` is in the future, or
            when ``scoring.lookback_years`` cannot produce a window. Each would
            otherwise print a table that looked like a run with no opinions.
        typer.Exit: With `EXIT_UNUSABLE` when the cache held nothing for the
            window, and when every currency came back at zero coverage, which
            is the coverage-collapsed case ``docs/interfaces.md`` gives for
            exit 1.

    """
    config = _effective_config(ctx)
    run_date = asof.date() if asof is not None else date.today()
    if run_date > date.today():
        raise typer.BadParameter(
            f"--asof {run_date} is in the future, so every series would be "
            "past its allowance and every currency would score zero on no "
            "data",
            param_hint="--asof",
        )
    wanted = _requested_currencies(currency)

    try:
        start = lookback_start(run_date, config.scoring.lookback_years)
    except ValueError as error:
        # A negative lookback is a config mistake and nothing validates it.
        # `refresh` guards the same call for the same reason; this command is
        # run several times a day, so a traceback here costs more.
        raise typer.BadParameter(
            f"scoring.lookback_years is unusable: {error}",
            param_hint="--asof",
        ) from error

    # Scoring reads the cache and never the network, which is the separation
    # `docs/interfaces.md` opens with: `refresh` is the only command allowed to
    # be slow or to fail on a connection. Passing the config through unchanged
    # would let a rolled-over TTL refetch mid-session, so two runs at the same
    # --asof and the same digest could print different tables with nothing on
    # screen to explain it.
    result = collect(
        replace(config.data, offline=True),
        start=start,
        end=run_date,
        sources=ALL_SOURCES,
    )
    if not result.usable:
        typer.echo(
            "No observations in the cache for this window, so there is nothing "
            "to score. Run fbe refresh to fill it, or fbe doctor to find out "
            "why it is empty."
        )
        raise typer.Exit(EXIT_UNUSABLE)

    scores = score_currencies(
        result.observations,
        default_pillars(config.scoring),
        config.scoring,
        run_date,
    )
    rows = _score_rows(scores, wanted)
    order = _pillar_order(config.scoring) if pillars else None

    if output_format is OutputFormat.JSON:
        typer.echo(_score_json(rows, order, run_date, config.digest()))
    elif output_format is OutputFormat.CSV:
        typer.echo(_score_csv(rows, order, run_date, config.digest()))
        for note in _score_notes(rows):
            # CSV has nowhere to put a run-level warning, and dropping it would
            # leave a run on six pillars looking like a run on seven. It goes
            # to stderr so `fbe score --format csv > monday.csv` still writes a
            # file a parser can read.
            typer.echo(note, err=True)
    else:
        _render_score(rows, order, run_date, config.digest())

    if _coverage_collapsed(scores):
        # Every currency scored on nothing. The rows still print, with 0% and
        # the reasons, because the issue asks for the absence to be visible
        # rather than hidden. The exit code is what a script reads, and
        # `docs/interfaces.md` reserves 1 for coverage collapsing.
        raise typer.Exit(EXIT_UNUSABLE)


def _coverage_collapsed(scores: Sequence[CurrencyScore]) -> bool:
    """Whether the run scored nothing at all.

    Args:
        scores: Every currency the scorer returned, before any row filter.

    Returns:
        True when no currency held any usable pillar weight. Judged on the
        whole run rather than on the printed rows: ``--currency JPY`` on a
        currency with no data is a thin currency in a working run, and the run
        is what the exit code describes.

    """
    return bool(scores) and all(row.coverage <= 0.0 for row in scores)


def _requested_currencies(currency: Sequence[str] | None) -> tuple[str, ...] | None:
    """Normalise ``--currency`` and refuse a code outside the universe.

    Args:
        currency: The repeated option, or ``None`` for every currency.

    Returns:
        Upper-cased codes in the order given, or ``None`` when no filter was
        asked for.

    Raises:
        typer.BadParameter: When a code is not in `fbe.universe.G10`. A filter
            that matches nothing would print an empty table and exit zero,
            which is indistinguishable from a run that scored the universe and
            found no opinions worth printing.

    """
    if not currency:
        return None
    wanted = tuple(code.strip().upper() for code in currency)
    unknown = [code for code in wanted if code not in G10]
    if unknown:
        raise typer.BadParameter(
            f"{', '.join(unknown)} is not in the scored universe. "
            f"This engine scores {', '.join(G10)}.",
            param_hint="--currency",
        )
    return wanted


def _score_rows(
    scores: Sequence[CurrencyScore], wanted: Sequence[str] | None
) -> tuple[CurrencyScore, ...]:
    """Order the rows strongest first and apply the row filter.

    Args:
        scores: What the scorer returned.
        wanted: Codes to keep, or ``None`` for all of them.

    Returns:
        The rows to print, ordered by `CurrencyScore.rank`, which is strongest
        first because that is how the scorer assigns it.

        Ordered on the rank rather than by re-deriving "composite descending,
        ISO code breaking a tie". That rule already lives in
        `fbe.scoring.score_currencies`, and a second copy of it here can
        disagree with the first: change the tie-break upstream and this table
        would print rank 3 above rank 2, with the rank column contradicting the
        row order and nothing saying which is right. A row with no rank sorts
        last, since there is nothing to place it by.

    """
    kept = [row for row in scores if wanted is None or row.currency in wanted]
    kept.sort(key=lambda row: (row.rank is None, row.rank or 0, row.currency))
    return tuple(kept)


def _pillar_order(config: config_module.ScoringConfig) -> tuple[PillarName, ...]:
    """Return the pillars in configured-weight order, heaviest first.

    Args:
        config: The run's scoring configuration.

    Returns:
        Every pillar, sorted by weight descending. Ties keep `PillarName`'s own
        declaration order, which runs from the fastest and heaviest driver to
        the slowest, so the four pillars sharing 0.10 stay in the order the
        published example prints them. That comes from `sorted` being stable
        over a list already in declaration order, not from a second sort key: a
        tiebreak on the index was written here first and could not change the
        result for any weight map, which is a line that reads as load-bearing
        and is not.

        A weight missing from the map raises rather than parking that pillar
        last. `fbe.scoring.score_currencies` would raise on the same lookup, so
        a quiet default here would only move where the operator meets it.

    """
    return tuple(sorted(PillarName, key=lambda name: -config.weights[name]))


def _pillar_cell(score: CurrencyScore, name: PillarName) -> float | None:
    """Return one pillar's score for one currency, or ``None`` if it has none.

    Args:
        score: The currency's row.
        name: Which pillar's column.

    Returns:
        `PillarScore.score` when the pillar scored this currency, and ``None``
        when it did not. Absence is read from ``z``, not from the score: an
        unscored pillar carries ``0.0`` and that number means no opinion was
        formed rather than an opinion of neutral. A pillar missing from the
        mapping entirely, which is how a pillar switched off for the run
        arrives, is the same answer.

    """
    found = score.pillars.get(name)
    if found is None or found.z is None:
        return None
    return found.score


def _share_percent(share: float) -> int:
    """Return a ``0.0`` to ``1.0`` share as a whole percentage, never rounded up.

    Args:
        share: The fraction to print. Coverage, the share of pillar weight
            that had usable data, and agreement, the share of pillar weight
            pointing the way the headline does. Both run ``0.0`` to ``1.0``
            and both flatter the run when rounded up, so both floor.

    Returns:
        The percentage, floored. See `COVERAGE_EPSILON` for why the floor is
        nudged and why it is a floor at all.

    One function for both columns rather than the same expression twice. The
    knowledge here is the flooring rule and the epsilon, and two copies of a
    rounding rule disagree eventually with nothing on screen to say which
    table is which.

    """
    return math.floor(share * 100.0 + COVERAGE_EPSILON)


def _score_notes(rows: Sequence[CurrencyScore]) -> tuple[str, ...]:
    """Collect the reasons pillars could not score, in first-seen order.

    Args:
        rows: The rows being printed.

    Returns:
        Each distinct non-empty note, once, from every pillar on every printed
        row.

        Every pillar and not only the ones that could not score.
        `fbe.scoring.score_currencies` catches a pillar that raises and records
        why on each currency's copy of that score, so the same sentence arrives
        eight times and is worth printing once; dropping it would leave a run
        on six pillars looking like a run on seven, with only the coverage
        column hinting at it. But `BasePillar.compute` also puts two things on
        the notes of pillars that *did* score, which its docstring says must
        reach a reader on every run: which path `blend_divisor` took, and how
        many inputs were admitted on an assumed publication lag rather than a
        real release date. Filtering on absence would drop both, and the
        divisor path is the one fact that says whether two runs are on the same
        scale.

    """
    seen: list[str] = []
    for row in rows:
        for score in row.pillars.values():
            if score.notes and score.notes not in seen:
                seen.append(score.notes)
    return tuple(seen)


def _run_header(asof: date, digest: str) -> str:
    """Return the line that ties a printed table to the run that made it.

    Shared by ``score`` and ``bias``, which publish the same line in
    ``docs/interfaces.md``. Two copies of it could drift into two different
    ideas of which run a table came from, which is the one question the line
    exists to answer.
    """
    return f"asof {asof.isoformat()}   config {digest}"


def _score_columns(order: Sequence[PillarName] | None) -> str:
    """Return the column headings, with the pillar columns when asked for."""
    line = (
        f"{'#':>{RANK_WIDTH}}"
        f"{'CCY':>{CODE_WIDTH}}"
        f"{'Composite':>{COMPOSITE_WIDTH}}"
        f"{'Disp':>{DISPERSION_WIDTH}}"
        f"{'Cov':>{COVERAGE_WIDTH}}"
    )
    if order is None:
        return line
    return line + "".join(
        f"{PILLAR_ABBREVIATIONS[name]:>{PILLAR_WIDTH}}" for name in order
    )


def _score_line(score: CurrencyScore, order: Sequence[PillarName] | None) -> str:
    """Return one currency's row in the published layout.

    Args:
        score: The currency's row.
        order: Pillar columns to append, or ``None`` for the ranking only.

    Returns:
        The rank, code, composite, dispersion and coverage, every one of them
        read off the dataclass. The rank is `CurrencyScore.rank` and not the
        row's position, because the scores are cross-sectional and a narrowed
        list still ranks against the whole universe. A row carrying no rank
        prints `ABSENT_CELL`: ``0`` would be a plausible rank and would sort
        above first place, which is the quiet default this repository refuses
        everywhere else.

    """
    rank = ABSENT_CELL if score.rank is None else str(score.rank)
    line = (
        f"{rank:>{RANK_WIDTH}}"
        f"{score.currency:>{CODE_WIDTH}}"
        f"{score.composite:>+{COMPOSITE_WIDTH}.2f}"
        f"{score.dispersion:>{DISPERSION_WIDTH}.2f}"
        f"{str(_share_percent(score.coverage)) + '%':>{COVERAGE_WIDTH}}"
    )
    if order is None:
        return line
    for name in order:
        value = _pillar_cell(score, name)
        cell = ABSENT_CELL if value is None else f"{value:+.2f}"
        line += f"{cell:>{PILLAR_WIDTH}}"
    return line


def _render_score(
    rows: Sequence[CurrencyScore],
    order: Sequence[PillarName] | None,
    asof: date,
    digest: str,
) -> None:
    """Print the ranking in the layout ``docs/interfaces.md`` publishes."""
    typer.echo(_run_header(asof, digest))
    typer.echo("")
    typer.echo(_score_columns(order))
    for score in rows:
        typer.echo(_score_line(score, order))
    notes = _score_notes(rows)
    if notes:
        typer.echo("")
        for note in notes:
            typer.echo(note)


def _score_payload(
    rows: Sequence[CurrencyScore], order: Sequence[PillarName] | None
) -> list[dict[str, object]]:
    """Return the rows as plain data, shared by both machine formats.

    One builder for both, so a formatter cannot round the table and not the
    JSON, or carry a field in one and drop it in the other.
    """
    payload: list[dict[str, object]] = []
    for score in rows:
        row: dict[str, object] = {
            "rank": score.rank,
            "currency": score.currency,
            "composite": score.composite,
            "dispersion": score.dispersion,
            "coverage": score.coverage,
        }
        if order is not None:
            row["pillars"] = {name.value: _pillar_cell(score, name) for name in order}
        payload.append(row)
    return payload


def _score_json(
    rows: Sequence[CurrencyScore],
    order: Sequence[PillarName] | None,
    asof: date,
    digest: str,
) -> str:
    """Return the ranking as JSON, carrying the run's own identifiers."""
    return json.dumps(
        {
            "asof": asof.isoformat(),
            "config_digest": digest,
            "currencies": _score_payload(rows, order),
            "warnings": list(_score_notes(rows)),
        },
        indent=2,
    )


def _score_csv(
    rows: Sequence[CurrencyScore],
    order: Sequence[PillarName] | None,
    asof: date,
    digest: str,
) -> str:
    """Return the ranking as CSV, with one column per pillar when asked for.

    Args:
        rows: The rows to write.
        order: Pillar columns to append, or ``None`` for the ranking only.
        asof: The run's date.
        digest: The run's config digest.

    Returns:
        The rows, each carrying ``asof`` and ``config_digest`` as columns.
        Those two are run-level facts and repeat on every line, which is how a
        flat format states one. They are carried because a saved CSV is the one
        rendering that outlives the terminal it was printed in, and a file of
        composites that cannot be tied to the weights that produced it is a
        file nobody can check later.

        An absent pillar is an empty field rather than a zero, the same
        distinction the table draws with ``n/a``. A spreadsheet reading ``0``
        for a pillar that never ran would average it in.

    Warnings are not here. CSV has no row-level place for a run-level sentence,
    so the caller sends them to stderr, which keeps a redirected file parseable
    while still putting the reason in front of the person who ran it.

    """
    columns = ["rank", "currency", "composite", "dispersion", "coverage"]
    if order is not None:
        columns += [name.value for name in order]
    columns += ["asof", "config_digest"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in _score_payload(rows, order):
        flat: dict[str, object] = {key: row[key] for key in columns if key in row}
        for name in order or ():
            value = row["pillars"][name.value]  # type: ignore[index]
            flat[name.value] = "" if value is None else value
        flat["asof"] = asof.isoformat()
        flat["config_digest"] = digest
        writer.writerow(flat)
    return buffer.getvalue().rstrip("\n")


@app.command(
    help=(
        "Show the pair matrix and the ranked directional calls. A pair bias "
        "is the difference between two currency scores: --ranked sorts by the "
        "width of that difference, --matrix lays it out base against quote."
    ),
)
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

    Every printed number and every printed marker is read from a
    `fbe.types.PairBias` field. Nothing here differences two scores, decides a
    direction, grades a conviction or judges a blocker: `fbe.bias` does all
    four and this command renders what it is handed. A spread carries an
    explicit sign, positive meaning the base currency's fundamentals sit above
    the quote's; agreement is a whole percentage of pillar weight, floored,
    and not a count of pillars.

    A pair a filter removed is listed under the table with the reason rather
    than dropped. A pair missing from the list is indistinguishable from a
    pair the engine never scored, and the second is the one worth knowing
    about.

    Raises:
        typer.BadParameter: With exit code 2 when ``--asof`` is in the future,
            when ``scoring.lookback_years`` cannot produce a window, or when
            ``--matrix`` is asked for, which needs `fbe.report._grid` and is
            not built yet. Each would otherwise print a table that looked like
            a run rather than a refusal.
        typer.Exit: With `EXIT_UNUSABLE` when the cache held nothing for the
            window, so an empty table cannot read as a working engine with no
            opinions.

    """
    config = _effective_config(ctx)
    run_date = asof.date() if asof is not None else date.today()
    if run_date > date.today():
        raise typer.BadParameter(
            f"--asof {run_date} is in the future, so every series would be "
            "past its allowance and every pair would come back flat",
            param_hint="--asof",
        )
    if matrix:
        # Refused rather than approximated. `report._grid` is the single place
        # a mirrored cell is produced, and until it lands a grid built here
        # would show market-convention numbers under the opposite row header,
        # which is a plausible number pointing the wrong way in exactly the
        # half of the grid nobody double-checks.
        raise typer.BadParameter(
            "--matrix needs the mirrored grid, which is not built yet. Use "
            "--ranked for the list of pairs.",
            param_hint="--matrix",
        )

    try:
        start = lookback_start(run_date, config.scoring.lookback_years)
    except ValueError as error:
        raise typer.BadParameter(
            f"scoring.lookback_years is unusable: {error}",
            param_hint="--asof",
        ) from error

    # Reads the cache and never the network, for the reason `score` gives at
    # the same call: a rolled-over TTL refetching mid-session would let two
    # runs at the same --asof and the same digest print different tables with
    # nothing on screen to explain it.
    result = collect(
        replace(config.data, offline=True),
        start=start,
        end=run_date,
        sources=ALL_SOURCES,
    )
    if not result.usable:
        typer.echo(
            "No observations in the cache for this window, so there is nothing "
            "to difference. Run fbe refresh to fill it, or fbe doctor to find "
            "out why it is empty."
        )
        raise typer.Exit(EXIT_UNUSABLE)

    scores = score_currencies(
        result.observations,
        default_pillars(config.scoring),
        config.scoring,
        run_date,
    )
    by_currency = {score.currency: score for score in scores}
    # Filtered before the view filters run, and over every pair rather than
    # the printed ones. A pair hidden by --majors still has to carry its real
    # blockers, because --majors is a change of view and the hidden list below
    # states why each pair went.
    filtered = tuple(
        apply_filters(bias_row, by_currency, config, run_date)
        for bias_row in build_pair_biases(scores, config, run_date)
    )
    shown, hidden = _bias_rows(filtered, majors, min_conviction, tradeable_only)

    if output_format is OutputFormat.JSON:
        typer.echo(
            _bias_json(
                shown,
                hidden,
                top,
                majors,
                run_date,
                config.digest(),
                min_conviction,
                tradeable_only,
            )
        )
    elif output_format is OutputFormat.CSV:
        typer.echo(_bias_csv(shown, top, run_date, config.digest()))
        for line in _hidden_lines(hidden, majors, min_conviction, tradeable_only):
            # CSV has no row-level place for a run-level statement, and the
            # hidden pairs are exactly that. They go to stderr so
            # `fbe bias --format csv > monday.csv` still writes a file a
            # parser can read, with the reasons still in front of whoever ran
            # it. The same split `score` makes with its warnings.
            typer.echo(line, err=True)
    else:
        _render_bias(
            shown,
            hidden,
            top,
            majors,
            run_date,
            config.digest(),
            min_conviction,
            tradeable_only,
        )


def _bias_rows(
    biases: Sequence[PairBias],
    majors: bool,
    min_conviction: Conviction,
    tradeable_only: bool,
) -> tuple[tuple[PairBias, ...], tuple[PairBias, ...]]:
    """Split the run into the rows to print and the rows a filter removed.

    Args:
        biases: Every pair the bias layer produced, already filtered by
            `fbe.bias.apply_filters`.
        majors: Restrict the pool to `fbe.universe.MAJORS`.
        min_conviction: The floor a pair must reach to be printed.
        tradeable_only: Drop pairs whose ``tradeable`` is ``False``.

    Returns:
        The kept rows and the removed rows, both ordered widest spread first.

        ``--majors`` narrows the pool and does not populate the removed list.
        It is a choice of which market to look at rather than a judgement
        about a pair, and the published example counts three majors hidden out
        of seven rather than twenty-four pairs hidden out of twenty-eight. The
        other two filters each say something about the pair they removed, so
        each removal is listed with its reason.

        ``tradeable`` is read from the field rather than from whether
        ``blockers`` is empty. Three of the eight strings `fbe.bias.BLOCKERS`
        declares do not block, and an offline run carries two of them on every
        pair, so a filter keying on the presence of a marker would hide the
        entire run.

    """
    pool = [row for row in biases if not majors or row.pair in MAJORS]
    pool.sort(key=_bias_order)
    kept: list[PairBias] = []
    removed: list[PairBias] = []
    floor = CONVICTION_RANK[min_conviction]
    for row in pool:
        if CONVICTION_RANK[row.conviction] < floor or (
            tradeable_only and not row.tradeable
        ):
            removed.append(row)
        else:
            kept.append(row)
    return tuple(kept), tuple(removed)


def _bias_order(bias_row: PairBias) -> tuple[float, str]:
    """Sort key placing the widest disagreement first.

    Args:
        bias_row: The row being placed.

    Returns:
        The negated absolute spread, then the pair code.

        On the width and not the signed value. The spread is
        ``composite(base) - composite(quote)``, so its sign says which leg is
        stronger and carries no information about how much the two economies
        disagree. Sorting on it descending would put every short pair below
        every long one and bury the widest disagreement in the run in the
        middle of the table.

        The pair code breaks a tie so two pairs at the same width print in the
        same order on every run. Without it the order falls out of whatever
        `fbe.universe.ALL_PAIRS` happened to hand over, and a table that
        reorders itself between two runs at the same ``--asof`` cannot be
        diffed.

    """
    return (-abs(bias_row.spread), bias_row.pair)


def _hidden_reason(
    bias_row: PairBias, min_conviction: Conviction, tradeable_only: bool
) -> str:
    """Say which filter removed this pair, in terms of the fields it read.

    Args:
        bias_row: The removed row.
        min_conviction: The floor that was in force.
        tradeable_only: Whether the tradeable filter was in force.

    Returns:
        One clause per filter that removed it, joined by a semicolon when both
        did. Every part is read off the row: the conviction it carries, the
        floor it was asked for, and the blockers `fbe.bias.apply_filters`
        recorded. Nothing is recomputed and no threshold is compared again,
        so the reason cannot disagree with the filter that produced it.

        A blocked pair names its blockers rather than saying "blocked". The
        reader's next action differs completely between ``coverage``, which
        means go and look at why the data is thin, and ``event``, which means
        wait for the release.

    """
    parts: list[str] = []
    if CONVICTION_RANK[bias_row.conviction] < CONVICTION_RANK[min_conviction]:
        parts.append(
            f"conviction {bias_row.conviction.value}, below {min_conviction.value}"
        )
    if tradeable_only and not bias_row.tradeable:
        parts.append("blocked: " + ", ".join(bias_row.blockers))
    return "; ".join(parts)


def _hidden_lines(
    hidden: Sequence[PairBias],
    majors: bool,
    min_conviction: Conviction,
    tradeable_only: bool,
) -> tuple[str, ...]:
    """Return the hidden-pairs block, heading included, or nothing at all.

    Args:
        hidden: The removed rows, already ordered.
        majors: Whether the pool was narrowed, which names it in the heading.
        min_conviction: The floor in force, for the reasons.
        tradeable_only: Whether the tradeable filter was in force.

    Returns:
        The heading and one indented line per removed pair, or an empty tuple
        when nothing was removed. Nothing rather than a heading reading zero:
        a run where every pair survived should not have to be read past.

    """
    if not hidden:
        return ()
    pool = "majors" if majors else "pairs"
    lines = [f"{len(hidden)} {pool} hidden by the filters:"]
    for row in hidden:
        reason = _hidden_reason(row, min_conviction, tradeable_only)
        lines.append(
            f"  {row.pair}  spread {row.spread:+.2f}, {reason}"
            if reason
            else f"  {row.pair}  spread {row.spread:+.2f}"
        )
    return tuple(lines)


def _bias_columns() -> str:
    """Return the column headings in the published layout."""
    return (
        f"{'Pair':<{PAIR_WIDTH}}"
        f"{'Dir':<{DIRECTION_WIDTH}}"
        f"{'Conv':<{CONVICTION_WIDTH}}"
        f"{'Spread':>{SPREAD_WIDTH}}"
        f"{'Base':>{LEG_WIDTH}}"
        f"{'Quote':>{LEG_WIDTH}}"
        f"{'Agree':>{AGREEMENT_WIDTH}}"
        "  Notes"
    )


def _bias_line(bias_row: PairBias) -> str:
    """Return one pair's row in the published layout.

    Args:
        bias_row: The row to print.

    Returns:
        The pair, direction, conviction, spread, both legs' composites,
        agreement and the blockers, every one of them read off the dataclass.

        The direction is `PairBias.direction` and not the sign of the spread.
        `fbe.bias.build_pair_biases` forces `Direction.NEUTRAL` whenever
        conviction is `Conviction.NONE`, however wide the spread, so a wide
        spread with no direction is a state the engine really produces and a
        renderer reading the sign would print a call over the engine's own
        refusal to make one.

        The blockers print verbatim, including the ones that do not block. An
        offline run carries ``cost:unchecked`` and ``event:unchecked`` on
        every pair, and those two say the checks never ran rather than that
        they passed. A marker that is not rendered does not exist.

    """
    notes = ", ".join(bias_row.blockers) if bias_row.blockers else NO_BLOCKERS
    return (
        f"{bias_row.pair:<{PAIR_WIDTH}}"
        f"{bias_row.direction.value:<{DIRECTION_WIDTH}}"
        f"{bias_row.conviction.value:<{CONVICTION_WIDTH}}"
        f"{bias_row.spread:>+{SPREAD_WIDTH}.2f}"
        f"{bias_row.base_score:>+{LEG_WIDTH}.2f}"
        f"{bias_row.quote_score:>+{LEG_WIDTH}.2f}"
        f"{str(_share_percent(bias_row.agreement)) + '%':>{AGREEMENT_WIDTH}}"
        f"  {notes}"
    )


def _render_bias(
    shown: Sequence[PairBias],
    hidden: Sequence[PairBias],
    top: int | None,
    majors: bool,
    asof: date,
    digest: str,
    min_conviction: Conviction,
    tradeable_only: bool,
) -> None:
    """Print the ranked list in the layout ``docs/interfaces.md`` publishes."""
    typer.echo(_run_header(asof, digest))
    typer.echo("")
    typer.echo(_bias_columns())
    for row in _truncate(shown, top):
        typer.echo(_bias_line(row))
    lines = _hidden_lines(hidden, majors, min_conviction, tradeable_only)
    if lines:
        typer.echo("")
        for line in lines:
            typer.echo(line)


def _truncate(rows: Sequence[PairBias], top: int | None) -> Sequence[PairBias]:
    """Return at most ``top`` rows, keeping the order.

    ``--top`` is a change of view and not a filter. It shortens what is
    printed and does not move a pair into the hidden list, which is why it is
    applied here rather than in `_bias_rows`: a pair below the cut was not
    rejected by anything and has no reason to give.
    """
    return rows if top is None else rows[:top]


def _bias_payload(rows: Sequence[PairBias]) -> list[dict[str, object]]:
    """Return the rows as plain data, shared by both machine formats.

    One builder for both, so a formatter cannot carry a field in one and drop
    it in the other, or round the table and not the JSON.
    """
    return [
        {
            "pair": row.pair,
            "base": row.base,
            "quote": row.quote,
            "direction": row.direction.value,
            "conviction": row.conviction.value,
            "spread": row.spread,
            "base_score": row.base_score,
            "quote_score": row.quote_score,
            "agreement": row.agreement,
            "tradeable": row.tradeable,
            "blockers": list(row.blockers),
        }
        for row in rows
    ]


def _bias_json(
    shown: Sequence[PairBias],
    hidden: Sequence[PairBias],
    top: int | None,
    majors: bool,
    asof: date,
    digest: str,
    min_conviction: Conviction,
    tradeable_only: bool,
) -> str:
    """Return the ranked list as JSON, carrying the run's own identifiers.

    The hidden pairs are a key of their own rather than being omitted. A
    consumer that only reads ``pairs`` sees the same list the table prints,
    and one that wants to know what went can find out without rerunning the
    command without its filters.
    """
    hidden_rows = _bias_payload(hidden)
    for row, source in zip(hidden_rows, hidden, strict=True):
        row["reason"] = _hidden_reason(source, min_conviction, tradeable_only)
    return json.dumps(
        {
            "asof": asof.isoformat(),
            "config_digest": digest,
            "pairs": _bias_payload(_truncate(shown, top)),
            "hidden": hidden_rows,
            "pool": "majors" if majors else "all",
        },
        indent=2,
    )


def _bias_csv(
    shown: Sequence[PairBias],
    top: int | None,
    asof: date,
    digest: str,
) -> str:
    """Return the ranked list as CSV, with the run's identifiers on every line.

    ``asof`` and ``config_digest`` are run-level facts and repeat on every
    row, which is how a flat format states one. They are carried because a
    saved CSV is the one rendering that outlives the terminal it was printed
    in, and a file of directional calls that cannot be tied to the weights
    that produced them is a file nobody can check later.

    ``blockers`` is a space-separated field. The strings `fbe.bias.BLOCKERS`
    declares contain no spaces, so the field splits back to the same list,
    where a comma would need quoting and a reader splitting on commas would
    tear ``event: <reason>`` in half.
    """
    columns = [
        "pair",
        "base",
        "quote",
        "direction",
        "conviction",
        "spread",
        "base_score",
        "quote_score",
        "agreement",
        "tradeable",
        "blockers",
        "asof",
        "config_digest",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in _bias_payload(_truncate(shown, top)):
        flat = dict(row)
        blockers = flat["blockers"]
        assert isinstance(blockers, list)
        flat["blockers"] = " ".join(str(entry) for entry in blockers)
        flat["asof"] = asof.isoformat()
        flat["config_digest"] = digest
        writer.writerow(flat)
    return buffer.getvalue().rstrip("\n")


@app.command(
    help=(
        "List upcoming releases and the blackout windows they create, so "
        "'avoid high-impact news' has clock times and affected pairs "
        "attached. The morning routine step."
    ),
)
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
    raise NotImplementedError(
        "fbe.cli.calendar is scaffolded; see docs/roadmap.md Phase 4"
    )


@app.command(
    help=(
        "Size a proposed trade from its stop distance, inside the plan's 1-2% "
        "risk band. Entry and stop are named options because two bare numbers "
        "on a command line are easy to transpose."
    ),
)
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

    The ticket prints both risk figures and leads with
    `fbe.types.PositionSize.realised_risk_amount`, what the lot size actually
    exposes once it is rounded down to a whole step. On an account this small
    the rounding gap is routinely 10% or more, and the intended figure is the
    one that is never actually at risk.

    The portfolio limits are checked against the journal, with no flag and no way
    to skip the read. `fbe.risk.check_limits` can only compare against a book it
    is given, and for as long as nothing gave it one it reported four limits as
    passed on no evidence. So this command reads `fbe.journal.JOURNAL_PATH` at
    call time, passes it explicitly to `fbe.journal.load`, keeps the records
    whose ``closed_at`` is ``None``, and hands those to `check_limits` as
    `fbe.risk.OpenPosition` views. `TradeRecord` satisfies that protocol as it
    stands, so nothing is converted and nothing is invented. The dependency runs
    one way: this layer reads the journal, `fbe.risk` never imports it.

    What the limits block on the ticket says, and why each part is there:

    * The count of open positions, the journal path, and when that file was last
      written. A clear concurrent limit means nothing without them, because a
      journal written up in the evening is behind the book by a whole session,
      and only the reader knows whether they have a ticket open that is not in
      the file yet.
    * One line per limit, each reading clear, breached or not performed.
      Not performed is printed as not performed. It is never folded into clear,
      and it never blocks the ticket: today the daily-loss and drawdown limits
      have no source at all, so refusing on absence would refuse every trade.
      See section 4 of ``docs/risk-and-execution.md``.
    * A breach prints as a warning on the ticket alongside the size, and does
      not change the exit code. Issue #46 decides whether it should.

    Missing and unreadable journals are different facts and print differently.
    An absent file is a fresh install with no trades: zero open positions, path
    named. A file that cannot be read or that holds a line `load` refuses to
    parse means the open book is **not known**, and both position limits report
    not performed rather than clear.

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
    raise NotImplementedError("fbe.cli.size is scaffolded; see docs/roadmap.md Phase 4")


@app.command(
    help=(
        "Render the full dated Markdown report into the reports directory, "
        "including the diff against the previous run. This is the audit "
        "trail."
    ),
)
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
    raise NotImplementedError(
        "fbe.cli.report is scaffolded; see docs/roadmap.md Phase 5"
    )


@app.command(
    help=(
        "Build the self-contained HTML dashboard: one file, no external "
        "assets, laid out for a phone during the session."
    ),
)
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
    raise NotImplementedError(
        "fbe.cli.dashboard is scaffolded; see docs/roadmap.md Phase 5"
    )


@journal_app.command(
    "add",
    help=(
        "Record one trade in the journal. The day's bias, conviction and "
        "config digest for that pair are attached automatically."
    ),
)
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

    R-multiples are recorded against the realised risk of the position rather
    than the intended risk, so the number the review reports is the one the
    account actually took.

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
    raise NotImplementedError(
        "fbe.cli.journal_add is scaffolded; see docs/roadmap.md Phase 4"
    )


@journal_app.command(
    "review",
    help=(
        "Summarise journalled trades against the plan and the engine: which "
        "followed the plan, and which followed the bias. Different failures "
        "with different fixes."
    ),
)
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
    raise NotImplementedError(
        "fbe.cli.journal_review is scaffolded; see docs/roadmap.md Phase 6"
    )
