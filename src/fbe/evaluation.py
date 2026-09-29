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
from math import fsum
from typing import TYPE_CHECKING

from fbe.config import ScoringConfig
from fbe.journal import (
    EVIDENCE_THRESHOLD_TRADES,
    TradeRecord,
    hit_rate_interval,
)
from fbe.report import SIDECAR_GLOB, load_report
from fbe.risk import MissingRateError, convert_rate
from fbe.types import Conviction, Direction

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from fbe.types import BiasReport

__all__ = [
    "AGAINST",
    "AGREEMENT_BROAD",
    "AGREEMENT_NARROW",
    "ALIGNED",
    "Evaluation",
    "ForwardRow",
    "ForwardJoin",
    "GroupStats",
    "evaluate",
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


AGREEMENT_BROAD: str = "agreement at or above the threshold"
"""Label for rows whose pillar agreement reached `ScoringConfig.min_agreement`."""

AGREEMENT_NARROW: str = "agreement below the threshold"
"""Label for rows whose pillar agreement fell short of it."""

ALIGNED: str = "traded with the bias"
"""Label for journal records the owner marked `agreed_with_bias`."""

AGAINST: str = "traded against the bias"
"""Label for journal records taken the other way.

Roadmap question 11 ruled that the engine counts these rather than warning
louder in the moment. A trader who does better against the engine than with it
is a finding, and it is invisible in a total.
"""


@dataclass(frozen=True, slots=True)
class GroupStats:
    """One bucket of the record, and what it does and does not say.

    Attributes:
        label: How the bucket is named in the output. A conviction band, a
            direction, one of the two agreement labels, or one of the two
            journal alignment labels.
        observations: Rows in the bucket that made a call. A row whose spread
            is exactly zero leaned nowhere and is counted nowhere here: there
            is nothing for it to have been right about.
        hits: Observations where the move went the way the call pointed.
            ``observations`` is not ``hits`` plus the misses: a move of exactly
            zero is neither, which is the rule `ConvictionStats` applies to a
            trade closed at breakeven.
        hit_rate: ``hits / observations``, in ``0..1``.
        hit_rate_low: Lower end of the Wilson interval around it at
            `fbe.journal.HIT_RATE_CONFIDENCE`, from
            `fbe.journal.hit_rate_interval`. The same implementation the
            journal uses, because two intervals over the same kind of rate will
            disagree and the disagreement will not be visible.
        hit_rate_high: Upper end of the same interval.
        below_evidence_threshold: True when ``observations`` is under
            `fbe.journal.EVIDENCE_THRESHOLD_TRADES`, so the bucket is a record
            of what happened rather than evidence about what will. On a forward
            record that began this month every bucket carries this, which is
            why it is a flag the renderer prints as the normal case rather than
            an alarm.
        avg_move: Mean realised move in the direction the call pointed, as a
            fraction rather than a percentage. Positive means the calls were
            right on average by that much; negative means they were wrong on
            average. Measured in the call's direction rather than raw, because
            a right long and a right short cancel in a raw average and report a
            model that predicted nothing.

    """

    label: str
    observations: int
    hits: int
    hit_rate: float
    hit_rate_low: float
    hit_rate_high: float
    below_evidence_threshold: bool
    avg_move: float


@dataclass(frozen=True, slots=True)
class Evaluation:
    """What the forward record and the journal say, and what they do not.

    Attributes:
        rows: Rows the evaluation was given, including any that made no call.
        called: Rows that leaned one way or the other, which is the denominator
            every hit rate below is built from. Reported beside ``rows`` so a
            reader can see how many were dropped for having no lean.
        by_conviction: One bucket per conviction band present, strongest first,
            `Conviction.NONE` included as the control.
        by_direction: One bucket per recorded direction present.
        by_agreement: Up to two buckets, split at `ScoringConfig.min_agreement`.
        by_alignment: Up to two buckets over closed journal records, split on
            `fbe.journal.TradeRecord.agreed_with_bias`. Empty when the journal
            holds no closed record, which is the normal state of a fresh clone
            and is different from a journal whose trades all lost.
        digests: Distinct config digests behind the rows, in first-seen order.
        separating: Labels of the buckets whose interval sits entirely above a
            half **and** which clear the evidence threshold. Empty is the
            honest answer for this record today and for some time.

    The question this answers is whether the model separated anything, and the
    answer it is built to be able to give is no. A bucket is only listed in
    ``separating`` when its whole interval is above chance and it holds enough
    observations to be evidence: Wilson's lower end at five wins from five
    trades is about 0.57, so separation judged on the interval alone would
    announce a finding from five rows.

    Nothing here is a claim about what the model will do. These are counts over
    what it did, with the width of each one stated beside it.

    """

    rows: int = 0
    called: int = 0
    by_conviction: tuple[GroupStats, ...] = ()
    by_direction: tuple[GroupStats, ...] = ()
    by_agreement: tuple[GroupStats, ...] = ()
    by_alignment: tuple[GroupStats, ...] = ()
    digests: tuple[str, ...] = ()
    separating: tuple[str, ...] = ()

    @property
    def mixed_digests(self) -> bool:
        """Whether the rows came from more than one configuration.

        Cross-sectional scores depend on the weights that made them, so rows
        from two digests are not comparable without saying so. They are pooled
        rather than split, because splitting a record this young leaves every
        bucket below the point of being reportable, and the mixture is named at
        the top of the output instead.
        """
        return len(self.digests) > 1

    @property
    def shows_no_separation(self) -> bool:
        """Whether nothing in the record separates from chance.

        True when ``separating`` is empty, which includes the empty record.
        This is the conclusion `docs/roadmap.md` Phase 6 requires the
        evaluation to be able to reach: an analysis with no way to say "no" is
        not an analysis, and it is the answer a young record should give.
        """
        return not self.separating


def _called(row: ForwardRow) -> Direction | None:
    """Which way a row leaned, from the sign of its spread.

    Args:
        row: A joined row.

    Returns:
        `Direction.LONG` for a positive spread, `Direction.SHORT` for a
        negative one, and ``None`` for exactly zero, which leaned nowhere.

    The spread rather than `ForwardRow.direction`, and the two agree wherever
    the engine graded a direction at all: `fbe.bias.direction_for` reads the
    same sign and only withholds a direction inside the low threshold. The
    difference is the `Conviction.NONE` control, whose direction is
    `Direction.NEUTRAL` by construction. Judged on direction it has nothing to
    have been right about and stops being a control, which is the one thing
    criterion 3 needs it to be.

    """
    if row.spread > 0.0:
        return Direction.LONG
    if row.spread < 0.0:
        return Direction.SHORT
    return None


def _hit(row: ForwardRow, call: Direction) -> bool:
    """Whether the move went the way the call pointed.

    Args:
        row: A joined row.
        call: The lean, from `_called`.

    Returns:
        True when the move agreed with the call, False otherwise.

        A move of exactly zero is False rather than a third answer. It counts
        in ``observations`` and not in ``hits``, which is how `ConvictionStats`
        counts a trade closed exactly at breakeven, and the two reports
        agreeing about the same kind of edge case is worth more than a
        distinction neither of them can act on. A three-valued answer was
        written here first and removed: nothing downstream could tell it from
        False, so it was a claim in a docstring rather than behaviour.

    """
    return (row.move > 0.0) if call is Direction.LONG else (row.move < 0.0)


def _signed(row: ForwardRow, call: Direction) -> float:
    """Return the move in the direction the call pointed, as a fraction.

    Positive means the call was right by that much. A short that fell one
    percent and a long that rose one percent both come back as ``+0.01``, so
    they reinforce rather than cancel.
    """
    return row.move if call is Direction.LONG else -row.move


def _stats(label: str, outcomes: Sequence[tuple[bool, float]]) -> GroupStats:
    """Build one bucket from its outcomes.

    Args:
        label: The bucket's name in the output.
        outcomes: ``(hit, signed_move)`` per observation.

    Returns:
        The bucket. Never called with an empty sequence: a bucket nothing fell
        into is left out of the report rather than printed at zero, because a
        zero hit rate over zero observations is a number that reads like a
        finding.

    """
    observations = len(outcomes)
    hits = sum(1 for hit, _ in outcomes if hit)
    low, high = hit_rate_interval(hits, observations)
    return GroupStats(
        label=label,
        observations=observations,
        hits=hits,
        hit_rate=hits / observations,
        hit_rate_low=low,
        hit_rate_high=high,
        below_evidence_threshold=observations < EVIDENCE_THRESHOLD_TRADES,
        avg_move=fsum(move for _, move in outcomes) / observations,
    )


def _grouped(
    buckets: Mapping[str, list[tuple[bool, float]]],
    order: Sequence[str],
) -> tuple[GroupStats, ...]:
    """Turn the collected buckets into stats, in the given label order."""
    return tuple(_stats(label, buckets[label]) for label in order if buckets[label])


def evaluate(
    rows: Sequence[ForwardRow],
    trades: Sequence[TradeRecord] = (),
    *,
    scoring: ScoringConfig | None = None,
) -> Evaluation:
    """Report what the forward record and the journal say, and how loudly.

    Args:
        rows: Joined rows from `join_report` or `join_reports`, in any order.
            Rows whose spread is exactly zero made no call and are counted in
            ``Evaluation.rows`` and nowhere else.
        trades: Journal records, normally from `fbe.journal.load`. Only closed
            records carrying an ``r_multiple`` are counted: an open trade has
            no outcome to attribute yet, and counting it as a loss would be a
            number where an absence belongs. Defaults to none, which leaves the
            alignment split out of the report rather than printing zeros.
        scoring: Supplies ``min_agreement``, the threshold the agreement
            buckets split at. Defaults to `ScoringConfig`'s packaged values.

    Returns:
        An `Evaluation`. Every rate carries its count and its interval, every
        bucket under `fbe.journal.EVIDENCE_THRESHOLD_TRADES` observations is
        marked, and ``separating`` names only the buckets that clear both
        chance and that threshold.

        An empty record comes back as an empty evaluation rather than as zeros:
        `Evaluation.shows_no_separation` is true for it, which is correct, and
        every bucket tuple is empty, which says there was nothing to bucket.

    Units and signs: ``avg_move`` is a fraction of the rate, not a percentage,
    and is measured in the direction the call pointed, so positive means the
    calls were right. ``hit_rate`` and both interval ends are in ``0..1``.

    Nothing here is a claim about what the model will do next. It counts what
    it did, and it is built so that "nothing here separates from chance" is a
    result it can return rather than a case it cannot express.

    """
    config = scoring if scoring is not None else ScoringConfig()

    conviction: dict[str, list[tuple[bool, float]]] = {
        band.value: [] for band in Conviction
    }
    direction: dict[str, list[tuple[bool, float]]] = {
        way.value: [] for way in Direction
    }
    agreement: dict[str, list[tuple[bool, float]]] = {
        AGREEMENT_BROAD: [],
        AGREEMENT_NARROW: [],
    }
    digests: list[str] = []
    called = 0

    for row in rows:
        if row.config_digest not in digests:
            digests.append(row.config_digest)
        call = _called(row)
        if call is None:
            continue
        called += 1
        outcome = (_hit(row, call), _signed(row, call))
        conviction[row.conviction.value].append(outcome)
        direction[row.direction.value].append(outcome)
        broad = row.agreement >= config.min_agreement
        agreement[AGREEMENT_BROAD if broad else AGREEMENT_NARROW].append(outcome)

    alignment: dict[str, list[tuple[bool, float]]] = {ALIGNED: [], AGAINST: []}
    for record in trades:
        if record.r_multiple is None:
            continue
        label = ALIGNED if record.agreed_with_bias else AGAINST
        # A trade closed exactly at breakeven counts in the denominator and not
        # in the numerator, which is what `ConvictionStats` does with the same
        # trade and what `_hit` does with a move of exactly zero.
        alignment[label].append((record.r_multiple > 0.0, record.r_multiple))

    by_conviction = _grouped(conviction, [band.value for band in Conviction])
    by_direction = _grouped(direction, [way.value for way in Direction])
    by_agreement = _grouped(agreement, [AGREEMENT_BROAD, AGREEMENT_NARROW])
    by_alignment = _grouped(alignment, [ALIGNED, AGAINST])

    return Evaluation(
        rows=len(rows),
        called=called,
        by_conviction=by_conviction,
        by_direction=by_direction,
        by_agreement=by_agreement,
        by_alignment=by_alignment,
        digests=tuple(digests),
        separating=tuple(
            entry.label
            for entry in (*by_conviction, *by_direction, *by_agreement, *by_alignment)
            if entry.hit_rate_low > 0.5 and not entry.below_evidence_threshold
        ),
    )
