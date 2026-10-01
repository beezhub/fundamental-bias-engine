"""Hit rate, interval and separation over the forward record: #286.

`join_report` produces rows and refuses to summarise them, which is what makes
this file possible: the join can be wrong in a visible way rather than inside a
statistic. These tests are about the statistic.

The criterion that shapes everything else is the fifth: the report must be able
to conclude that the model has no edge. A pipeline that can only find patterns
is not a measurement, so the first thing asserted here is that independent
direction and outcome come back as separating nothing, and the rest of the file
is built so that result cannot be produced by accident.

Three decisions the issue left open are pinned here rather than in prose:

* A hit is judged against the sign of the recorded spread, not against
  `Direction`. They agree wherever the engine graded a direction, because
  `bias.direction_for` reads the same sign, and the spread extends the question
  to the `Conviction.NONE` control, whose direction is `NEUTRAL` by
  construction. Without it the control has nothing to be right about and stops
  being a control.
* A move of exactly zero counts in the denominator and not in the numerator,
  matching how `ConvictionStats` counts a trade closed at breakeven.
* Agreement is bucketed at `ScoringConfig.min_agreement`, the engine's own
  threshold, rather than at edges invented here.

Nothing reaches the network. Every row is built in the test.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest

from fbe.config import ScoringConfig
from fbe.evaluation import (
    AGAINST,
    AGREEMENT_BROAD,
    AGREEMENT_NARROW,
    ALIGNED,
    Evaluation,
    ForwardRow,
    GroupStats,
    evaluate,
)
from fbe.journal import EVIDENCE_THRESHOLD_TRADES, TradeRecord
from fbe.types import Conviction, Direction

ASOF = date(2026, 9, 10)
DIGEST = "digest-a"
HORIZON = ScoringConfig().horizon_days


def spaced(index: int, *, every: int = HORIZON) -> date:
    """The as-of of the ``index``-th report in a run spaced ``every`` days apart.

    At the default spacing each report's window ends where the next one starts,
    so every report is an independent window and none overlaps another.
    """
    return ASOF + timedelta(days=every * index)


def row(
    *,
    spread: float,
    move: float,
    conviction: Conviction = Conviction.HIGH,
    agreement: float = 0.9,
    pair: str = "EURUSD",
    asof: date = ASOF,
    digest: str = DIGEST,
    coverage: float = 1.0,
) -> ForwardRow:
    """One joined row, with only the fields the statistics read set on purpose.

    `direction` is derived from the spread rather than passed, because the two
    agreeing is the invariant the hit rule rests on and a fixture free to
    disagree with itself would hide a rule that read the wrong field.
    """
    if conviction is Conviction.NONE:
        direction = Direction.NEUTRAL
    elif spread > 0:
        direction = Direction.LONG
    else:
        direction = Direction.SHORT
    return ForwardRow(
        asof=asof,
        pair=pair,
        direction=direction,
        conviction=conviction,
        spread=spread,
        agreement=agreement,
        coverage=coverage,
        tradeable=True,
        blockers=(),
        config_digest=digest,
        generated_at=datetime(asof.year, asof.month, asof.day, 5, 0, tzinfo=UTC),
        opened_at=asof,
        closed_at=asof,
        open_rate=1.0,
        close_rate=1.0 + move,
        move=move,
    )


def group(stats: Evaluation, label: str) -> GroupStats:
    """The one group carrying ``label``, from wherever in the evaluation it sits."""
    every = (
        *stats.by_conviction,
        *stats.by_direction,
        *stats.by_agreement,
        *stats.by_alignment,
    )
    found = [entry for entry in every if entry.label == label]
    assert len(found) == 1, f"{label} appears {len(found)} times"
    return found[0]


def trade(
    *,
    aligned: bool = True,
    r_multiple: float | None = 1.0,
    index: int = 0,
) -> TradeRecord:
    """One journal record, closed unless ``r_multiple`` is ``None``.

    The fields the split reads are ``agreed_with_bias`` and ``r_multiple``;
    everything else is filled with the plan's own account so the record is
    valid rather than because anything here reads it.
    """
    return TradeRecord(
        trade_id=f"EURUSD-{index:04d}",
        pair="EURUSD",
        direction=Direction.LONG,
        opened_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        entry=1.0850,
        stop=1.0825,
        units=1000.0,
        lots=0.01,
        risk_amount=40.00,
        risk_fraction=0.02,
        account_balance_at_entry=2000.0,
        conviction=Conviction.HIGH,
        base_score=1.2,
        quote_score=-0.4,
        spread_score=1.6,
        config_digest=DIGEST,
        agreed_with_bias=aligned,
        r_multiple=r_multiple,
    )


# ----------------------------------------------------------------------
# Criterion 5, first because nothing else means anything without it
# ----------------------------------------------------------------------


def test_a_record_with_no_signal_in_it_is_reported_as_separating_nothing() -> None:
    """Independent direction and outcome must come back as no separation.

    Four hundred rows whose move is drawn without reference to the spread. A
    hit rate near a half is the right answer and the interval around it
    straddles a half, so no group can claim the record shows the model picking
    winners. An analysis with no way to return this answer is not an analysis,
    and every other test in this file would pass against one.
    """
    generator = random.Random(286)
    rows = [
        row(
            spread=generator.choice((-1.0, 1.0)) * generator.uniform(0.8, 3.0),
            move=generator.choice((-1.0, 1.0)) * generator.uniform(0.001, 0.02),
        )
        for _ in range(400)
    ]

    stats = evaluate(rows)

    assert stats.separating == ()
    assert stats.shows_no_separation is True
    high = group(stats, Conviction.HIGH.value)
    assert high.observations == 400
    assert high.hit_rate_low < 0.5 < high.hit_rate_high


def test_a_record_that_does_separate_says_which_groups_did() -> None:
    """The counterpart, so the previous test is not passing on a stuck answer.

    Two hundred rows where the move always follows the spread. The interval sits
    entirely above a half and the group is named. A function that could only
    ever answer "no separation" would pass the test above and fail here.
    """
    rows = [
        row(
            spread=1.0 if index % 2 else -1.0,
            move=0.01 if index % 2 else -0.01,
            asof=spaced(index // 2),
        )
        for index in range(200)
    ]

    stats = evaluate(rows)

    assert Conviction.HIGH.value in stats.separating
    assert stats.shows_no_separation is False
    high = group(stats, Conviction.HIGH.value)
    assert high.hit_rate == pytest.approx(1.0)
    assert high.hit_rate_low > 0.5


# ----------------------------------------------------------------------
# Criterion 1: the three groupings, each with its count
# ----------------------------------------------------------------------


def test_every_figure_carries_the_number_of_observations_behind_it() -> None:
    """A rate with no denominator is not a finding.

    Three rows at HIGH and one at LOW, so the two buckets cannot be told apart
    by their rates alone: both are 100%, and only the counts say one of them is
    a single observation.
    """
    rows = [
        *(row(spread=1.0, move=0.01) for _ in range(3)),
        row(spread=1.0, move=0.01, conviction=Conviction.LOW),
    ]

    stats = evaluate(rows)

    assert group(stats, Conviction.HIGH.value).observations == 3
    assert group(stats, Conviction.LOW.value).observations == 1
    assert group(stats, Conviction.HIGH.value).hit_rate == pytest.approx(1.0)
    assert group(stats, Conviction.LOW.value).hit_rate == pytest.approx(1.0)


def test_the_hit_rate_is_the_share_of_rows_the_move_agreed_with() -> None:
    """Hand-worked: three of four, with the miss on the other side of the spread."""
    rows = [
        row(spread=1.5, move=0.010),
        row(spread=1.5, move=0.004),
        row(spread=-1.5, move=-0.006),
        row(spread=-1.5, move=0.002),
    ]

    high = group(evaluate(rows), Conviction.HIGH.value)

    assert high.observations == 4
    assert high.hits == 3
    assert high.hit_rate == pytest.approx(0.75)


def test_the_average_move_is_measured_in_the_direction_the_call_pointed() -> None:
    """Otherwise a right short and a right long cancel and the average is zero.

    Two rows, each right by one percent, one long and one short. Averaging the
    raw moves gives nothing; averaging them in the direction of the call gives
    the one percent that is the actual finding.
    """
    rows = [row(spread=2.0, move=0.01), row(spread=-2.0, move=-0.01)]

    high = group(evaluate(rows), Conviction.HIGH.value)

    assert high.avg_move == pytest.approx(0.01)
    assert high.hit_rate == pytest.approx(1.0)


def test_direction_is_grouped_as_recorded_and_not_recomputed() -> None:
    """The engine's own call is what is being evaluated.

    Longs and shorts are reported apart, because a model that is right on one
    side and wrong on the other has a hit rate near a half overall and nothing
    in the headline says which half it got.
    """
    rows = [
        *(row(spread=2.0, move=0.01) for _ in range(3)),
        *(row(spread=-2.0, move=0.01) for _ in range(2)),
    ]

    stats = evaluate(rows)

    assert group(stats, Direction.LONG.value).observations == 3
    assert group(stats, Direction.LONG.value).hit_rate == pytest.approx(1.0)
    assert group(stats, Direction.SHORT.value).observations == 2
    assert group(stats, Direction.SHORT.value).hit_rate == pytest.approx(0.0)


def test_agreement_is_bucketed_at_the_engines_own_threshold() -> None:
    """`ScoringConfig.min_agreement`, not an edge invented here.

    A bucket boundary is a free parameter, and a free parameter chosen to make
    the output look good is the curve fit the roadmap rules out. Reusing the
    threshold the engine already gates on means the two halves are "broad
    enough for the engine" and "not", which is a question the reader can act on.
    """
    config = ScoringConfig()
    rows = [
        row(spread=2.0, move=0.01, agreement=config.min_agreement),
        row(spread=2.0, move=0.01, agreement=config.min_agreement - 0.01),
        row(spread=2.0, move=-0.01, agreement=config.min_agreement - 0.2),
    ]

    stats = evaluate(rows)

    assert group(stats, AGREEMENT_BROAD).observations == 1
    assert group(stats, AGREEMENT_NARROW).observations == 2
    assert group(stats, AGREEMENT_BROAD).hit_rate == pytest.approx(1.0)
    assert group(stats, AGREEMENT_NARROW).hit_rate == pytest.approx(0.5)


def test_the_agreement_bucket_follows_a_configured_threshold_that_moved() -> None:
    """The wire, not the default. A literal 0.6 would pass the test above."""
    moved = ScoringConfig(min_agreement=0.95)
    rows = [row(spread=2.0, move=0.01, agreement=0.9)]

    assert group(evaluate(rows), AGREEMENT_BROAD).observations == 1
    assert group(evaluate(rows, scoring=moved), AGREEMENT_NARROW).observations == 1


# ----------------------------------------------------------------------
# Criterion 2: the interval, computed rather than assumed
# ----------------------------------------------------------------------


def test_every_hit_rate_carries_an_interval_around_it() -> None:
    """Hand-worked against the same Wilson arithmetic the journal uses.

    Seven of ten at 95% is 0.3968 to 0.8922 under Wilson. The point estimate is
    inside it, which the normal approximation does not guarantee at the ends,
    and the interval is more than half the range wide: ten observations say
    almost nothing and the interval is how the output admits it.
    """
    rows = [
        *(row(spread=2.0, move=0.01) for _ in range(7)),
        *(row(spread=2.0, move=-0.01) for _ in range(3)),
    ]

    high = group(evaluate(rows), Conviction.HIGH.value)

    assert high.hit_rate == pytest.approx(0.7)
    assert high.hit_rate_low == pytest.approx(0.3968, abs=5e-4)
    assert high.hit_rate_high == pytest.approx(0.8922, abs=5e-4)
    assert high.hit_rate_low < high.hit_rate < high.hit_rate_high


def test_a_group_that_never_missed_still_has_width() -> None:
    """Five from five is not certainty, and the interval must not say it is.

    This is the case the normal approximation collapses to zero width on, which
    is why `journal.hit_rate_interval` is Wilson and why this reuses it rather
    than computing a second interval that would disagree with the first.
    """
    rows = [row(spread=2.0, move=0.01) for _ in range(5)]

    high = group(evaluate(rows), Conviction.HIGH.value)

    assert high.hit_rate == pytest.approx(1.0)
    assert high.hit_rate_high == pytest.approx(1.0)
    assert high.hit_rate_low < 0.7


def test_the_interval_is_the_journals_interval_and_not_a_second_one() -> None:
    """One implementation, so the two reports cannot disagree about the same rate."""
    from fbe.journal import hit_rate_interval

    rows = [
        *(row(spread=2.0, move=0.01) for _ in range(9)),
        *(row(spread=2.0, move=-0.01) for _ in range(4)),
    ]

    high = group(evaluate(rows), Conviction.HIGH.value)
    low, upper = hit_rate_interval(9, 13)

    assert (high.hit_rate_low, high.hit_rate_high) == (low, upper)


# ----------------------------------------------------------------------
# Criterion 3: the control group
# ----------------------------------------------------------------------


def test_the_pairs_the_engine_declined_are_their_own_group() -> None:
    """`Conviction.NONE` is the control and is reported, not dropped.

    Its direction is `NEUTRAL` by construction, so the hit is judged against the
    spread's sign: the question the control answers is whether the lean the
    engine refused to back would have worked anyway.
    """
    rows = [
        *(row(spread=2.0, move=0.01) for _ in range(4)),
        *(row(spread=0.2, move=0.01, conviction=Conviction.NONE) for _ in range(3)),
        row(spread=-0.2, move=0.01, conviction=Conviction.NONE),
    ]

    stats = evaluate(rows)
    control = group(stats, Conviction.NONE.value)

    assert control.observations == 4
    assert control.hits == 3
    assert all(entry.direction is Direction.NEUTRAL for entry in rows[4:])


def test_a_control_performing_like_the_backed_group_is_visible() -> None:
    """The finding the control exists to make possible.

    Both groups at the same hit rate means the conviction ladder separated
    nothing, and the output has to let a reader see that rather than reporting
    only the backed band.
    """
    rows = [
        *(
            row(spread=2.0, move=0.01 if index % 4 else -0.01, asof=spaced(index))
            for index in range(20)
        ),
        *(
            row(
                spread=0.2,
                move=0.01 if index % 4 else -0.01,
                conviction=Conviction.NONE,
                asof=spaced(20 + index),
            )
            for index in range(20)
        ),
    ]

    stats = evaluate(rows)

    assert group(stats, Conviction.HIGH.value).hit_rate == pytest.approx(0.75)
    assert group(stats, Conviction.NONE.value).hit_rate == pytest.approx(0.75)
    # Neither conviction bucket is named as separating, and not because the
    # rate is low: twenty windows is under the evidence threshold, so the gate
    # holds even at 75%. The agreement bucket pools all forty, from forty
    # spaced reports, and is named, which is the same rows judged where there
    # are enough of them.
    assert Conviction.HIGH.value not in stats.separating
    assert Conviction.NONE.value not in stats.separating
    assert stats.separating == (AGREEMENT_BROAD,)


# ----------------------------------------------------------------------
# Criterion 6: below thirty is a record, not evidence
# ----------------------------------------------------------------------


def test_a_bucket_under_thirty_independent_windows_is_marked() -> None:
    """Every figure on this record for months, so the marking is the normal case.

    Thirty reports spaced a full horizon apart is thirty windows that share no
    day of price, and that is the first count at which a bucket is evidence.
    """
    assert EVIDENCE_THRESHOLD_TRADES == 30
    thin = evaluate([row(spread=2.0, move=0.01, asof=spaced(n)) for n in range(29)])
    thick = evaluate([row(spread=2.0, move=0.01, asof=spaced(n)) for n in range(30)])

    assert group(thin, Conviction.HIGH.value).windows == 29
    assert group(thin, Conviction.HIGH.value).below_evidence_threshold is True
    assert group(thick, Conviction.HIGH.value).windows == 30
    assert group(thick, Conviction.HIGH.value).below_evidence_threshold is False
    assert Conviction.HIGH.value in thick.separating


def test_five_mornings_calling_the_dollar_short_in_a_fortnight_is_one_window() -> None:
    """The early false finding this threshold exists to refuse.

    Five daily reports, each calling the dollar short on all seven dollar pairs,
    in a fortnight the dollar fell. That is 35 rows, every one a hit, and a
    count of rows clears thirty with an interval well above a half. It is one
    bet: the seven pairs share a leg, and five ten-day windows a day apart share
    almost every day of price. Counted as windows it is one, and the output has
    to say it is a record rather than evidence.
    """
    dollar_pairs = {
        "EURUSD": 1.0,
        "GBPUSD": 1.0,
        "AUDUSD": 1.0,
        "NZDUSD": 1.0,
        "USDJPY": -1.0,
        "USDCHF": -1.0,
        "USDCAD": -1.0,
    }
    rows = [
        row(
            spread=2.0 * sign,
            move=0.01 * sign,
            pair=pair,
            asof=ASOF + timedelta(days=day),
        )
        for day in range(5)
        for pair, sign in dollar_pairs.items()
    ]

    stats = evaluate(rows)
    high = group(stats, Conviction.HIGH.value)

    assert high.observations == 35
    assert high.hit_rate_low > 0.5
    assert high.windows == 1
    assert high.below_evidence_threshold is True
    assert stats.separating == ()
    assert stats.shows_no_separation is True


def test_windows_closer_than_the_horizon_count_once_greedily_from_the_earliest() -> (
    None
):
    """Reports nine days apart under a ten day horizon overlap, so not all count.

    Greedy from the earliest: day 0 is taken, day 9 overlaps it, day 18 is far
    enough from day 0 and is taken, day 27 overlaps day 18. Two windows from
    four reports.
    """
    rows = [row(spread=2.0, move=0.01, asof=spaced(n, every=9)) for n in range(4)]

    assert group(evaluate(rows), Conviction.HIGH.value).windows == 2


def test_the_window_spacing_follows_a_configured_horizon() -> None:
    """Read from `ScoringConfig.horizon_days`, not from a literal ten.

    The same four reports nine days apart are four windows under a five day
    horizon, because none of them shares a day of price with the next.
    """
    rows = [row(spread=2.0, move=0.01, asof=spaced(n, every=9)) for n in range(4)]

    stats = evaluate(rows, scoring=ScoringConfig(horizon_days=5))

    assert group(stats, Conviction.HIGH.value).windows == 4
    assert stats.windows == 4


def test_a_marked_bucket_cannot_be_reported_as_separating() -> None:
    """A 100% hit rate over five rows is noise, whatever its interval says.

    Wilson's lower end at five from five is about 0.57, which is above a half,
    so separation judged on the interval alone would announce a finding from
    five observations. The threshold gates it.
    """
    rows = [row(spread=2.0, move=0.01) for _ in range(5)]

    stats = evaluate(rows)
    high = group(stats, Conviction.HIGH.value)

    assert high.hit_rate_low > 0.5
    assert high.below_evidence_threshold is True
    assert stats.separating == ()


# ----------------------------------------------------------------------
# Criterion 7: two digests are not pooled silently
# ----------------------------------------------------------------------


def test_rows_from_two_digests_are_pooled_and_the_mixture_is_named() -> None:
    """Named rather than split, and the reason is on the issue and in the code.

    Splitting a record this young by digest leaves every bucket below the point
    of being reportable. Pooling is the usable answer and the mixture is stated
    at the top of the output, which is what the criterion asks for in its second
    form.
    """
    rows = [
        *(row(spread=2.0, move=0.01) for _ in range(3)),
        *(row(spread=2.0, move=0.01, digest="digest-b") for _ in range(2)),
    ]

    stats = evaluate(rows)

    assert stats.digests == ("digest-a", "digest-b")
    assert stats.mixed_digests is True
    assert group(stats, Conviction.HIGH.value).observations == 5


def test_one_digest_is_not_reported_as_a_mixture() -> None:
    """The caveat has to be absent on the ordinary run or it stops being read."""
    stats = evaluate([row(spread=2.0, move=0.01) for _ in range(3)])

    assert stats.digests == ("digest-a",)
    assert stats.mixed_digests is False


# ----------------------------------------------------------------------
# Criterion 4: the journal split
# ----------------------------------------------------------------------


def test_trades_against_the_bias_are_reported_apart_from_trades_with_it() -> None:
    """Roadmap question 11's ruling: the engine counts them, it does not shout.

    A trader who does better against the engine than with it is the finding this
    split exists to surface, and it is invisible in a total.
    """
    trades = [
        *(trade(aligned=True, r_multiple=1.0, index=i) for i in range(6)),
        *(trade(aligned=True, r_multiple=-1.0, index=10 + i) for i in range(2)),
        *(trade(aligned=False, r_multiple=-1.0, index=20 + i) for i in range(3)),
    ]

    stats = evaluate([], trades)

    assert group(stats, ALIGNED).observations == 8
    assert group(stats, ALIGNED).hits == 6
    assert group(stats, AGAINST).observations == 3
    assert group(stats, AGAINST).hits == 0


def test_an_open_trade_is_not_counted_in_either_half() -> None:
    """A trade with no `r_multiple` has no outcome to attribute yet."""
    closed = trade(index=1)
    still_open = trade(index=2, r_multiple=None)

    stats = evaluate([], [closed, still_open])

    assert group(stats, ALIGNED).observations == 1


def test_an_empty_journal_leaves_the_split_out_rather_than_reporting_zeros() -> None:
    """Zeros look like a result. The normal case on a fresh clone is no records."""
    stats = evaluate([row(spread=2.0, move=0.01)])

    assert stats.by_alignment == ()
    assert stats.by_conviction != ()


# ----------------------------------------------------------------------
# Absence, and the edges
# ----------------------------------------------------------------------


def test_a_move_of_exactly_zero_counts_in_the_denominator_and_not_the_numerator() -> (
    None
):
    """The same rule `ConvictionStats` applies to a trade closed at breakeven.

    Not a third answer: a pair that did not move is in ``observations`` and out
    of ``hits``, which is arithmetically a miss. A three-valued hit was written
    first and removed, because nothing downstream could tell it from a miss and
    a distinction no consumer can act on is a claim rather than behaviour.
    """
    rows = [
        row(spread=2.0, move=0.01),
        row(spread=2.0, move=0.0),
        row(spread=2.0, move=-0.01),
    ]

    high = group(evaluate(rows), Conviction.HIGH.value)

    assert high.observations == 3
    assert high.hits == 1
    assert high.hit_rate == pytest.approx(1 / 3)


def test_a_row_with_no_lean_is_left_out_of_the_hit_rate() -> None:
    """A spread of exactly zero made no call, so there is nothing to be right about."""
    rows = [row(spread=0.0, move=0.01, conviction=Conviction.NONE)]

    stats = evaluate(rows)

    assert stats.by_conviction == ()
    assert stats.rows == 1
    assert stats.called == 0


def test_no_rows_at_all_is_an_empty_evaluation_rather_than_a_division() -> None:
    """A record with nothing in it is a fact about the record."""
    stats = evaluate([])

    assert stats.rows == 0
    assert stats.by_conviction == ()
    assert stats.digests == ()
    assert stats.shows_no_separation is True
