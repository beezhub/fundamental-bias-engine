"""Markdown rendering for a `fbe.types.BiasReport`.

Why reports go to disk instead of only to the terminal
------------------------------------------------------
A bias call you cannot audit a week later is worthless. When a trade goes
wrong, the only useful question is which part was wrong: the fundamental call,
the conviction attached to it, the size, or the entry. That question cannot be
answered from memory, and it cannot be answered from a terminal scrollback that
was closed on Tuesday. So every run writes a dated Markdown file carrying the
scores, the inputs that produced them, and the config digest that weighted them.
Markdown because it stays readable in ten years, diffs cleanly, and needs no
tooling to open.

The files are meant to be committed alongside the code. A report and the config
digest that produced it are one artefact: re-weighting the pillars changes every
future call, and without the historical files there is no way to tell whether
the model improved or simply started agreeing with a different set of trades.
Note that ``data/reports/`` is currently listed in ``.gitignore``, so keeping the
history requires relaxing that rule; see ``docs/interfaces.md``.

Why the diff is a first-class section
-------------------------------------
The diff between two runs is often more informative than either run alone. A
composite of +1.8 on USD says little on its own. The same +1.8 after +0.4
yesterday says the rate expectations repriced overnight, which is a reason to
look at the chart today. Fundamentals move slowly, so any large one-day move in
a score is either news worth trading or a data error worth fixing, and both are
worth being told about explicitly rather than being left to spot by eye.

Report sections, in order:

1. Header: as-of date, generation timestamp, config digest, pillar weights.
2. Currency ranking: composite, rank, dispersion, coverage, pillar columns.
3. Pair matrix: the 8x8 base against quote grid of spreads.
4. Tradeable shortlist: each idea with its reasoning, the pillars that carry it,
   the pillars that dissent, size, and any blackout.
5. Calendar: upcoming high-impact events and the blackout windows they create.
6. Data coverage and warnings: which pillars ran short of data and how stale it
   was, so a thin call is never mistaken for a confident one.
7. What changed since the last run: the `ReportDiff` sections.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from fbe.config import Config
    from fbe.types import (
        BiasReport,
        Conviction,
        Direction,
        PairBias,
        PillarName,
    )

__all__ = [
    "TEMPLATE_NAME",
    "CurrencyChange",
    "PairChange",
    "ReportDiff",
    "build_context",
    "render_report",
    "write_report",
    "load_report",
    "latest_report",
    "diff_reports",
]

TEMPLATE_NAME = "report.md.j2"
"""Template file inside ``fbe/templates``. Shipped with the package so the
report renders the same from a checkout and from an installed wheel."""

FILENAME_FORMAT = "bias-{asof:%Y-%m-%d}.md"
"""Dated filename. Sorting the directory by name sorts it by date, which is
what makes ``--compare last`` a directory listing rather than a database."""


@dataclass(frozen=True, slots=True)
class CurrencyChange:
    """One currency's movement between two runs.

    Attributes:
        currency: ISO 4217 code.
        previous_composite: Composite in the baseline run, or ``None`` when the
            currency was absent, for example because every pillar lacked data.
        current_composite: Composite in the current run.
        previous_rank: Rank in the baseline run, 1 being strongest.
        current_rank: Rank in the current run.
        delta: ``current_composite - previous_composite``, or ``None`` when
            either side is missing.
    """

    currency: str
    previous_composite: float | None
    current_composite: float | None
    previous_rank: int | None = None
    current_rank: int | None = None
    delta: float | None = None


@dataclass(frozen=True, slots=True)
class PairChange:
    """One pair's movement between two runs.

    A direction flip is the loudest thing this engine can say, so it is kept as
    an explicit field rather than left to be inferred from the spread sign.

    Attributes:
        pair: Pair in market convention.
        previous_direction: Direction in the baseline run.
        current_direction: Direction in the current run.
        previous_conviction: Conviction in the baseline run.
        current_conviction: Conviction in the current run.
        previous_spread: Score spread in the baseline run.
        current_spread: Score spread in the current run.
        flipped: True when the direction changed and neither side was neutral.
    """

    pair: str
    previous_direction: Direction | None
    current_direction: Direction | None
    previous_conviction: Conviction | None
    current_conviction: Conviction | None
    previous_spread: float | None = None
    current_spread: float | None = None
    flipped: bool = False


@dataclass(frozen=True, slots=True)
class ReportDiff:
    """What changed between two runs of the engine.

    Attributes:
        previous_asof: As-of date of the baseline run.
        current_asof: As-of date of the current run.
        currencies: Per-currency movement, ordered by absolute ``delta``.
        pairs: Per-pair movement, direction flips first.
        shortlist_added: Pairs that entered the tradeable shortlist.
        shortlist_removed: Pairs that left it, with the reason where known.
        new_warnings: Warnings present now and absent before.
        resolved_warnings: Warnings present before and absent now.
        config_changed: True when the config digest differs, in which case score
            movements are not comparable and the report says so.
    """

    previous_asof: date
    current_asof: date
    currencies: Sequence[CurrencyChange] = field(default_factory=tuple)
    pairs: Sequence[PairChange] = field(default_factory=tuple)
    shortlist_added: Sequence[str] = field(default_factory=tuple)
    shortlist_removed: Sequence[tuple[str, str]] = field(default_factory=tuple)
    new_warnings: Sequence[str] = field(default_factory=tuple)
    resolved_warnings: Sequence[str] = field(default_factory=tuple)
    config_changed: bool = False


def build_context(
    report: BiasReport,
    *,
    diff: ReportDiff | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """Assemble the variables the templates render against.

    Both the Markdown template and the dashboard template consume this, so the
    two views cannot drift apart in what they show or in how they order it.

    Context keys:
        report: The `fbe.types.BiasReport` itself.
        diff: The `ReportDiff` against the baseline run, or ``None``.
        config: The `fbe.config.Config` behind the run, for the weights table.
        grid: ``grid[base][quote]`` giving the `fbe.types.PairBias` for that
            ordered cell, or ``None`` where the pair is not quoted that way.
            Filled on both sides of the diagonal with the sign flipped, since a
            matrix that is only half populated is unreadable.
        pillar_order: `fbe.types.PillarName` values in display order, heaviest
            weight first, so the columns that drive the score come first.
        currencies: Currency scores sorted strongest to weakest.
        pairs: Pair biases sorted by absolute spread, widest first.

    Args:
        report: The run to render.
        diff: Optional diff against the previous run.
        config: Optional effective config, used for the weights table.

    Returns:
        A mapping suitable for ``jinja2.Template.render``.

    Raises:
        NotImplementedError: Always, until rendering lands.
    """
    raise NotImplementedError("fbe.report.build_context is scaffolded")


def render_report(
    report: BiasReport,
    *,
    diff: ReportDiff | None = None,
    config: Config | None = None,
    template_dir: Path | None = None,
) -> str:
    """Render a report to Markdown.

    Args:
        report: The run to render.
        diff: Optional diff against the previous run, rendered as the final
            section.
        config: Optional effective config, used for the weights table.
        template_dir: Override for the template search path. Defaults to
            ``fbe/templates``. Useful for testing a template change without
            touching the installed package.

    Returns:
        The rendered Markdown document.

    Raises:
        NotImplementedError: Always, until rendering lands.
    """
    raise NotImplementedError("fbe.report.render_report is scaffolded")


def write_report(
    report: BiasReport,
    out_dir: Path,
    *,
    diff: ReportDiff | None = None,
    config: Config | None = None,
    overwrite: bool = True,
) -> Path:
    """Render a report and write it to a dated file.

    Args:
        report: The run to render.
        out_dir: Directory to write into. Created if missing.
        diff: Optional diff against the previous run.
        config: Optional effective config.
        overwrite: When false, refuse to replace an existing file for the same
            as-of date. A second run on the same day normally should overwrite,
            because the later run saw more data.

    Returns:
        Path to the written file.

    Raises:
        NotImplementedError: Always, until rendering lands.
    """
    raise NotImplementedError("fbe.report.write_report is scaffolded")


def load_report(path: Path) -> BiasReport:
    """Read a previously written report back into a `fbe.types.BiasReport`.

    The Markdown file is for humans, so the numbers are also written to a JSON
    sidecar next to it with the same stem. This reads the sidecar. Parsing the
    Markdown back would make the layout load-bearing, and the layout should stay
    free to change.

    Args:
        path: Path to the Markdown report or its JSON sidecar.

    Returns:
        The reconstructed report.

    Raises:
        NotImplementedError: Always, until serialisation lands.
    """
    raise NotImplementedError("fbe.report.load_report is scaffolded")


def latest_report(reports_dir: Path, *, before: date | None = None) -> Path | None:
    """Find the most recent report on disk, for ``--compare last``.

    Args:
        reports_dir: Directory holding the dated report files.
        before: When given, ignore reports on or after this as-of date, so a
            backfilled run compares against the run that actually preceded it.

    Returns:
        Path to the newest matching report, or ``None`` when the directory holds
        no reports yet.

    Raises:
        NotImplementedError: Always, until report discovery lands.
    """
    raise NotImplementedError("fbe.report.latest_report is scaffolded")


def diff_reports(previous: BiasReport, current: BiasReport) -> ReportDiff:
    """Compare two runs and describe what moved.

    Rules the implementation must hold to:
        * Currencies and pairs present in only one run are reported as such
          rather than treated as a move from zero. A missing score and a score
          of zero mean opposite things.
        * A direction flip is recorded even when both spreads are small, because
          a flip is a change in the story, not only in the number.
        * When the config digest differs, ``config_changed`` is set and the
          score deltas are reported as not comparable. A weight change moves
          every score at once, and reading that as a market move is the easiest
          way to talk yourself into a trade that is not there.

    Args:
        previous: The baseline run.
        current: The run being reported.

    Returns:
        The populated `ReportDiff`.

    Raises:
        NotImplementedError: Always, until the diff lands.
    """
    raise NotImplementedError("fbe.report.diff_reports is scaffolded")


def _grid(pairs: Sequence[PairBias]) -> Mapping[str, Mapping[str, PairBias | None]]:
    """Lay the pair list out as a base against quote grid.

    Args:
        pairs: Pair biases from one run.

    Returns:
        ``grid[base][quote]``, populated on both sides of the diagonal.

    Raises:
        NotImplementedError: Always, until rendering lands.
    """
    raise NotImplementedError("fbe.report._grid is scaffolded")


def _pillar_order(config: Config | None) -> Sequence[PillarName]:
    """Order pillars by configured weight, heaviest first.

    Args:
        config: Effective config, or ``None`` to use the declaration order of
            `fbe.types.PillarName`.

    Returns:
        The display order for pillar columns.

    Raises:
        NotImplementedError: Always, until rendering lands.
    """
    raise NotImplementedError("fbe.report._pillar_order is scaffolded")
