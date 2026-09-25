"""Join a past bias record to the move that followed it.

This module answers one question and refuses the next one: what actually
happened after the engine published a view. It produces rows. It computes no
hit rate, no average and no interval, because a wrong join buried inside a
statistic is a statistic nobody can audit, and the statistics are
`docs/roadmap.md` Phase 6's separate piece of work.

Why the bias record rather than the journal
-------------------------------------------
Twenty-eight pairs are recorded every morning and perhaps five trades a week
are taken, so the bias record carries roughly forty times the evidence the
journal does. `docs/roadmap.md` Phase 6 says evaluation runs "against the bias
record for all 28 pairs, not just the pairs that were traded", and that is the
only route to a conclusion this side of years. The pairs the engine declined to
back are part of it: they are the control group, and a record holding only the
cases the model liked cannot say whether the model separated anything.

The as-of line
--------------
A report is built from observations released on or before its as-of date, so a
fixing dated that day is one the run could have been looking at. Every price
this module reads is dated strictly after the as-of. That is not a nicety: a
backtest that sees the future flatters, and `.claude/skills/engineering-standards`
lists it as one of the defects that produced a plausible number rather than an
error.

What is not here
----------------
Nothing in this module claims a measured result. These are inputs to an
analysis. A pair that moved is not a pair the model called, and this file
cannot tell the difference: that is the reader's work, downstream, with the
counts and the intervals in front of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from fbe.config import ScoringConfig
from fbe.report import SIDECAR_GLOB, load_report
from fbe.risk import MissingRateError, convert_rate
from fbe.types import Conviction, Direction

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from fbe.types import BiasReport

__all__ = [
    "ForwardRow",
    "ForwardJoin",
    "join_report",
    "join_reports",
]


@dataclass(frozen=True, slots=True)
class ForwardRow:
    """One pair's recorded bias, with the move that followed it attached.

    Attributes:
        asof: The report's as-of date. The bias was published for this day and
            every price below is dated after it.
        pair: Six characters in market convention, as `fbe.universe.ALL_PAIRS`
            writes it.
        direction: The direction recorded in the report, copied rather than
            recomputed. The point of the join is to evaluate what was
            published.
        conviction: The conviction band recorded in the report, `NONE`
            included.
        spread: The composite spread that produced the direction, on the
            ``-6..+6`` band the two composites can span. Positive means the
            base scored higher, which is the same sign convention ``move``
            carries, so the two are directly comparable.
        agreement: Pillar agreement behind that spread, 0.0 to 1.0.
        coverage: The weaker of the two legs' coverage, 0.0 to 1.0. A pair is
            only as well evidenced as its thinner side, and a reader filtering
            on evidence wants the floor rather than the average.
        config_digest: The digest of the config that produced the bias.
            Cross-sectional scores depend on the weights that made them, so
            rows from two digests are not poolable without saying so.
        opened_at: The first session after ``asof`` that had a fixing. The move
            is measured from here.
        closed_at: The last session inside the horizon that had a fixing.
        open_rate: Quote currency per one unit of the base, at ``opened_at``.
        close_rate: The same, at ``closed_at``.
        move: ``close_rate / open_rate - 1``, a fraction rather than a
            percentage. Positive means the base currency strengthened against
            the quote, in the pair's market convention.

    """

    asof: date
    pair: str
    direction: Direction
    conviction: Conviction
    spread: float
    agreement: float
    coverage: float
    config_digest: str
    opened_at: date
    closed_at: date
    open_rate: float
    close_rate: float
    move: float


@dataclass(frozen=True, slots=True)
class ForwardJoin:
    """The rows a join produced, and everything it could not produce.

    Attributes:
        rows: One per pair per report, in as-of order and then pair order.
        problems: One sentence per gap, naming the report and the pair. A gap
            that is not reported is a gap that flatters every figure computed
            from what remains, so this is never empty when rows are missing.

    """

    rows: tuple[ForwardRow, ...] = ()
    problems: tuple[str, ...] = ()


def join_report(
    report: BiasReport,
    rates: Mapping[date, Mapping[str, float]],
    *,
    scoring: ScoringConfig | None = None,
) -> ForwardJoin:
    """Attach the realised move to every pair in one report.

    Args:
        report: A run already written and read back, not one computed now.
        rates: Fixings by session, each session mapping a pair in market
            convention to the quote currency per one unit of the base. Only the
            dollar legs need be present: every cross is derived from them
            through `fbe.risk.convert_rate`, which owns the resolution rule
            including the pivot. Sessions on or before the report's as-of are
            ignored rather than refused, since a caller handing over a whole
            history should not have to trim it.
        scoring: Supplies ``horizon_days``, the window the move is measured
            over. Defaults to `ScoringConfig()`, which is the packaged value
            rather than the operator's; a caller holding a config should pass
            ``load_config().scoring``, because a window that does not match the
            one the engine scored for compares two different questions.

    Returns:
        The rows, and a sentence for every pair that could not be given one.

        A pair yields no row when the rate table cannot produce its rate on
        either session, and when the table does not reach `horizon_days` past
        the as-of. The second is the one that matters: an outcome nobody can
        see yet is not a zero move. Reported as zero it would enter the sample
        as a flat result and the whole record would read more neutral than it
        is, which is the difference `docs/decisions/0002-representing-not-known.md`
        exists to keep.

    Note:
        Nothing is mutated. The rates handed in are read and handed back
        untouched.

    """
    settings = ScoringConfig() if scoring is None else scoring
    horizon = report.asof + timedelta(days=settings.horizon_days)
    window = sorted(session for session in rates if report.asof < session <= horizon)
    # Reaching the horizon and having a fixing on it are different questions.
    # The market is shut two days in seven, so a horizon landing on a Saturday
    # has no fixing of its own and the Friday before it is the right close. A
    # table that stops before the horizon has not answered yet.
    reaches = any(session >= horizon for session in rates)
    if not window or not reaches:
        return ForwardJoin(
            problems=(
                f"{report.asof}: the rates given do not reach the "
                f"{settings.horizon_days}-day horizon ending {horizon}, so no "
                "move has happened yet to attach. This is not a flat result.",
            )
        )

    opened_at, closed_at = window[0], window[-1]
    coverage = {score.currency: score.coverage for score in report.currencies}
    rows: list[ForwardRow] = []
    problems: list[str] = []
    for bias in sorted(report.pairs, key=lambda row: row.pair):
        try:
            open_rate = convert_rate(bias.base, bias.quote, rates[opened_at])
            close_rate = convert_rate(bias.base, bias.quote, rates[closed_at])
        except MissingRateError as error:
            problems.append(
                f"{report.asof} {bias.pair}: no rate could be built from the "
                f"sessions given, so the move is unknown rather than zero "
                f"({error})"
            )
            continue
        if bias.base not in coverage or bias.quote not in coverage:
            problems.append(
                f"{report.asof} {bias.pair}: the report carries no score for "
                "one of its legs, so how well evidenced the bias was cannot "
                "be stated"
            )
            continue
        rows.append(
            ForwardRow(
                asof=report.asof,
                pair=bias.pair,
                direction=bias.direction,
                conviction=bias.conviction,
                spread=bias.spread,
                agreement=bias.agreement,
                coverage=min(coverage[bias.base], coverage[bias.quote]),
                config_digest=report.config_digest,
                opened_at=opened_at,
                closed_at=closed_at,
                open_rate=open_rate,
                close_rate=close_rate,
                move=(close_rate / open_rate) - 1.0,
            )
        )
    return ForwardJoin(rows=tuple(rows), problems=tuple(problems))


def join_reports(
    directory: Path,
    rates: Mapping[date, Mapping[str, float]],
    *,
    scoring: ScoringConfig | None = None,
) -> ForwardJoin:
    """Join every report in a directory, oldest first.

    Args:
        directory: Where the dated sidecars live, normally
            ``DataConfig.reports_dir``.
        rates: As `join_report`, spanning every report's horizon.
        scoring: As `join_report`.

    Returns:
        Every row from every readable report, ordered by as-of and then pair,
        with one problem per report that could not be read and per pair that
        could not be given a row.

        A report that will not decode is named rather than skipped. Dropping a
        day quietly understates the record and flatters everything computed
        from what is left, and the day that drops is the day something went
        wrong.

        An empty directory is a fact about the record rather than a failure, so
        it comes back as no rows and one sentence saying so.

    Raises:
        FileNotFoundError: When ``directory`` is not a directory. Absent and
            empty are different answers, and a caller pointed at the wrong path
            should hear about it rather than read a clean empty record.

    """
    if not directory.is_dir():
        raise FileNotFoundError(
            f"{directory} is not a directory, so there are no reports to join. "
            "An empty reports directory and a wrong path are different answers."
        )
    rows: list[ForwardRow] = []
    problems: list[str] = []
    sidecars = sorted(directory.glob(SIDECAR_GLOB))
    if not sidecars:
        return ForwardJoin(problems=(f"{directory} holds no reports to join.",))
    for sidecar in sidecars:
        try:
            report = load_report(sidecar)
        except (ValueError, OSError) as error:
            problems.append(
                f"{sidecar.name} could not be read as a report, so that day is "
                f"missing from the record rather than absent from it: {error}"
            )
            continue
        joined = join_report(report, rates, scoring=scoring)
        rows.extend(joined.rows)
        problems.extend(joined.problems)
    rows.sort(key=lambda row: (row.asof, row.pair))
    return ForwardJoin(rows=tuple(rows), problems=tuple(problems))
