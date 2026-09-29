"""`journal.evaluate`: what the record says about the conviction ladder.

This is the first function in the project capable of saying the model does not
work, and the criteria on issue #263 are mostly about what it must not
overstate. The tests here are written the same way round: most of them assert
that a figure cannot be read without the sample behind it, that an empty bucket
is not a losing one, and that the output can say the buckets are the same.

Three rulings from the triage desk of 2026-09-24 shape what is asserted:

* The sample-size caveat is a computed flag on the bucket, not prose and not a
  docstring sentence. A renderer has to branch on a flag; it can print prose and
  a reader can skim past it.
* Criterion 9's second half, that no number comes from anywhere but these
  records, is enforced as a purity property rather than by a scanning test: a
  test cannot tell where a number came from by reading source, but it can assert
  the function reads nothing but its argument.
* `ConvictionStats` gains the interval field criterion 7 needs. It is in
  `src/fbe/journal.py` rather than `src/fbe/types.py`, and `evaluate` is its
  only producer, so nothing downstream breaks.

Every record here is built in this file. Nothing reads the repository's journal,
which is git-ignored and absent on a fresh clone, and nothing reaches the
network.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist

import pytest

import fbe
from fbe.journal import (
    EVIDENCE_THRESHOLD_TRADES,
    HIT_RATE_CONFIDENCE,
    ConvictionStats,
    TradeRecord,
    evaluate,
)
from fbe.types import Conviction, Direction

PACKAGE_ROOT = Path(fbe.__file__).resolve().parent

OPENED = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
"""Entry time for every record, so ordering comes from ``closed_at`` alone."""


def record(
    trade_id: str,
    conviction: Conviction,
    r_multiple: float | None,
    *,
    closed_day: int | None = 2,
    pair: str = "EURUSD",
) -> TradeRecord:
    """One journal record, carrying only what evaluation reads.

    ``r_multiple`` is the realised figure, measured against realised risk by
    `TradeRecord.r_multiple`'s own definition, which is why nothing here
    recomputes it from money. ``closed_day`` of ``None`` leaves the trade open.
    """
    closed = (
        None if closed_day is None else datetime(2026, 9, closed_day, 17, 0, tzinfo=UTC)
    )
    return TradeRecord(
        trade_id=trade_id,
        pair=pair,
        direction=Direction.LONG,
        opened_at=OPENED,
        closed_at=closed,
        entry=1.0850,
        stop=1.0825,
        units=400.0,
        lots=0.004,
        risk_amount=20.0,
        risk_fraction=0.01,
        account_balance_at_entry=2000.0,
        conviction=conviction,
        base_score=0.81,
        quote_score=-0.61,
        spread_score=1.42,
        config_digest="abc123",
        r_multiple=r_multiple,
        outcome_zar=None if r_multiple is None else r_multiple * 20.0,
    )


def wilson(wins: int, trades: int) -> tuple[float, float]:
    """The interval this module's figures are checked against, written out here.

    Written independently of the implementation rather than imported from it, so
    a test comparing the two is comparing two derivations rather than one
    function against itself. Wilson rather than the textbook normal
    approximation, because the approximation runs outside ``0..1`` at the sample
    sizes this journal will have for months and reports a zero-width interval on
    a bucket that has not lost yet.
    """
    z = NormalDist().inv_cdf(1 - (1 - HIT_RATE_CONFIDENCE) / 2)
    proportion = wins / trades
    denominator = 1 + z * z / trades
    centre = (proportion + z * z / (2 * trades)) / denominator
    half = (z / denominator) * (
        (proportion * (1 - proportion) / trades + z * z / (4 * trades * trades)) ** 0.5
    )
    return centre - half, centre + half


# --- what is in a bucket and what is not ------------------------------------


def test_an_open_trade_is_not_evaluated() -> None:
    """Criterion 1. A trade still running has no outcome to report."""
    stats = evaluate(
        [
            record("closed", Conviction.HIGH, 1.5),
            record("open", Conviction.HIGH, None, closed_day=None),
        ]
    )

    assert stats[Conviction.HIGH].trades == 1


def test_a_closed_trade_with_no_r_multiple_is_not_evaluated() -> None:
    """Criterion 1's other half, and the reason it is stated separately.

    A trade can be closed and still carry no R multiple, because
    `TradeRecord.r_multiple` divides by the realised risk and a record written
    without one has nothing to divide by. Counting it as a loss would be the
    plausible wrong answer.
    """
    stats = evaluate(
        [
            record("measured", Conviction.LOW, -1.0),
            record("unmeasured", Conviction.LOW, None, closed_day=3),
        ]
    )

    assert stats[Conviction.LOW].trades == 1
    assert stats[Conviction.LOW].total_r == pytest.approx(-1.0)


def test_an_open_trade_carrying_a_multiple_is_still_excluded() -> None:
    """Criterion 1 read strictly: open is decided by ``closed_at``, not by luck.

    The ordinary open record has no ``r_multiple`` either, so a filter that
    checked only the multiple would pass every test above. This record is the
    malformed one: still open and carrying a figure, which a hand-edited
    journal line produces. A trade that has not closed has no outcome whatever
    a field says, and counting one would put an unrealised number in a report
    about realised performance.
    """
    stats = evaluate(
        [
            record("closed", Conviction.HIGH, 1.0, closed_day=2),
            record("open-but-marked", Conviction.HIGH, 5.0, closed_day=None),
        ]
    )

    assert stats[Conviction.HIGH].trades == 1
    assert stats[Conviction.HIGH].total_r == pytest.approx(1.0)


def test_a_trade_lands_in_the_bucket_it_was_taken_at() -> None:
    """Criterion 2. The conviction at entry, not the conviction today.

    Two records identical in every scored field and different only in the
    conviction they carry. An implementation that re-derived conviction from the
    spread would put both in the same bucket, and the whole evaluation would
    then be measuring today's model against yesterday's trades.
    """
    stats = evaluate(
        [
            record("taken-high", Conviction.HIGH, 2.0),
            record("taken-low", Conviction.LOW, -1.0),
        ]
    )

    assert stats[Conviction.HIGH].trades == 1
    assert stats[Conviction.LOW].trades == 1
    assert stats[Conviction.HIGH].total_r == pytest.approx(2.0)


def test_a_conviction_with_no_closed_trades_is_absent() -> None:
    """Criterion 3. Zeros would read as a bucket that lost.

    "No trades were taken at that conviction" and "trades were taken and lost"
    are different facts, and a mapping that answers the first with
    ``expectancy_r=0.0`` has destroyed the difference.
    """
    stats = evaluate([record("only", Conviction.MEDIUM, 0.5)])

    assert set(stats) == {Conviction.MEDIUM}
    assert Conviction.LOW not in stats
    assert Conviction.NONE not in stats


def test_an_empty_journal_evaluates_to_an_empty_mapping() -> None:
    """The state of every fresh clone, which is not an error.

    `data/journal/` is git-ignored because it holds real account records, so the
    routine hosts see an empty journal on every run. An exception here would
    make the weekly review fail on a machine that has simply not traded yet.
    """
    assert evaluate([]) == {}


def test_a_journal_of_open_trades_only_evaluates_to_an_empty_mapping() -> None:
    """The same answer by a different route, which a filter could get wrong."""
    assert evaluate([record("open", Conviction.HIGH, None, closed_day=None)]) == {}


# --- the figures -------------------------------------------------------------


def test_the_hit_rate_and_expectancy_come_from_the_realised_multiples() -> None:
    """Criterion 4, worked by hand.

    Four trades at HIGH: +2.0, +1.0, +0.5 and -1.0. Three winners of four is a
    hit rate of 0.75. The multiples sum to 2.5, so expectancy is 0.625R, the
    winners average 1.1666..R and the one loser averages -1.0R.

    Expectancy rather than hit rate is the figure that decides whether the
    bucket makes money, which is why both are asserted and why the docstring
    says a 35% hit rate at 3R beats 70% at 0.4R.
    """
    stats = evaluate(
        [
            record("a", Conviction.HIGH, 2.0, closed_day=2),
            record("b", Conviction.HIGH, 1.0, closed_day=3),
            record("c", Conviction.HIGH, 0.5, closed_day=4),
            record("d", Conviction.HIGH, -1.0, closed_day=5),
        ]
    )
    high = stats[Conviction.HIGH]

    assert high.trades == 4
    assert high.wins == 3
    assert high.hit_rate == pytest.approx(0.75)
    assert high.expectancy_r == pytest.approx(0.625)
    assert high.total_r == pytest.approx(2.5)
    assert high.avg_win_r == pytest.approx(3.5 / 3)
    assert high.avg_loss_r == pytest.approx(-1.0)


def test_every_bucket_reports_its_sample_beside_its_figures() -> None:
    """Criterion 5. A figure without its count is not readable.

    The field is not new, but the property is the point: there is no way to
    obtain `hit_rate` from this mapping without `trades` arriving with it.
    """
    stats = evaluate([record(f"t{index}", Conviction.LOW, 1.0) for index in range(3)])

    assert stats[Conviction.LOW].trades == 3
    assert "trades" in ConvictionStats.__dataclass_fields__


def test_a_bucket_with_no_loser_reports_no_average_loss() -> None:
    """A mean of an empty set is not zero, and -0.0R would read as a breakeven.

    `avg_loss_r` of ``None`` says the bucket has not lost yet. A float there
    would have to be a number the bucket never produced, which is the sentinel
    ADR 0002 rule 1 forbids.
    """
    stats = evaluate([record("won", Conviction.HIGH, 1.5)])

    assert stats[Conviction.HIGH].avg_win_r == pytest.approx(1.5)
    assert stats[Conviction.HIGH].avg_loss_r is None


def test_a_bucket_with_no_winner_reports_no_average_win() -> None:
    """The other half. A bucket of pure losses is a real and important answer."""
    stats = evaluate(
        [
            record("lost", Conviction.LOW, -1.0, closed_day=2),
            record("lost-again", Conviction.LOW, -0.5, closed_day=3),
        ]
    )
    low = stats[Conviction.LOW]

    assert low.wins == 0
    assert low.hit_rate == pytest.approx(0.0)
    assert low.avg_win_r is None
    assert low.avg_loss_r == pytest.approx(-0.75)


def test_a_breakeven_trade_is_neither_a_win_nor_a_loss() -> None:
    """``wins`` is above zero and losers are below it, so 0.0 is in neither.

    Moving a stop to entry is how a breakeven trade happens, and it happens
    often enough to matter. Counting it as a loss would make a bucket's average
    loss shallower every time the owner protected a trade, which is the
    opposite of what that figure is read for. It still counts in the trade
    count and in expectancy, where it belongs: it is a trade that made nothing.
    """
    stats = evaluate([record("flat", Conviction.MEDIUM, 0.0)])
    medium = stats[Conviction.MEDIUM]

    assert medium.trades == 1
    assert medium.wins == 0
    assert medium.avg_win_r is None
    assert medium.avg_loss_r is None
    assert medium.expectancy_r == pytest.approx(0.0)
    assert medium.hit_rate == pytest.approx(0.0)


def test_the_drawdown_is_the_deepest_fall_in_close_order() -> None:
    """Worked by hand, and it is the one figure that depends on ordering.

    Closed in this order: +1.0, +2.0, -1.5, -1.0, +0.5. The cumulative curve
    runs 1.0, 3.0, 1.5, 0.5, 1.0, so the peak is 3.0 and the trough after it is
    0.5, a fall of 2.5R. Reported as a magnitude: a drawdown of 2.5R means the
    curve fell 2.5R from its high, and a signed figure here would leave a reader
    guessing which direction 2.5 meant.
    """
    stats = evaluate(
        [
            record("e", Conviction.HIGH, 0.5, closed_day=6),
            record("a", Conviction.HIGH, 1.0, closed_day=2),
            record("c", Conviction.HIGH, -1.5, closed_day=4),
            record("b", Conviction.HIGH, 2.0, closed_day=3),
            record("d", Conviction.HIGH, -1.0, closed_day=5),
        ]
    )

    assert stats[Conviction.HIGH].max_drawdown_r == pytest.approx(2.5)


def test_a_bucket_that_opens_with_a_loss_counts_it_as_drawdown() -> None:
    """The peak starts at zero, not at the first trade, and that is the point.

    Losing 1.0R and then making 0.5R leaves the curve at -0.5R, having been
    1.0R below where it started. Measuring the fall from the first point
    instead reports no drawdown at all for a bucket that went straight down,
    which is the flattering answer and the one a reader would act on.
    """
    stats = evaluate(
        [
            record("first-lost", Conviction.LOW, -1.0, closed_day=2),
            record("then-won", Conviction.LOW, 0.5, closed_day=3),
        ]
    )

    assert stats[Conviction.LOW].max_drawdown_r == pytest.approx(1.0)
    assert stats[Conviction.LOW].total_r == pytest.approx(-0.5)


def test_a_bucket_that_never_falls_has_no_drawdown() -> None:
    """Zero is a reading here, not an absence: the curve genuinely never fell."""
    stats = evaluate(
        [
            record("a", Conviction.HIGH, 1.0, closed_day=2),
            record("b", Conviction.HIGH, 2.0, closed_day=3),
        ]
    )

    assert stats[Conviction.HIGH].max_drawdown_r == pytest.approx(0.0)


# --- the interval and the sample-size flag -----------------------------------


def test_the_hit_rate_carries_a_computed_interval() -> None:
    """Criterion 7, against a hand-worked case.

    Three winners of four at 95% gives a Wilson interval of roughly 0.3006 to
    0.9544. The width is the finding: on four trades the record cannot tell a
    30% hit rate from a 95% one, and a point estimate of 0.75 hides that.
    """
    stats = evaluate(
        [
            record("a", Conviction.HIGH, 2.0, closed_day=2),
            record("b", Conviction.HIGH, 1.0, closed_day=3),
            record("c", Conviction.HIGH, 0.5, closed_day=4),
            record("d", Conviction.HIGH, -1.0, closed_day=5),
        ]
    )
    high = stats[Conviction.HIGH]

    assert high.hit_rate_low == pytest.approx(0.300642, abs=5e-7)
    assert high.hit_rate_high == pytest.approx(0.954413, abs=5e-7)
    assert (high.hit_rate_low, high.hit_rate_high) == pytest.approx(wilson(3, 4))
    assert high.hit_rate_low < high.hit_rate < high.hit_rate_high


def test_an_unbeaten_bucket_still_has_an_interval_with_width() -> None:
    """The case the textbook approximation gets wrong, so it is pinned.

    Five winners of five gives a point estimate of 1.0 and an interval from
    0.5655 to 1.0. The normal approximation gives zero width here and would
    report certainty from five trades.
    """
    stats = evaluate(
        [
            record(f"w{index}", Conviction.HIGH, 1.0, closed_day=2 + index)
            for index in range(5)
        ]
    )
    high = stats[Conviction.HIGH]

    assert high.hit_rate == pytest.approx(1.0)
    assert high.hit_rate_low == pytest.approx(0.565518, abs=5e-7)
    assert high.hit_rate_high == pytest.approx(1.0)


def test_a_bucket_with_no_winner_still_has_an_interval() -> None:
    """The mirror case, which divides by zero under the approximation."""
    stats = evaluate(
        [
            record(f"l{index}", Conviction.LOW, -1.0, closed_day=2 + index)
            for index in range(5)
        ]
    )
    low = stats[Conviction.LOW]

    assert low.hit_rate == pytest.approx(0.0)
    assert low.hit_rate_low == pytest.approx(0.0)
    assert low.hit_rate_high == pytest.approx(0.434482, abs=5e-7)


def test_the_interval_never_leaves_the_band_it_is_documented_in() -> None:
    """The ends are a proportion, so a negative one is a wrong number.

    Wilson is analytically inside ``0..1`` everywhere, but the arithmetic is
    floating point: at zero wins the lower end evaluates just below zero and at
    every win the upper end just above one. Both would reach a report as
    ``-0.0%`` or ``100.0%`` of something impossible, so the band is asserted
    across the bucket sizes this journal will actually have rather than at one
    hand-picked pair.
    """
    for trades in (1, 2, 3, 5, 8, 13, 40):
        for wins in range(trades + 1):
            multiples = [1.0] * wins + [-1.0] * (trades - wins)
            stats = evaluate(
                [
                    record(
                        f"t{index}", Conviction.HIGH, value, closed_day=2 + index % 26
                    )
                    for index, value in enumerate(multiples)
                ]
            )
            high = stats[Conviction.HIGH]

            assert high.hit_rate_low >= 0.0, (trades, wins, high.hit_rate_low)
            assert high.hit_rate_high <= 1.0, (trades, wins, high.hit_rate_high)
            assert high.hit_rate_low <= high.hit_rate <= high.hit_rate_high


def test_the_ends_are_exact_where_the_hit_rate_is_exact() -> None:
    """A bucket that has only won cannot have a lower bound above the range.

    The two extremes are the ones the clamp touches, so they are pinned as exact
    equalities rather than approximations: anything else would pass on the
    unclamped value.
    """
    unbeaten = evaluate(
        [
            record(f"w{index}", Conviction.HIGH, 1.0, closed_day=2 + index)
            for index in range(5)
        ]
    )
    winless = evaluate(
        [
            record(f"l{index}", Conviction.LOW, -1.0, closed_day=2 + index)
            for index in range(5)
        ]
    )

    assert unbeaten[Conviction.HIGH].hit_rate_high == 1.0
    assert winless[Conviction.LOW].hit_rate_low == 0.0


def test_a_small_bucket_is_marked_as_not_evidence() -> None:
    """Criterion 6, as the triage desk ruled it: a flag, not prose.

    Below the threshold a bucket is a record of what happened, not evidence
    about what will. A renderer must branch on this rather than print a sentence
    a reader can skim past.
    """
    stats = evaluate([record("a", Conviction.HIGH, 1.0)])

    assert stats[Conviction.HIGH].below_evidence_threshold is True


def test_a_bucket_at_the_threshold_is_not_marked() -> None:
    """At the threshold, not past it, and the boundary is asserted both sides."""
    trades = [
        record(f"t{index}", Conviction.HIGH, 1.0, closed_day=1 + (index % 28))
        for index in range(EVIDENCE_THRESHOLD_TRADES)
    ]

    assert evaluate(trades)[Conviction.HIGH].below_evidence_threshold is False
    assert evaluate(trades[:-1])[Conviction.HIGH].below_evidence_threshold is True


def test_the_flag_reads_the_module_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests the wire. Move the constant and the flag moves with it.

    Thirty is a number, and an inline one is the pattern this repository keeps
    filing defects about. This fails if the comparison is ever written against a
    literal.
    """
    trades = [
        record(f"t{index}", Conviction.LOW, 1.0, closed_day=2 + index)
        for index in range(3)
    ]
    monkeypatch.setattr("fbe.journal.EVIDENCE_THRESHOLD_TRADES", 2)

    assert evaluate(trades)[Conviction.LOW].below_evidence_threshold is False


# --- the answer this has to be able to give ---------------------------------


def test_two_buckets_that_performed_identically_read_as_identical() -> None:
    """Criterion 8. An analysis with no way to say "no difference" is not one.

    HIGH and LOW are given the same four outcomes, so every figure matches and
    the intervals are the same interval. Nothing in the mapping ranks the
    buckets, orders them or names a best one: the caller reads two rows that
    agree, which is the finding.
    """
    outcomes = (2.0, 1.0, 0.5, -1.0)
    stats = evaluate(
        [
            record(f"high{index}", Conviction.HIGH, value, closed_day=2 + index)
            for index, value in enumerate(outcomes)
        ]
        + [
            record(f"low{index}", Conviction.LOW, value, closed_day=2 + index)
            for index, value in enumerate(outcomes)
        ]
    )
    high, low = stats[Conviction.HIGH], stats[Conviction.LOW]

    assert high.expectancy_r == pytest.approx(low.expectancy_r)
    assert high.hit_rate == pytest.approx(low.hit_rate)
    assert (high.hit_rate_low, high.hit_rate_high) == pytest.approx(
        (low.hit_rate_low, low.hit_rate_high)
    )
    assert high.below_evidence_threshold is low.below_evidence_threshold


def test_overlapping_intervals_are_what_no_difference_looks_like() -> None:
    """The same answer where the point estimates differ, which is the real case.

    HIGH wins three of four and LOW two of four. The point estimates differ by
    25 percentage points and the intervals overlap heavily, so the record cannot
    yet tell the buckets apart. This is the shape a reader needs to see before
    acting on a ladder, and it is available from the returned data alone.
    """
    stats = evaluate(
        [
            record("h1", Conviction.HIGH, 1.0, closed_day=2),
            record("h2", Conviction.HIGH, 1.0, closed_day=3),
            record("h3", Conviction.HIGH, 1.0, closed_day=4),
            record("h4", Conviction.HIGH, -1.0, closed_day=5),
            record("l1", Conviction.LOW, 1.0, closed_day=2),
            record("l2", Conviction.LOW, 1.0, closed_day=3),
            record("l3", Conviction.LOW, -1.0, closed_day=4),
            record("l4", Conviction.LOW, -1.0, closed_day=5),
        ]
    )
    high, low = stats[Conviction.HIGH], stats[Conviction.LOW]

    assert high.hit_rate > low.hit_rate
    assert high.hit_rate_low < low.hit_rate_high
    assert low.hit_rate_low < high.hit_rate_high


# --- what the function may not do -------------------------------------------


def test_the_module_makes_no_unmeasured_claim() -> None:
    """Criterion 9's first half, which is what a scan can actually check."""
    source = (PACKAGE_ROOT / "journal.py").read_text().lower()

    for forbidden in ("proven", "backtested", " edge", "win rate"):
        assert forbidden not in source, forbidden


def test_evaluate_reads_nothing_but_its_argument() -> None:
    """Criterion 9's second half, as the triage desk re-ruled it.

    A test cannot tell by reading source where a number came from. It can tell
    that the function opens no file, reads no config, consults no clock and
    touches no module-level path, which is the property the criterion is
    reaching for: every figure is a function of the records passed in. This
    fails the day anyone reaches for a stored figure to fill a thin bucket.

    The positive assertion at the end is load-bearing. Against the scaffolded
    function, whose body is a single ``raise``, every negative assertion here
    passed and the test proved nothing. Requiring that the body actually reads
    its argument is what makes the absence of the others meaningful.
    """
    tree = ast.parse(inspect.getsource(evaluate))
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for forbidden in (
        "open",
        "load",
        "JOURNAL_PATH",
        "now",
        "today",
        "load_config",
        "read_text",
        "Path",
    ):
        assert forbidden not in used, forbidden
    assert "records" in used


def test_the_same_records_always_give_the_same_answer() -> None:
    """The behavioural half of the same property, in case the scan misses a route."""
    records = [
        record("a", Conviction.HIGH, 1.5, closed_day=2),
        record("b", Conviction.LOW, -1.0, closed_day=3),
    ]

    assert evaluate(records) == evaluate(records)
    assert evaluate(records) == evaluate(list(reversed(records)))


def test_the_records_are_not_mutated_or_reordered_in_place() -> None:
    """Evaluation is a read. A sort in place would reorder the caller's list."""
    records = [
        record("late", Conviction.HIGH, 1.0, closed_day=5),
        record("early", Conviction.HIGH, 2.0, closed_day=2),
    ]
    before = list(records)

    evaluate(records)

    assert records == before
