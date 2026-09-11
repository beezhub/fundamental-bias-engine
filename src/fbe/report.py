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

The files are committed alongside the code. A report and the config digest
that produced it are one artefact: re-weighting the pillars changes every
future call, and without the historical files there is no way to tell whether
the model improved or simply started agreeing with a different set of trades.
``data/reports/`` is tracked on purpose, and ``.gitignore`` says so where the
exclusion used to be. A report is precisely what cannot be reproduced later:
macro series get revised, cross-sectional scores depend on the rest of the
universe on the day, and the weights may have changed since. Re-running last
week's date does not recover last week's call. ``CLAUDE.md`` and the "Why
reports go to disk" section of ``docs/interfaces.md`` set this out.

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    "SIDECAR_FORMAT",
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

SIDECAR_FORMAT = "bias-{asof:%Y-%m-%d}.json"
"""The serialised `fbe.types.BiasReport` written beside every Markdown report.
Same stem, so the two are found together and lost together. ``--compare`` reads
this, never the Markdown, which keeps the report layout free to change."""


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
        grid: ``grid[base][quote]`` from `_grid`, already oriented so that
            every cell reads along its row. Templates render ``cell.spread``,
            ``cell.direction`` and ``cell.conviction`` exactly as they find
            them and never negate or relabel anything themselves.
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
    raise NotImplementedError(
        "fbe.report.build_context is scaffolded; see docs/roadmap.md Phase 5"
    )


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
    raise NotImplementedError(
        "fbe.report.render_report is scaffolded; see docs/roadmap.md Phase 5"
    )


def write_report(
    report: BiasReport,
    out_dir: Path,
    *,
    diff: ReportDiff | None = None,
    config: Config | None = None,
    overwrite: bool = True,
) -> Path:
    """Render a report and write both the Markdown and its JSON sidecar.

    Two files per run, sharing a stem: ``bias-YYYY-MM-DD.md`` from
    `FILENAME_FORMAT` and ``bias-YYYY-MM-DD.json`` from `SIDECAR_FORMAT`. The
    Markdown is for a reader. The sidecar is the serialised `BiasReport`, and it
    is the only thing `load_report` and therefore ``--compare last`` can read.

    Writing the Markdown alone is a silent failure, not a loud one: every run
    still succeeds, ``--compare last`` still finds a report, and the
    "what changed since the last run" section is empty forever with nothing
    raising to say why. Write both files or write neither. Both are governed by
    ``overwrite`` together, so a run can never leave a report whose sidecar
    belongs to a different run.

    Args:
        report: The run to render.
        out_dir: Directory to write into. Created if missing.
        diff: Optional diff against the previous run.
        config: Optional effective config.
        overwrite: When false, refuse to replace either existing file for the
            same as-of date. A second run on the same day normally should
            overwrite, because the later run saw more data.

    Returns:
        Path to the Markdown file. The sidecar sits beside it with the same
        stem and a ``.json`` suffix; `load_report` accepts either path.

    Raises:
        NotImplementedError: Always, until rendering lands.

    """
    raise NotImplementedError(
        "fbe.report.write_report is scaffolded; see docs/roadmap.md Phase 5"
    )


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
    raise NotImplementedError(
        "fbe.report.load_report is scaffolded; see docs/roadmap.md Phase 5"
    )


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
    raise NotImplementedError(
        "fbe.report.latest_report is scaffolded; see docs/roadmap.md Phase 5"
    )


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
    raise NotImplementedError(
        "fbe.report.diff_reports is scaffolded; see docs/roadmap.md Phase 5"
    )


def _grid(pairs: Sequence[PairBias]) -> Mapping[str, Mapping[str, PairBias | None]]:
    """Lay the pair list out as a base against quote grid, oriented per row.

    A run holds 28 pairs in market convention, and the grid has 56 populated
    cells. Half of them are therefore mirrors, and this function is the single
    place that produces them. Both templates read a cell raw, so anything not
    inverted here is displayed inverted: the reader is told to read along the
    row, and a mirrored cell that still holds the market-convention values says
    the opposite of what the row header claims. That inverts the whole lower
    triangle of the matrix and the heatmap colour with it, while every number
    on screen stays plausible.

    For the cell at ``grid[X][Y]`` where the market quotes ``YX``, derive a new
    `fbe.types.PairBias` from the convention row:

    * ``spread`` negated.
    * ``base`` and ``quote`` swapped, so ``base == X`` and ``quote == Y``.
    * ``base_score`` and ``quote_score`` swapped with them, so the cell still
      satisfies ``spread == base_score - quote_score``.
    * ``pair`` rewritten as ``X + Y``. This is a display string for the cell it
      sits in and is deliberately not market convention. Mirrored cells are for
      reading the grid and must never reach the ranked list, the shortlist, the
      journal or anything that places an order; those all take pairs from
      ``report.pairs``, which stays in convention.
    * ``direction`` inverted, long to short and short to long. Neutral is
      unchanged, since neutral has no side to invert.
    * ``conviction``, ``agreement``, ``asof``, ``tradeable`` and ``blockers``
      unchanged. How strongly the model holds a view, and whether a release
      blocks it, do not depend on which way round the pair is written.

    The diagonal is ``None``: a currency has no bias against itself.

    Args:
        pairs: Pair biases from one run, in market convention.

    Returns:
        ``grid[base][quote]`` in `fbe.universe.G10` order on both axes, every
        off-diagonal cell oriented so a positive ``spread`` means the row
        currency is the fundamentally stronger of the two.

    Raises:
        NotImplementedError: Always, until rendering lands.

    """
    raise NotImplementedError(
        "fbe.report._grid is scaffolded; see docs/roadmap.md Phase 5"
    )


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
    raise NotImplementedError(
        "fbe.report._pillar_order is scaffolded; see docs/roadmap.md Phase 5"
    )
