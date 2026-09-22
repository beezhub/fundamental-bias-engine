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

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin, get_type_hints

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from fbe.bias import UNKNOWN_SUFFIX, blocking
from fbe.types import (
    BiasReport,
    Conviction,
    Direction,
    PairBias,
    PillarName,
)
from fbe.universe import G10

if TYPE_CHECKING:
    from fbe.config import Config

__all__ = [
    "TEMPLATE_NAME",
    "SIDECAR_FORMAT",
    "SIDECAR_GLOB",
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

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
"""Where `TEMPLATE_NAME` lives. Resolved from this module's own location
rather than from the working directory, so ``fbe report`` renders the same
from anywhere."""

FILENAME_FORMAT = "bias-{asof:%Y-%m-%d}.md"
"""Dated filename. Sorting the directory by name sorts it by date, which is
what makes ``--compare last`` a directory listing rather than a database."""

SIDECAR_FORMAT = "bias-{asof:%Y-%m-%d}.json"
"""The serialised `fbe.types.BiasReport` written beside every Markdown report.
Same stem, so the two are found together and lost together. ``--compare`` reads
this, never the Markdown, which keeps the report layout free to change."""

SIDECAR_GLOB = "bias-*.json"
"""Glob matching every sidecar `SIDECAR_FORMAT` can produce.

Kept beside the format rather than derived from it by string replacement: a
change to the date spec would leave a derived pattern matching nothing, and a
reader that finds no reports is indistinguishable from a directory that holds
none. ``fbe doctor`` reads this to find the newest report."""


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
        unknown_prefix: The blocker prefix that means the calendar guard was
            asked and could not answer, ``"event:unknown"``. Supplied rather
            than written into the templates so the string lives in
            `fbe.bias.UNKNOWN_SUFFIX` alone: a copy in a template is a copy
            that drifts, and the drift is silent because the label still
            renders.

            **Read by the Markdown template only.** It uses this to label a
            failed fetch differently from an offline run, which one label over
            both states cannot do. The dashboard gives the two states one
            label, "Not checked on this run", and shows the reason in the list
            beneath it;
            ``tests/test_blockers.py::test_the_dashboard_says_an_unknown_marker_was_not_checked_too``
            asserts that deliberately. Whether the dashboard should follow the
            Markdown report here is part of issue #45's first criterion and
            waits on `fbe.dashboard.build`, which is still scaffolded. This key
            is supplied to both because `build_context` builds one context, not
            two.

    Args:
        report: The run to render.
        diff: Optional diff against the previous run.
        config: Optional effective config, used for the weights table.

    Returns:
        A mapping suitable for ``jinja2.Template.render``, carrying exactly the
        keys listed above and no others.

        Ordering is the only decision made here, and it is made once for both
        templates. ``currencies`` is sorted by composite descending with the
        ISO code breaking ties, which reproduces the order and the tie-break
        `fbe.scoring.score_currencies` already applied, so the rank column
        never prints out of order against the list beside it. ``pairs`` is
        sorted by absolute spread: on the signed spread every short pair falls
        below every long one and the widest disagreement in the run ends up in
        the middle of the table.

        Nothing here computes a number. Every value on the page is a field on
        the objects passed in, which is what lets a report be checked against
        the `BiasReport` it came from.

    Raises:
        ValueError: From `_grid`, when a pair has a leg outside
            `fbe.universe.G10` or the run holds one pair twice.
        KeyError: From `_pillar_order`, when ``config`` carries no weight for
            a pillar.

    """
    return {
        "report": report,
        "diff": diff,
        "config": config,
        "grid": _grid(report.pairs),
        "pillar_order": _pillar_order(config),
        "currencies": tuple(
            sorted(report.currencies, key=lambda row: (-row.composite, row.currency))
        ),
        "pairs": tuple(
            sorted(report.pairs, key=lambda row: (-abs(row.spread), row.pair))
        ),
        "unknown_prefix": "event" + UNKNOWN_SUFFIX,
    }


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
        jinja2.UndefinedError: When the template reads a context key
            `build_context` does not supply. The environment uses
            ``StrictUndefined`` on purpose: Jinja's default renders an unknown
            name as an empty string, so a renamed key would empty a column and
            the page would still look like a report.
        jinja2.TemplateNotFound: When ``template_dir`` holds no
            `TEMPLATE_NAME`.

    Autoescaping is off because the output is Markdown, not HTML. The dashboard
    renders the same context into HTML and turns it on there.

    """
    directory = template_dir if template_dir is not None else _TEMPLATE_DIR
    environment = Environment(
        loader=FileSystemLoader(directory),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    return environment.get_template(TEMPLATE_NAME).render(
        **build_context(report, diff=diff, config=config)
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
        FileExistsError: When ``overwrite`` is false and either file already
            exists for this as-of date. Both are named in the message, because
            the one that exists is the one that says which half of an earlier
            run survived.
        OSError: When either file cannot be written, leaving the directory as
            it was. Both files are written under unique temporary names first,
            and any Markdown already there is moved aside rather than
            overwritten, so a sidecar that cannot be put in place restores the
            earlier run's pair instead of destroying half of it. Unlinking the
            new Markdown would not have been a rollback: `os.replace` has
            already overwritten the earlier one by then, and that file is the
            audit trail `CLAUDE.md` says no rerun can recreate.
        ValueError: When the report holds a value JSON cannot carry, such as a
            NaN produced by a division by zero upstream. A NaN written out
            reads back as a number and poisons any average computed over it.
        TypeError: When the report holds a value that could be written but not
            read back as what it is, which today means a date or a tuple
            inside an `fbe.types.Observation`'s free-form ``meta``. See
            `_encoded_opaque`.

    """
    markdown_path = out_dir / FILENAME_FORMAT.format(asof=report.asof)
    sidecar_path = out_dir / SIDECAR_FORMAT.format(asof=report.asof)
    if not overwrite:
        present = [path.name for path in (markdown_path, sidecar_path) if path.exists()]
        if present:
            raise FileExistsError(
                f"{', '.join(present)} already exists and overwrite is off. A "
                "second run on the same day normally should overwrite, because "
                "the later run saw more data."
            )

    # Both payloads are built before anything is opened, so a rendering or
    # serialisation failure cannot leave a half-written file on disk.
    rendered = render_report(report, diff=diff, config=config)
    payload = json.dumps(
        _encoded(report, BiasReport, "report"),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    # Unique per call rather than named after the as-of date. Two runs for one
    # date in one directory would otherwise share both temporary names, and an
    # interleaving leaves this run's Markdown beside the other run's sidecar:
    # a page and a set of numbers describing different runs, every figure in
    # both of them plausible.
    markdown_temp = _temporary(out_dir, markdown_path.name)
    sidecar_temp = _temporary(out_dir, sidecar_path.name)
    # An existing Markdown is moved aside rather than overwritten, so the pair
    # already on disk can be put back if the sidecar cannot be placed.
    kept = _temporary(out_dir, markdown_path.name) if markdown_path.is_file() else None
    if kept is not None:
        os.replace(markdown_path, kept)
    markdown_replaced = False
    try:
        markdown_temp.write_text(rendered, encoding="utf-8")
        sidecar_temp.write_text(payload, encoding="utf-8")
        # Markdown first. It is the destination most likely to refuse, and
        # putting the sidecar in place ahead of it is exactly the half-written
        # run this function exists to prevent.
        os.replace(markdown_temp, markdown_path)
        markdown_replaced = True
        os.replace(sidecar_temp, sidecar_path)
    except OSError:
        if markdown_replaced:
            markdown_path.unlink(missing_ok=True)
        if kept is not None:
            os.replace(kept, markdown_path)
            kept = None
        raise
    finally:
        for temporary in (markdown_temp, sidecar_temp, kept):
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return markdown_path


def _temporary(out_dir: Path, name: str) -> Path:
    """Return a fresh hidden path in ``out_dir``, for a file being put in place.

    Args:
        out_dir: The directory the finished file belongs in. The temporary
            file has to share it, because `os.replace` is only atomic within
            one filesystem.
        name: The finished file's name, carried into the temporary one so an
            operator who finds a leftover knows what it was going to be.

    Returns:
        A path no other run holds. ``mkstemp`` creates the file and returns a
        descriptor this closes immediately: the value here is the name, which
        is reserved for this caller, and the writes that follow go through
        `pathlib.Path` like every other write in this module.

    """
    handle, created = tempfile.mkstemp(dir=out_dir, prefix=f".{name}.", suffix=".part")
    os.close(handle)
    return Path(created)


def load_report(path: Path) -> BiasReport:
    """Read a previously written report back into a `fbe.types.BiasReport`.

    The Markdown file is for humans, so the numbers are also written to a JSON
    sidecar next to it with the same stem. This reads the sidecar. Parsing the
    Markdown back would make the layout load-bearing, and the layout should stay
    free to change.

    Args:
        path: Path to the Markdown report or its JSON sidecar.

    Returns:
        The reconstructed report. Every sequence comes back as a tuple and
        every mapping as a dict, which is what the producers in this package
        emit, so a report written and read back compares equal to itself. That
        holds for every field, free-form provenance included, because
        `_encoded_opaque` refuses at write time the values it could not
        restore here: a date, a tuple, anything JSON has no type for.

    Raises:
        FileNotFoundError: When the sidecar is absent. A report whose numbers
            cannot be read back is not a report, and an empty one returned
            here would be a run with no opinions.
        ValueError: When the sidecar is not JSON, is not an object, carries a
            field this version does not know, is missing one, or holds a value
            of the wrong shape for the field it sits in. Every message names
            the file and the field. Nothing is defaulted: a field absent from
            the sidecar is a report from a different version, and filling it
            in from the dataclass default would silently invent a number.

    """
    # ``with_suffix`` is the identity on a path that already ends ``.json``,
    # so one call covers both the Markdown path `write_report` returns and the
    # sidecar path `latest_report` returns.
    sidecar = path.with_suffix(".json")
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{sidecar.name} is not readable JSON: {error}") from error
    try:
        decoded = _decoded(payload, BiasReport, "report")
    except ValueError as error:
        raise ValueError(f"{sidecar.name}: {error}") from error
    assert isinstance(decoded, BiasReport)
    return decoded


def latest_report(reports_dir: Path, *, before: date | None = None) -> Path | None:
    """Find the most recent report on disk, for ``--compare last``.

    Args:
        reports_dir: Directory holding the dated report files.
        before: When given, ignore reports on or after this as-of date, so a
            backfilled run compares against the run that actually preceded it.

    Returns:
        Path to the newest matching sidecar, or ``None`` when the directory
        holds no reports yet. A directory that does not exist gives ``None``
        too: on a fresh clone, and under a ``--out`` pointed somewhere new,
        "no reports yet" is the same answer either way.

        The sidecar rather than the Markdown, because the sidecar is the only
        one ``--compare`` can read. `load_report` accepts either.

        Chosen by the date in the name rather than by the name itself. The two
        agree for every name `SIDECAR_FORMAT` produces and stop agreeing for
        anything else the glob picks up, and the newest report is not a
        question a directory listing should be able to get wrong.

    Raises:
        ValueError: When a file matching `SIDECAR_GLOB` carries no parseable
            as-of date. ``bias-backup.json`` sorts after every dated name, so
            a newest-by-name rule hands it to ``--compare``; skipping it
            quietly makes the newest report depend on what else is in the
            directory. Both are silent, so this is loud.

    """
    if not reports_dir.is_dir():
        return None
    dated: list[tuple[date, Path]] = []
    for candidate in sorted(reports_dir.glob(SIDECAR_GLOB)):
        stamp = _asof_in(candidate)
        if before is None or stamp < before:
            dated.append((stamp, candidate))
    if not dated:
        return None
    return max(dated, key=lambda item: (item[0], item[1].name))[1]


_SIDECAR_PREFIX = SIDECAR_FORMAT.split("{", 1)[0]
"""The literal head of `SIDECAR_FORMAT`, before its date placeholder.

Taken from the format rather than written out again, so renaming the files
moves the parser with them. This is the safe half of the derivation
`SIDECAR_GLOB` refuses: a wrong prefix makes every name unparseable and raises,
where a wrong glob matches nothing and returns an empty directory instead."""


def _asof_in(sidecar: Path) -> date:
    """Read the as-of date out of a sidecar's filename.

    Args:
        sidecar: A path matching `SIDECAR_GLOB`.

    Returns:
        The date `SIDECAR_FORMAT` wrote into the name.

    Raises:
        ValueError: When the name carries no ISO 8601 date, naming the file so
            the operator knows which one to move out of the directory.

    """
    stem = sidecar.stem
    try:
        return date.fromisoformat(stem.removeprefix(_SIDECAR_PREFIX))
    except ValueError as error:
        raise ValueError(
            f"{sidecar.name} matches {SIDECAR_GLOB} but carries no as-of date, "
            f"so it cannot be placed against the other reports: {error}"
        ) from error


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

        ``currencies`` holds every currency either run scored, ordered by the
        size of the move, largest first, with the ones that have no move to
        measure after them and the ISO code breaking ties. ``pairs`` holds only
        the pairs whose direction or conviction changed, flips first: twenty-
        eight unchanged rows under "what changed" is a section nobody reads,
        and the flip in the middle of it is what the section exists to show.

    Raises:
        ValueError: From `fbe.bias.blocking`, when a pair that left the
            shortlist carries a blocker string `fbe.bias.BLOCKERS` does not
            declare. An unrecognised marker is a defect in whatever produced
            it, and a departure reason that quietly dropped it would name the
            wrong cause.

    """
    return ReportDiff(
        previous_asof=previous.asof,
        current_asof=current.asof,
        currencies=_currency_changes(previous, current),
        pairs=_pair_changes(previous, current),
        shortlist_added=_shortlist_arrivals(previous, current),
        shortlist_removed=_shortlist_departures(previous, current),
        new_warnings=tuple(
            line for line in current.warnings if line not in set(previous.warnings)
        ),
        resolved_warnings=tuple(
            line for line in previous.warnings if line not in set(current.warnings)
        ),
        config_changed=previous.config_digest != current.config_digest,
    )


def _currency_changes(
    previous: BiasReport, current: BiasReport
) -> tuple[CurrencyChange, ...]:
    """Pair up the two runs' currency scores, one row per currency seen.

    A currency present in only one run has ``None`` on the other side and no
    delta. It did not move from zero, it appeared or it dropped out, and those
    are different facts: a currency that scored nothing yesterday and +1.20
    today reported as a delta of +1.20 is a fundamental move nobody can find in
    the data.
    """
    before = {row.currency: row for row in previous.currencies}
    after = {row.currency: row for row in current.currencies}
    changes = [
        CurrencyChange(
            currency=code,
            previous_composite=None if code not in before else before[code].composite,
            current_composite=None if code not in after else after[code].composite,
            previous_rank=None if code not in before else before[code].rank,
            current_rank=None if code not in after else after[code].rank,
            delta=(
                after[code].composite - before[code].composite
                if code in before and code in after
                else None
            ),
        )
        for code in before | after
    ]
    # An absent delta sorts last rather than as a move of zero. It is not a
    # small move, it is an appearance, and it should not compete with the
    # measured ones for the top of the list.
    return tuple(
        sorted(
            changes,
            key=lambda change: (
                change.delta is None,
                -abs(change.delta) if change.delta is not None else 0.0,
                change.currency,
            ),
        )
    )


def _pair_changes(previous: BiasReport, current: BiasReport) -> tuple[PairChange, ...]:
    """Report the pairs whose story changed, not the pairs whose number moved.

    Direction and conviction are the story. A spread that drifted from -1.40 to
    -1.31 without moving either is the model holding the same view slightly
    less far, which the ranked table already shows.
    """
    before = {row.pair: row for row in previous.pairs}
    after = {row.pair: row for row in current.pairs}
    changes: list[PairChange] = []
    for pair in before | after:
        was, now = before.get(pair), after.get(pair)
        previous_direction = was.direction if was else None
        current_direction = now.direction if now else None
        previous_conviction = was.conviction if was else None
        current_conviction = now.conviction if now else None
        if (
            previous_direction is current_direction
            and previous_conviction is current_conviction
        ):
            continue
        changes.append(
            PairChange(
                pair=pair,
                previous_direction=previous_direction,
                current_direction=current_direction,
                previous_conviction=previous_conviction,
                current_conviction=current_conviction,
                previous_spread=was.spread if was else None,
                current_spread=now.spread if now else None,
                flipped=_flipped(previous_direction, current_direction),
            )
        )
    return tuple(sorted(changes, key=_change_order))


def _flipped(previous: Direction | None, current: Direction | None) -> bool:
    """Whether the model reversed its side on this pair.

    Neutral is not a side, so neutral to long is the model forming a view
    rather than reversing one, and counting it would put those two in the same
    sentence. A pair missing from either run has no side either: it did not
    reverse, it arrived or it left.
    """
    sides = (Direction.LONG, Direction.SHORT)
    return previous in sides and current in sides and previous is not current


def _change_order(change: PairChange) -> tuple[bool, float, str]:
    """Sort key placing flips first, then the widest spread moves."""
    moved = (
        abs(change.current_spread - change.previous_spread)
        if change.current_spread is not None and change.previous_spread is not None
        else 0.0
    )
    return (not change.flipped, -moved, change.pair)


def _shortlist_arrivals(previous: BiasReport, current: BiasReport) -> tuple[str, ...]:
    """Pairs on today's shortlist that were not on the last one, in today's order."""
    before = {idea.bias.pair for idea in previous.shortlist}
    return tuple(
        idea.bias.pair for idea in current.shortlist if idea.bias.pair not in before
    )


def _shortlist_departures(
    previous: BiasReport, current: BiasReport
) -> tuple[tuple[str, str], ...]:
    """Pairs that left the shortlist, each with why, in the last run's order."""
    after = {idea.bias.pair for idea in current.shortlist}
    rows = {row.pair: row for row in current.pairs}
    return tuple(
        (idea.bias.pair, _departure_reason(idea.bias.pair, rows))
        for idea in previous.shortlist
        if idea.bias.pair not in after
    )


def _departure_reason(pair: str, rows: Mapping[str, PairBias]) -> str:
    """Say why a pair is no longer shortlisted, from the current run alone.

    Read off the current run's own row, which is data both reports carry.
    Anything else would be a reason the renderer invented, and a wrong reason
    is worse here than no reason: GBPJPY leaving for a data reason and GBPJPY
    leaving because the model changed its mind mean opposite things about
    whether to look at the chart.

    Args:
        pair: The pair that left.
        rows: The current run's pair biases, keyed by pair.

    Returns:
        One clause. ``fbe.bias.shortlist`` drops a pair for one of three
        reasons it records, plus one it does not: a pair can be tradeable,
        backed and simply beaten by a wider one, or beaten by a pair sharing a
        leg with it. That last case has nothing on the row to read, and it is
        named as the absence it is rather than guessed at.

    """
    row = rows.get(pair)
    if row is None:
        return "not in the current run"
    stoppers = blocking(row.blockers)
    if stoppers:
        return f"blocked: {', '.join(stoppers)}"
    if row.conviction is Conviction.NONE:
        return "conviction fell to none"
    if not row.tradeable:
        return "not tradeable, with no blocker recorded"
    return "still tradeable, not among this run's best"


# --- the sidecar codec -------------------------------------------------------
#
# The sidecar is the serialised `fbe.types.BiasReport` and nothing else: a JSON
# object keyed by field name, nested the way the dataclasses nest. There is no
# schema version and no envelope, because the only reader is this module and
# the only writer is `write_report`.
#
# Encoding walks the values and decoding walks the annotations. That asymmetry
# is deliberate. JSON cannot tell a date from a string, a `Direction` from the
# word "short", or an absent number from one that happens to be zero, and every
# one of those distinctions is load-bearing here. The annotation is the only
# place the right answer is written down, so the decoder reads it rather than
# guessing from the value in front of it.


def _encoded(value: Any, hint: Any, where: str) -> Any:
    """Turn one value into the JSON form its annotation says it has.

    Args:
        value: The value as the dataclass holds it.
        hint: The annotation it sits under, resolved to real types.
        where: Dotted path to this value inside the report, for the message.

    Returns:
        The same information as dicts, lists, strings, numbers, booleans and
        ``None``. Enums become their values, dates and instants become ISO
        8601 strings, dataclasses become objects keyed by field name, and
        every other sequence becomes a list.

        An instant keeps whatever offset it carries and a naive one is written
        without an offset, so it reads back exactly as it was written. Every
        producer in this package stamps UTC; nothing here invents an offset
        for one that does not, because that would move the timestamp.

    Raises:
        TypeError: When a value cannot be written without losing what it is.
            The annotation is read here for the same reason `_decoded` reads
            it: JSON has no date, no tuple and no enum, so a value written
            under a field annotated `typing.Any` comes back as whatever JSON
            made of it and nothing can restore the difference. Refusing at
            write time is the only point where the file and the object are
            both in hand.

    """
    origin = get_origin(hint)
    if origin in (Union, UnionType):
        return _encoded_union(value, hint, where)
    if origin is not None:
        return _encoded_container(value, hint, origin, where)
    if hint is Any:
        return _encoded_opaque(value, where)
    if isinstance(hint, type):
        if issubclass(hint, Enum):
            return _encoded_enum(value, where)
        if is_dataclass(hint):
            return _encoded_dataclass(value, hint, where)
        # One branch for both, because ``isoformat`` already writes each one
        # in full: a date gives a day and an instant gives a day and a time.
        # The distinction is only needed on the way back, where `_decoded`
        # picks the ``fromisoformat`` that refuses the other's string.
        if issubclass(hint, datetime | date):
            return _encoded_stamp(value, where)
    return _encoded_scalar(value, hint, where)


def _encoded_scalar(value: Any, hint: Any, where: str) -> Any:
    """Check a scalar against its annotation on the way out.

    The same shape check `_decoded_scalar` makes on the way in, made here so
    that the run which produced a value its annotation does not describe is
    the run that raises. Left to the decoder, a ``3.0`` in an ``int`` field
    writes cleanly and takes down the next morning's ``--compare last``
    instead, on a run that had nothing to do with it.

    Anything the checks below do not recognise passes through, with
    ``json.dumps`` as the backstop; it also refuses NaN and infinity, which
    `write_report` turns into its own message.
    """
    if hint is bool:
        if not isinstance(value, bool):
            raise TypeError(f"{where} should be a bool, found {value!r}")
    elif hint is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{where} should be an int, found {value!r}")
    elif hint is float:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise TypeError(f"{where} should be a float, found {value!r}")
    elif hint is str and not isinstance(value, str):
        raise TypeError(f"{where} should be a str, found {value!r}")
    return value


def _encoded_union(value: Any, hint: Any, where: str) -> Any:
    """Encode an optional value, keeping ``None`` as an absence."""
    if value is None:
        return None
    present = [member for member in get_args(hint) if member is not type(None)]
    if len(present) != 1:
        raise TypeError(f"{where} is annotated {hint!r}, which is ambiguous to write")
    return _encoded(value, present[0], where)


def _encoded_container(value: Any, hint: Any, origin: Any, where: str) -> Any:
    """Encode a mapping or a sequence, one element at a time."""
    arguments = get_args(hint)
    if isinstance(origin, type) and issubclass(origin, Mapping):
        key_hint, value_hint = arguments
        return {
            _encoded_key(key, key_hint, where): _encoded(
                item, value_hint, f"{where}[{key!r}]"
            )
            for key, item in value.items()
        }
    if isinstance(origin, type) and issubclass(origin, Sequence):
        return [
            _encoded(item, arguments[0], f"{where}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{where} is annotated {hint!r}, which a sidecar cannot carry")


def _encoded_dataclass(value: Any, hint: type, where: str) -> dict[str, Any]:
    """Encode one dataclass as an object keyed by field name."""
    hints = _annotations(hint)
    return {
        item.name: _encoded(
            getattr(value, item.name), hints[item.name], f"{where}.{item.name}"
        )
        for item in fields(hint)
    }


def _encoded_enum(value: Any, where: str) -> Any:
    """Encode an enum member as its value."""
    if not isinstance(value, Enum):
        raise TypeError(f"{where} should be an enum member, found {value!r}")
    return value.value


def _encoded_stamp(value: Any, where: str) -> str:
    """Encode a day or an instant in ISO 8601."""
    if not isinstance(value, date):
        raise TypeError(f"{where} should be a date or a datetime, found {value!r}")
    return value.isoformat()


def _encoded_key(key: Any, hint: Any, where: str) -> str:
    """Return a mapping key as the string JSON will hold.

    Refused rather than coerced with ``str``: a key silently stringified comes
    back as a string and stops matching the key the writer used, so a lookup
    that worked before the round trip returns nothing after it.
    """
    if isinstance(hint, type) and issubclass(hint, Enum):
        if not isinstance(key, hint):
            raise TypeError(f"{where} is keyed by {hint.__name__}, found {key!r}")
        encoded = key.value
        if not isinstance(encoded, str):
            raise TypeError(f"{key!r} has a non-string value and cannot key an object")
        return encoded
    if hint is str:
        if not isinstance(key, str):
            raise TypeError(f"{where} is keyed by str, found {key!r}")
        return key
    raise TypeError(f"{where} is keyed by {hint!r}, which a sidecar cannot carry")


_OPAQUE_SCALARS = (bool, int, float, str)


def _encoded_opaque(value: Any, where: str) -> Any:
    """Encode a value under a `typing.Any` annotation, refusing a lossy one.

    `fbe.types.Observation.meta` is the only such field. It is free-form
    provenance read from ``data/manual/*.yaml``, so it carries whatever
    ``yaml.safe_load`` produced, and an unquoted ``vintage: 2026-06-29`` is a
    ``datetime.date`` rather than a string.

    Nothing here can restore that on the way back: `_decoded` has only
    ``Any`` to go on, so it returns what JSON gave it. Writing the date as
    ``"2026-06-29"`` would make a report that does not equal itself after a
    round trip, and the loss would be invisible on the page and in the file.

    So a value JSON has no type for is refused with its path and the fix,
    rather than quietly changed. The nearest correct place for the rule is
    `fbe.datasources.manual`, which could require the block to be
    JSON-shaped at the point an operator writes it; this is the last place it
    can still be caught.
    """
    if value is None or isinstance(value, _OPAQUE_SCALARS):
        return value
    if isinstance(value, Mapping):
        return {
            _encoded_opaque_key(key, where): _encoded_opaque(item, f"{where}[{key!r}]")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _encoded_opaque(item, f"{where}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        # A list is the one nested shape JSON carries, so any other sequence
        # is refused rather than flattened into one. A tuple written out comes
        # back as a list and the report stops equalling itself, which is the
        # same loss as the date below wearing a different type.
        raise TypeError(
            f"{where} is a {type(value).__name__}, and JSON has only the list, "
            "so it would read back as a list. Free-form provenance holds "
            "lists: write it as one."
        )
    raise TypeError(
        f"{where} is a {type(value).__name__}, which JSON has no type for, so it "
        "would read back as something else. Free-form provenance has to be "
        "written as text, a number or a boolean: quote it in the YAML."
    )


def _encoded_opaque_key(key: Any, where: str) -> str:
    """Return a free-form mapping's key, which JSON requires to be a string."""
    if isinstance(key, str):
        return key
    raise TypeError(
        f"{where} is keyed by a {type(key).__name__} and JSON objects are keyed "
        "by strings, so the key would read back as text. Quote it in the YAML."
    )


def _decoded(payload: Any, hint: Any, where: str) -> Any:
    """Rebuild one value from the sidecar, guided by its annotation.

    Args:
        payload: The value as JSON produced it.
        hint: The annotation the value has to satisfy, resolved to real types.
        where: Dotted path to this value inside the report, for the message.

    Returns:
        The value as its annotation says it should be: an enum member, a date,
        an instant, a dataclass, a tuple, a dict or a scalar.

    Raises:
        ValueError: When the value does not fit its annotation. Naming the path
            matters more than it looks: a report holds 28 pairs and eight
            currencies of seven pillars each, and "not a number" without a path
            is a message nobody can act on.

    """
    origin = get_origin(hint)
    if origin in (Union, UnionType):
        return _decoded_union(payload, hint, where)
    if origin is not None:
        return _decoded_container(payload, hint, origin, where)
    if hint is Any:
        return payload
    if isinstance(hint, type):
        if issubclass(hint, Enum):
            return _decoded_enum(payload, hint, where)
        if is_dataclass(hint):
            return _decoded_dataclass(payload, hint, where)
        if issubclass(hint, datetime):
            return _decoded_stamp(payload, datetime, where)
        if issubclass(hint, date):
            return _decoded_stamp(payload, date, where)
        return _decoded_scalar(payload, hint, where)
    raise ValueError(f"{where} is annotated {hint!r}, which a sidecar cannot carry")


def _decoded_union(payload: Any, hint: Any, where: str) -> Any:
    """Decode an optional value, keeping ``None`` as an absence."""
    members = get_args(hint)
    if payload is None:
        if type(None) in members:
            return None
        raise ValueError(f"{where} is null but is not optional")
    present = [member for member in members if member is not type(None)]
    if len(present) != 1:
        raise ValueError(f"{where} is annotated {hint!r}, which is ambiguous to decode")
    return _decoded(payload, present[0], where)


def _decoded_container(payload: Any, hint: Any, origin: Any, where: str) -> Any:
    """Decode a mapping or a sequence, one element at a time."""
    arguments = get_args(hint)
    if isinstance(origin, type) and issubclass(origin, Mapping):
        if not isinstance(payload, dict):
            raise ValueError(f"{where} should be an object, found {_shape(payload)}")
        key_hint, value_hint = arguments
        return {
            _decoded_key(key, key_hint, where): _decoded(
                item, value_hint, f"{where}[{key!r}]"
            )
            for key, item in payload.items()
        }
    if isinstance(origin, type) and issubclass(origin, Sequence):
        if not isinstance(payload, list):
            raise ValueError(f"{where} should be a list, found {_shape(payload)}")
        # A tuple, because every producer in this package emits one and the
        # dataclasses default to one. A list here would compare unequal to the
        # report it was written from, on a field that had not changed.
        return tuple(
            _decoded(item, arguments[0], f"{where}[{index}]")
            for index, item in enumerate(payload)
        )
    raise ValueError(f"{where} is annotated {hint!r}, which a sidecar cannot carry")


def _decoded_key(key: str, hint: Any, where: str) -> Any:
    """Decode a mapping key, which JSON always hands over as a string."""
    if isinstance(hint, type) and issubclass(hint, Enum):
        return _decoded_enum(key, hint, f"{where} key")
    if hint is str:
        return key
    raise ValueError(f"{where} is keyed by {hint!r}, which a sidecar cannot carry")


def _decoded_enum(payload: Any, hint: type[Enum], where: str) -> Enum:
    """Rebuild an enum member, refusing a value the vocabulary does not hold."""
    try:
        return hint(payload)
    except ValueError as error:
        raise ValueError(
            f"{where} is {payload!r}, which is not one of "
            f"{', '.join(repr(member.value) for member in hint)}: {error}"
        ) from error


def _decoded_stamp(payload: Any, hint: type[date], where: str) -> date:
    """Rebuild a day or an instant from its ISO 8601 form.

    ``date.fromisoformat`` refuses a string carrying a time, which is the check
    that keeps an instant out of a field meaning a day. A day that secretly
    carries a time sorts against a real date in ways that look right until two
    runs on one date stop comparing equal.
    """
    if not isinstance(payload, str):
        raise ValueError(
            f"{where} should be an ISO 8601 string, found {_shape(payload)}"
        )
    try:
        return hint.fromisoformat(payload)
    except ValueError as error:
        raise ValueError(
            f"{where} is {payload!r}, which is not an ISO 8601 {hint.__name__}: {error}"
        ) from error


def _decoded_scalar(payload: Any, hint: type, where: str) -> Any:
    """Check a string, boolean or number against the field that holds it.

    ``bool`` is handled before ``int`` and excluded from both ``int`` and
    ``float``, because it is a subclass of ``int`` in Python and ``True``
    arriving in a risk figure would pass an ``isinstance`` check and then be
    arithmetic.
    """
    if hint is bool:
        if isinstance(payload, bool):
            return payload
    elif hint is int:
        if isinstance(payload, int) and not isinstance(payload, bool):
            return payload
    elif hint is float:
        if isinstance(payload, int | float) and not isinstance(payload, bool):
            return float(payload)
    elif hint is str:
        if isinstance(payload, str):
            return payload
    else:
        raise ValueError(f"{where} is annotated {hint!r}, which a sidecar cannot carry")
    raise ValueError(
        f"{where} should be a {hint.__name__}, found {_shape(payload)}: {payload!r}"
    )


def _decoded_dataclass(payload: Any, hint: type, where: str) -> Any:
    """Rebuild one dataclass, requiring exactly the fields it declares.

    A missing field is not filled from the dataclass default. The writer emits
    every field, so an absent one means a sidecar from a different version of
    this package, and a default substituted here would invent a number that
    looks like the run's own.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"{where} should be an object, found {_shape(payload)}")
    declared = {item.name for item in fields(hint)}
    unknown = sorted(set(payload) - declared)
    if unknown:
        raise ValueError(
            f"{where} carries {', '.join(unknown)}, which "
            f"{hint.__name__} does not declare"
        )
    missing = sorted(declared - set(payload))
    if missing:
        raise ValueError(
            f"{where} is missing {', '.join(missing)}, which {hint.__name__} declares"
        )
    hints = _annotations(hint)
    return hint(
        **{
            name: _decoded(payload[name], hints[name], f"{where}.{name}")
            for name in declared
        }
    )


_ANNOTATIONS: dict[type, Mapping[str, Any]] = {}


def _annotations(hint: type) -> Mapping[str, Any]:
    """Resolve one dataclass's annotations to real types, once per class.

    `fbe.types` uses ``from __future__ import annotations``, so every hint on
    it is a string until something resolves it. Cached because a report holds
    eight currencies of seven pillars, and resolving the same six classes on
    every one of them is the whole cost of loading a report.
    """
    resolved = _ANNOTATIONS.get(hint)
    if resolved is None:
        resolved = get_type_hints(hint)
        _ANNOTATIONS[hint] = resolved
    return resolved


def _shape(payload: Any) -> str:
    """Name what JSON actually produced, for a message about what it should be."""
    if payload is None:
        return "null"
    return {
        bool: "a boolean",
        int: "a number",
        float: "a number",
        str: "a string",
        list: "a list",
        dict: "an object",
    }.get(type(payload), f"a {type(payload).__name__}")


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

    The diagonal is ``None``: a currency has no bias against itself. A pair
    the run does not hold leaves both its cells ``None``, since a mirror with
    no original is a number the run never produced.

    Args:
        pairs: Pair biases from one run, in market convention.

    Returns:
        ``grid[base][quote]`` in `fbe.universe.G10` order on both axes, every
        off-diagonal cell oriented so a positive ``spread`` means the row
        currency is the fundamentally stronger of the two.

    Raises:
        ValueError: When a leg is outside `fbe.universe.G10`, which has no
            cell for it, or when two rows claim one cell, which happens when
            a pair arrives both ways round. Either dropped silently would lose
            or overwrite a pair with nothing on screen to say so.

    """
    grid: dict[str, dict[str, PairBias | None]] = {
        base: dict.fromkeys(G10) for base in G10
    }
    # Keyed by the unordered legs, so a pair arriving both ways round is
    # caught and named by the row that got there first.
    placed: dict[frozenset[str], str] = {}
    for row in pairs:
        for leg in (row.base, row.quote):
            if leg not in grid:
                raise ValueError(
                    f"{row.pair} has a leg outside the universe: {leg} has no "
                    "row or column in the grid"
                )
        legs = frozenset((row.base, row.quote))
        if legs in placed:
            raise ValueError(
                f"{row.pair} would overwrite the cells {placed[legs]} already "
                "fills: the run holds this pair twice, or both ways round"
            )
        placed[legs] = row.pair
        grid[row.base][row.quote] = row
        grid[row.quote][row.base] = _mirror(row)
    return grid


_INVERSE: Mapping[Direction, Direction] = {
    Direction.LONG: Direction.SHORT,
    Direction.SHORT: Direction.LONG,
    Direction.NEUTRAL: Direction.NEUTRAL,
}
"""What a direction becomes when the pair is read from the other leg.

Neutral maps to itself because it has no side to invert. The direction is
looked up rather than re-derived from the sign of the spread: `fbe.bias` forces
neutral on a wide spread whenever conviction is none, and a mirror that took
the sign would print a call over the engine's own refusal to make one."""


def _mirror(row: PairBias) -> PairBias:
    """Return the convention row read from its quote currency's side.

    Only the fields with a side change. The spread is negated rather than
    recomputed from the swapped scores, so the mirror agrees with the original
    to the bit rather than to rounding, and the pair string is the cell's own
    ``base + quote`` rather than market convention, per `_grid`.
    """
    return replace(
        row,
        pair=row.quote + row.base,
        base=row.quote,
        quote=row.base,
        base_score=row.quote_score,
        quote_score=row.base_score,
        spread=-row.spread,
        direction=_INVERSE[row.direction],
    )


def _pillar_order(config: Config | None) -> Sequence[PillarName]:
    """Order pillars by configured weight, heaviest first.

    Args:
        config: Effective config, or ``None`` to use the declaration order of
            `fbe.types.PillarName`.

    Returns:
        Every pillar, heaviest configured weight first. Ties keep
        `fbe.types.PillarName`'s own declaration order, which runs from the
        fastest and heaviest driver to the slowest, because `sorted` is stable
        over a sequence already in that order.

        ``None`` gives the declaration order unchanged. That is the honest
        answer for a caller that supplied no weights: sorting a map nobody
        passed would be a claim about weights this run does not hold.

    Raises:
        KeyError: When the config carries no weight for a pillar.
            `fbe.scoring.score_currencies` raises on the same lookup, so a
            quiet default here would only move where the operator meets it,
            and move it to the one place that prints a table rather than
            stopping.

    `fbe.cli._pillar_order` holds the same rule for a `fbe.config.ScoringConfig`
    and orders the ``--pillars`` columns with it. The two must agree, or the
    terminal table and the morning Markdown print the same seven numbers under
    different headings; ``tests/test_report.py`` asserts they do.

    """
    if config is None:
        return tuple(PillarName)
    weights = config.scoring.weights
    return tuple(sorted(PillarName, key=lambda name: -weights[name]))
