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

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from fbe.config import Config, ScoringConfig
from fbe.report import SIDECAR_GLOB, load_report, ranked_pairs
from fbe.risk import MissingRateError, convert_rate
from fbe.types import Conviction, Direction, PillarName

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from fbe.types import BiasReport, Observation, PairBias

__all__ = [
    "CROSS_SECTION_NOTE",
    "DEFAULT_WEIGHT_STEP",
    "ForwardRow",
    "ForwardJoin",
    "WeightMove",
    "WeightSensitivity",
    "join_report",
    "join_reports",
    "weight_sensitivity",
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


CROSS_SECTION_NOTE = (
    "These figures describe how far one run's cross-section moves when one "
    "weight moves, and say nothing about returns. A pillar whose weight can "
    "be moved without disturbing the ranking is not thereby useless, and one "
    "that disturbs it a lot is not thereby right: both are statements about "
    "this morning's eight currencies and the 28 pairs built from them, not "
    "about whether any of it was correct."
)
"""What the sensitivity figures are, and the two conclusions they do not support.

Carried on `WeightSensitivity` rather than left in this docstring, because the
figures are read by someone deciding whether to propose a weight change and a
caveat in the source is one they never see. Both wrong readings are named:
either alone invites the other, and a reader given only "a small move does not
mean the pillar is useless" concludes that a large move means it is right.
"""

DEFAULT_WEIGHT_STEP = 0.05
"""How far a pillar weight is moved, in weight units, absolute.

Absolute rather than a fraction of the weight. The only quantified precedent in
this repository is `docs/scoring-spec.md`'s own comparison, "6.07% of tiers for
a single 0.05 shift between two pillar weights", and a second convention beside
it would make the two figures look comparable when they are not.

The cost of absolute is that the seven moves are not comparable with each
other: 0.05 on ``MONETARY``'s 0.30 is a sixth of it and 0.05 on ``RISK``'s 0.10
is half. `WeightMove` carries ``weight_before`` and ``weight_after`` so a
reader can see the relative size of the move that produced each count, rather
than inferring it from a weight they have to look up.

A parameter on `weight_sensitivity`, so this is the default and not the rule.
"""


@dataclass(frozen=True, slots=True)
class WeightMove:
    """One pillar's weight moved one way, and what the ranking did.

    Attributes:
        pillar: The pillar whose weight was moved.
        step: How far it moved, in weight units, signed. Negative is down.
        weight_before: The weight in the configuration handed in.
        weight_after: ``weight_before + step``. Carried rather than recomputed
            so the relative size of the move is on the row: a reader comparing
            two pillars needs both numbers and the step alone gives neither.
        weights: The whole perturbed vector, summing to 1.0. A copy; writing
            through it reaches nothing.
        directions_changed: How many of the run's pairs came back with a
            different `fbe.types.Direction`. A flip is the loudest thing a
            weight can do, because it is the one that puts the trader on the
            other side of the pair.
        convictions_changed: How many came back in a different
            `fbe.types.Conviction` band, counted in either direction. A band
            change moves position size through `fbe.risk`, so it is not
            cosmetic even where the direction held.
        largest_rank_move: The furthest any single pair travelled in the
            ranking, in places, against `fbe.report.ranked_pairs`. Zero means
            the order was untouched.
        largest_rank_move_pair: Which pair travelled that far, or ``None`` when
            nothing moved. Named so a reader can look at the pair rather than
            at the number.

    """

    pillar: PillarName
    step: float
    weight_before: float
    weight_after: float
    weights: Mapping[PillarName, float]
    directions_changed: int
    convictions_changed: int
    largest_rank_move: int
    largest_rank_move_pair: str | None = None


@dataclass(frozen=True, slots=True)
class WeightSensitivity:
    """A whole sensitivity run: every pillar moved both ways.

    Attributes:
        asof: The as-of of the run that was perturbed. A figure with no date
            outlives the cross-section it describes.
        step: The step the run used, so the output states the number rather
            than assuming the reader passed the default.
        baseline: The pairs as rescored at the original weights, in the
            report's own order.
        baseline_matches_report: Whether ``baseline`` reproduced the report it
            started from. Always true on a returned result, because a run that
            could not reproduce its own starting point raises instead. It is a
            field rather than an assumption so a caller writing the result out
            records which it was.
        moves: Fourteen rows, seven pillars up and down, in `fbe.types.
            PillarName` order with the upward move first.
        problems: One sentence per perturbation that could not be made. Never
            silently empty: a pillar missing from ``moves`` with nothing said
            about it reads as a pillar nothing moved.
        note: `CROSS_SECTION_NOTE`, travelling with the figures.

    """

    asof: date
    step: float
    baseline: tuple[PairBias, ...] = ()
    baseline_matches_report: bool = False
    moves: tuple[WeightMove, ...] = ()
    problems: tuple[str, ...] = ()
    note: str = CROSS_SECTION_NOTE


def _perturbed_weights(
    weights: Mapping[PillarName, float], pillar: PillarName, step: float
) -> dict[PillarName, float]:
    """One weight moved by ``step``, the other six renormalised around it.

    Args:
        weights: The configured vector, which is read and not written.
        pillar: The weight to move.
        step: How far, signed, in weight units.

    Returns:
        A new dict summing to 1.0, in which ``pillar`` carries
        ``weights[pillar] + step`` and every other weight is scaled by
        ``(1 - moved) / (1 - before)``. Scaling the rest preserves their
        proportions to each other, so the measurement isolates the one weight
        rather than mixing in a second re-weighting among the six.

    Raises:
        ValueError: When the moved weight would leave ``0.0 .. 1.0``, or when
            the other six sum to zero so there is nothing to renormalise.
            Clipping to the boundary instead would report a move of 0.10 as a
            move of 0.5 and attribute the ranking's behaviour to the larger
            number, which is a wrong figure that looks right.

    """
    before = weights[pillar]
    moved = before + step
    if not 0.0 <= moved <= 1.0:
        raise ValueError(
            f"moving {pillar.value} by {step:+} from {before} lands at {moved}, "
            "outside 0.0 to 1.0. A weight outside the band is not a weighting, "
            "and clipping it would label the result with a step that was not "
            "taken."
        )
    rest = 1.0 - before
    if rest <= 0.0:
        raise ValueError(
            f"{pillar.value} already carries the whole weight, so moving it by "
            f"{step:+} leaves nothing to renormalise against."
        )
    scale = (1.0 - moved) / rest
    return {
        name: (moved if name is pillar else weight * scale)
        for name, weight in weights.items()
    }


def _rescore(
    observations: Sequence[Observation],
    config: Config,
    weights: Mapping[PillarName, float],
    asof: date,
) -> tuple[PairBias, ...]:
    """Run the real pipeline at one weight vector.

    Args:
        observations: The run's own observations. Nothing is fetched here: a
            refetch would read revised series and fold a data change into a
            figure labelled as a weight change.
        config: The configuration to copy. Never modified.
        weights: The vector to score at, already summing to 1.0.
        asof: The date the run represents, so staleness is measured against
            the morning the report was built rather than against today.

    Returns:
        The 28 pair rows, in ``universe.ALL_PAIRS`` order.

    The two functions are `fbe.scoring.score_currencies` and
    `fbe.bias.build_pair_biases`, called rather than reimplemented, because a
    second copy of the composite or of the conviction ladder would grade this
    run differently from the report it is being compared against and every
    difference would be attributed to the weight.

    No event horizon guard is passed. `fbe.bias.build_pair_biases` then applies
    no 24-hour conviction cap, which is the right reading of a past run being
    rescored: the cap depends on a calendar the report does not carry, and
    assuming an event would cap convictions the original run did not.

    """
    from fbe.bias import build_pair_biases
    from fbe.pillars import default_pillars
    from fbe.scoring import score_currencies

    scoring = replace(config.scoring, weights=dict(weights))
    scores = score_currencies(observations, default_pillars(scoring), scoring, asof)
    return tuple(build_pair_biases(scores, replace(config, scoring=scoring), asof))


def _counts(
    baseline: Sequence[PairBias], moved: Sequence[PairBias]
) -> tuple[int, int, int, str | None]:
    """Count the three figures, against the unperturbed run.

    Args:
        baseline: The pairs at the configured weights.
        moved: The same pairs rescored at the perturbed weights.

    Returns:
        Directions changed, convictions changed, the largest rank move in
        places, and the pair that made it, or ``None`` when the order held.

    Raises:
        KeyError: When the two runs do not hold the same pairs. Comparing a
            27-pair run against a 28-pair one by name would silently drop the
            missing pair from every count and report a smaller movement than
            happened.

    """
    before = {row.pair: row for row in baseline}
    after = {row.pair: row for row in moved}
    missing = set(before) ^ set(after)
    if missing:
        raise KeyError(
            "the perturbed run does not hold the same pairs as the baseline: "
            + ", ".join(sorted(missing))
        )

    directions = sum(
        1 for pair, row in before.items() if row.direction is not after[pair].direction
    )
    convictions = sum(
        1
        for pair, row in before.items()
        if row.conviction is not after[pair].conviction
    )

    before_rank = {row.pair: i for i, row in enumerate(ranked_pairs(baseline))}
    after_rank = {row.pair: i for i, row in enumerate(ranked_pairs(moved))}
    moves = {pair: abs(before_rank[pair] - after_rank[pair]) for pair in before_rank}
    largest = max(moves.values(), default=0)
    # Ties broken by pair name so the reported pair does not depend on dict
    # order, which would make the same run name a different pair on a rerun.
    worst = (
        min(pair for pair, places in moves.items() if places == largest)
        if largest
        else None
    )
    return directions, convictions, largest, worst


def weight_sensitivity(
    report: BiasReport,
    observations: Sequence[Observation],
    *,
    config: Config | None = None,
    step: float = DEFAULT_WEIGHT_STEP,
) -> WeightSensitivity:
    """Move each pillar weight in turn and report how far the ranking travelled.

    Args:
        report: A run already written and read back. Its pairs are the thing
            every count is measured against.
        observations: The observations that run was scored from, handed over
            rather than fetched. A `fbe.types.BiasReport` carries neither its
            observations nor a pointer to them, so the caller supplies them:
            the live path holds them from the run it just made, and a fixture
            commits them beside the report. Refetching would read revised macro
            series and fold a data change into a figure labelled as a weight
            change, which is the one thing this measurement cannot survive.
        config: The configuration the report was produced under. Defaults to
            the packaged `fbe.config.Config`; a caller holding the operator's
            should pass it, because perturbing weights the run did not use
            measures a vector nobody scored with. Never modified: every
            perturbation is scored through a copy.
        step: How far to move each weight, in weight units, absolute and
            unsigned. Each pillar is moved up by it and down by it. Defaults to
            `DEFAULT_WEIGHT_STEP`.

    Returns:
        Fourteen rows where the step could be applied, one per pillar per
        direction, plus a sentence for every pillar where it could not. Each
        row carries the perturbed vector and the three counts against the
        unperturbed run.

    Raises:
        ValueError: When ``step`` is not above zero, since a step of zero
            perturbs nothing and would report fourteen rows of honest-looking
            zeroes. And when rescoring at the configured weights does not
            reproduce ``report``, because every count in the output is a
            difference against that run: if the starting point cannot be
            rebuilt, the differences carry whatever else moved, most likely the
            observations being from a different morning than the report.

    What this measures, and the two things it does not:

    A weight that can move without disturbing the ranking is a weight this
    morning's cross-section was not resting on. That is not evidence that the
    pillar is unnecessary, and a weight that does disturb the ranking is not
    thereby wrong. `CROSS_SECTION_NOTE` states both and travels on the result.

    It is also not a search for a better weight vector. Choosing weights by how
    stable they make the output fits to the shape of the data, which
    `docs/roadmap.md` rules out until there is a forward record to fit against,
    and this function reports movement rather than ranking the vectors it
    produced. Nothing here writes a weight back.

    """
    if step <= 0.0:
        raise ValueError(
            f"step must be above zero, got {step}. A step of zero moves no "
            "weight and would report every pillar as having changed nothing."
        )

    run_config = config if config is not None else Config()
    weights = run_config.scoring.weights

    baseline = _rescore(observations, run_config, weights, report.asof)
    original = {row.pair: row for row in report.pairs}
    reproduced = len(baseline) == len(original) and all(
        row.pair in original
        and row.spread == original[row.pair].spread
        and row.direction is original[row.pair].direction
        and row.conviction is original[row.pair].conviction
        for row in baseline
    )
    if not reproduced:
        raise ValueError(
            f"rescoring the {report.asof} run at its own weights does not "
            "reproduce it, so nothing measured against it would mean what it "
            "says. The usual cause is observations from a different run than "
            "the report, or a configuration whose scoring section is not the "
            "one the report was produced under."
        )

    moves: list[WeightMove] = []
    problems: list[str] = []
    for pillar in PillarName:
        for signed in (step, -step):
            try:
                perturbed = _perturbed_weights(weights, pillar, signed)
            except ValueError as error:
                problems.append(str(error))
                continue
            directions, convictions, largest, worst = _counts(
                baseline, _rescore(observations, run_config, perturbed, report.asof)
            )
            moves.append(
                WeightMove(
                    pillar=pillar,
                    step=signed,
                    weight_before=weights[pillar],
                    weight_after=perturbed[pillar],
                    weights=perturbed,
                    directions_changed=directions,
                    convictions_changed=convictions,
                    largest_rank_move=largest,
                    largest_rank_move_pair=worst,
                )
            )

    return WeightSensitivity(
        asof=report.asof,
        step=step,
        baseline=baseline,
        baseline_matches_report=True,
        moves=tuple(moves),
        problems=tuple(problems),
    )
