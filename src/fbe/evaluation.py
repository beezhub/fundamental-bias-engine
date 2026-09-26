"""Join a past bias record to the move that followed it.

This module answers one question and refuses the next one: what actually
happened after the engine published a view. It produces rows. It computes no
hit rate, no average and no interval, because a wrong join buried inside a
statistic is a statistic nobody can audit, and the statistics are
`docs/roadmap.md` Phase 6's separate piece of work.

Why the bias record rather than the journal
-------------------------------------------
Twenty-eight pairs are recorded every morning and a handful are traded in a
week, so the bias record carries something like an order of magnitude more
evidence than the journal does. Neither figure has been counted, and the ratio
is not a measurement: it is the reason `docs/roadmap.md` Phase 6 says evaluation
runs "against the bias record for all 28 pairs, not just the pairs that were
traded", which is the only route to a conclusion this side of years.

The pairs the engine declined to back are part of it: they are the control
group, and a record holding only the cases the model liked cannot say whether
the model separated anything.

The as-of line
--------------
A report is built from observations released on or before its as-of date, so a
fixing dated that day is one the run could have been looking at. Every price
this module reads is dated strictly after the as-of. That is not a nicety: a
backtest that sees the future flatters, and `docs/methodology.md` and the house
standards both name it as one of the defects that produce a plausible number
rather than an error.

The as-of line is the half this module can enforce. The other half is the
report's own: `generated_at` says when the run was made, and a report backfilled
weeks later read revised macro series that the morning's run could not have. The
date is carried onto every row rather than checked here, because whether a
backfilled run belongs in a sample is the reader's decision and not this
module's.

What is not here
----------------
Nothing in this module claims a measured result. These are inputs to an
analysis. A pair that moved is not a pair the model called, and this file
cannot tell the difference: that is the reader's work, downstream, with the
counts and the intervals in front of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from fbe.config import ScoringConfig
from fbe.report import SIDECAR_GLOB, load_report
from fbe.risk import MissingRateError, convert_rate
from fbe.types import Conviction, Direction

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
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
            base scored higher, which is the sign convention ``move`` carries
            too, so the two are comparable in sign. They are not comparable in
            scale: this is a score-band difference and ``move`` is a return.
        agreement: Pillar agreement behind that spread, 0.0 to 1.0.
        coverage: The weaker of the two legs' coverage, 0.0 to 1.0. A pair is
            only as well evidenced as its thinner side, and a reader filtering
            on evidence wants the floor rather than the average. It is the same
            quantity `fbe.bias.apply_filters` gates on.
        tradeable: Whether the engine would have backed this view on the day.
        blockers: Why it would not have, as `fbe.bias.BLOCKERS` names them, and
            empty when it would have. These two are copied because
            `apply_filters` never changes a direction, a spread or a conviction:
            a pair blocked on cost or inside a news blackout keeps its HIGH, and
            a row without them cannot tell a call the engine would have taken
            from one it refused. Pooled, they put views that were never
            actionable into the headline hit rate for the band.
        config_digest: The digest of the config that produced the bias.
            Cross-sectional scores depend on the weights that made them, so
            rows from two digests are not poolable without saying so. The
            digest also covers ``horizon_days``, which is the window ``move``
            is measured over, so a later re-identification of it changes what
            these rows mean and the digest is what says which is which.
        generated_at: When the run that produced the bias was made. A report
            backfilled weeks after its as-of read macro series that had been
            revised since, so it is not a forward call however clean the row
            looks. Carried rather than judged: whether such a row belongs in a
            sample is the reader's decision.
        opened_at: The first session after ``asof`` that had a fixing. The move
            is measured from here.
        closed_at: The last session inside the horizon that had a fixing. The
            horizon is ``horizon_days`` calendar days from the as-of, not
            sessions: about seven of them at the packaged ten. `fbe.bias` uses
            the same number as trading days when it scales an ATR, so the two
            windows are not the same length and a reader comparing a realised
            move against an expected one should know it.
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
    tradeable: bool
    blockers: tuple[str, ...]
    config_digest: str
    generated_at: datetime
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


SESSION_GAP_DAYS = 4
"""How far the window's edges may sit from the as-of and the horizon.

The market is shut two days in seven and a long weekend is three, so a window
whose first fixing is a day or two after the as-of, or whose last is a day or
two before the horizon, is a normal week rather than a hole. Wider than that and
the move being labelled with the horizon was measured over something shorter,
which is the same defect as reporting an unknown outcome as a flat one: the
number is in range, it is stamped with a window it did not cover, and nothing
downstream can tell.

Four rather than three, so a Christmas or an Easter run does not trip it. This
is a tolerance on the data, not a property of the market, and a calendar of
trading days would replace it.
"""


def _problem(asof: date | None, pair: str | None, kind: str, detail: str) -> str:
    """One gap, written so the front of the line is the same shape every time.

    Args:
        asof: The run the gap belongs to, or ``None`` for one about the
            directory rather than about a run.
        pair: The pair, or ``None`` when the gap covers a whole report.
        kind: A ``noun:state`` marker, as `fbe.bias.BLOCKERS` writes them, so a
            reader can group the lines without reading the prose.
        detail: The sentence a person acts on.

    Returns:
        ``"<asof> <pair> <kind>: <detail>"``, with the absent parts written as
        ``-``. A fixed shape because the alternative is four sentence forms
        distinguishable only by wording, and a caller wanting to know which
        pairs went missing would be parsing English to find out.

    """
    return f"{asof or '-'} {pair or '-'} {kind}: {detail}"


def _unmeasurable(
    asof: date,
    horizon: date,
    window: Sequence[date],
    rates: Mapping[date, Mapping[str, float]],
    horizon_days: int,
) -> str | None:
    """Say why this run's move cannot be measured, or ``None`` when it can.

    Args:
        asof: The report's as-of date.
        horizon: The far edge of the window, ``horizon_days`` after it.
        window: The sessions inside ``(asof, horizon]``, in order.
        rates: Every session given, so the last one can be named.
        horizon_days: The configured window, for the message.

    Returns:
        One sentence, or ``None`` when the window is complete enough to measure
        over. Four different answers rather than one, because they have four
        different fixes and a reader told only "the rates do not reach the
        horizon" would wait for data that had already arrived.

    Note:
        The rule is that the window has to be covered, not merely touched. A
        table holding one session just after the as-of and another a month
        later reaches past the horizon and spans nothing: taken as a window it
        reports a move of exactly zero, which is a finding, for an outcome
        nobody can see. That is the case this exists for.

        A report whose horizon falls on a day the market does not fix is
        refused until a later session arrives, normally the next working day.
        That is deliberate. Nothing in a table of fixings distinguishes "the
        market was shut" from "the data stops here", and a late row is worth
        more than a short one labelled as a full window.

    """
    latest = max(rates, default=None)
    if not window:
        return _problem(
            asof,
            None,
            "window:empty",
            f"no session given falls inside the {horizon_days} days after the "
            f"as-of, ending {horizon}"
            + (f"; the sessions run to {latest}" if latest is not None else ""),
        )
    if latest is None or latest < horizon:
        return _problem(
            asof,
            None,
            "window:open",
            f"the sessions stop at {latest}, before the {horizon_days}-day "
            f"horizon ending {horizon}, so the outcome has not happened yet. "
            "This is not a flat result.",
        )
    if window[0] == window[-1]:
        return _problem(
            asof,
            None,
            "window:thin",
            f"only one session, {window[0]}, falls inside the {horizon_days} "
            "days after the as-of, so there is no move to measure over it",
        )
    if (window[0] - asof).days > SESSION_GAP_DAYS:
        return _problem(
            asof,
            None,
            "window:late",
            f"the first session inside the window is {window[0]}, "
            f"{(window[0] - asof).days} days after the as-of, so the move "
            "would start well after the bias was published",
        )
    if (horizon - window[-1]).days > SESSION_GAP_DAYS:
        return _problem(
            asof,
            None,
            "window:short",
            f"the last session inside the window is {window[-1]}, "
            f"{(horizon - window[-1]).days} days before the horizon "
            f"{horizon}, so a move measured to it would be stamped with a "
            "window it did not cover",
        )
    return None


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

            A session where the market published no fixing omits the pair
            rather than mapping it to ``None``. That is what the type says, and
            it is what `fbe.datasources.prices.PricesSource.spot` means by
            returning ``None``: the absence is the answer, and written into the
            table it would reach the rate check as a value.

            Two inherited behaviours worth knowing, both from
            `fbe.risk.convert_rate`. A rate of exactly 1.0 between two
            different currencies is refused as an unpopulated placeholder,
            which was written for sizing and here costs a genuine parity fixing
            its session. And a pair that cannot be resolved raises rather than
            returning nothing, which this catches per pair so one unserved
            cross does not cost the other twenty-seven their rows.
        scoring: Supplies ``horizon_days``, the window the move is measured
            over, in calendar days rather than sessions: ten of them is about
            seven fixings. Defaults to `ScoringConfig()`, which is the packaged
            value rather than the operator's; a caller holding a config should
            pass ``load_config().scoring``, because a window that does not
            match the one the engine scored for compares two different
            questions.

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
    refusal = _unmeasurable(report.asof, horizon, window, rates, settings.horizon_days)
    if refusal is not None:
        return ForwardJoin(problems=(refusal,))

    opened_at, closed_at = window[0], window[-1]
    coverage = {score.currency: score.coverage for score in report.currencies}
    rows: list[ForwardRow] = []
    problems: list[str] = []
    for bias in sorted(report.pairs, key=lambda row: row.pair):
        if bias.pair != bias.base + bias.quote:
            # The rate is built from the legs and the row is labelled with the
            # pair. A record where the three disagree produces a move that is
            # the inverse of the one the label claims, in range, with nothing
            # downstream able to tell. `data/reports/` is committed and
            # hand-editable, so this is reachable without a code defect.
            problems.append(
                _problem(
                    report.asof,
                    bias.pair,
                    "pair:legs",
                    f"the record names {bias.base}/{bias.quote} as the legs, "
                    f"which do not spell {bias.pair}, so the label and the "
                    "price would describe different pairs",
                )
            )
            continue
        try:
            open_rate = convert_rate(bias.base, bias.quote, rates[opened_at])
            close_rate = convert_rate(bias.base, bias.quote, rates[closed_at])
        except MissingRateError as error:
            problems.append(
                _problem(
                    report.asof,
                    bias.pair,
                    "rate:absent",
                    "no rate could be built from the sessions given, so the "
                    f"move is unknown rather than zero ({error})",
                )
            )
            continue
        if bias.base not in coverage or bias.quote not in coverage:
            problems.append(
                _problem(
                    report.asof,
                    bias.pair,
                    "coverage:absent",
                    "the report carries no score for one of its legs, so how "
                    "well evidenced the bias was cannot be stated",
                )
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
                tradeable=bias.tradeable,
                blockers=tuple(bias.blockers),
                config_digest=report.config_digest,
                generated_at=report.generated_at,
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

        Two files describing the same as-of are refused the same way, for the
        opposite reason: joined, that morning's cross-section is counted twice
        and weights double in everything downstream. `data/reports/` is
        committed and hand-editable, so a stray copy is reachable without a
        code defect, and its body may differ from the original's.

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
    # Sorted for the problem lines, which come back in the order the files were
    # read. The rows' own order does not depend on it: the as-of in the file is
    # what orders those, and a name can disagree with the body it holds.
    sidecars = sorted(directory.glob(SIDECAR_GLOB))
    if not sidecars:
        return ForwardJoin(
            problems=(
                _problem(
                    None, None, "directory:empty", f"{directory} holds no reports."
                ),
            )
        )
    seen: dict[date, str] = {}
    for sidecar in sidecars:
        try:
            report = load_report(sidecar)
        except (ValueError, OSError) as error:
            problems.append(
                _problem(
                    None,
                    None,
                    "report:unreadable",
                    f"{sidecar.name} could not be read as a report, so that day "
                    f"is missing from the record rather than absent from it: "
                    f"{error}",
                )
            )
            continue
        if report.asof in seen:
            # Refused rather than deduped. Two files describing one run may
            # hold different bodies, and nothing here can pick the right one.
            # Joined, the day is counted twice and weights one morning's
            # cross-section double in every figure computed downstream.
            problems.append(
                _problem(
                    report.asof,
                    None,
                    "report:duplicated",
                    f"{sidecar.name} describes a run already read from "
                    f"{seen[report.asof]}, so it is left out rather than "
                    "counted twice. Remove one of the two.",
                )
            )
            continue
        seen[report.asof] = sidecar.name
        joined = join_report(report, rates, scoring=scoring)
        rows.extend(joined.rows)
        problems.extend(joined.problems)
    # By as-of only, and the sort is stable, so the pair order each report was
    # given above survives it. Sorting by both here would make that one
    # redundant, and a redundant guarantee is one nothing can test: either
    # could then be dropped and the other would hide it.
    rows.sort(key=lambda row: row.asof)
    return ForwardJoin(rows=tuple(rows), problems=tuple(problems))
