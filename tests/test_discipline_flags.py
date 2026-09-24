"""`discipline_flags`: the three behaviours the plan names, counted and not blocked.

The plan warns against revenge trading, overtrading and fading the engine. This
function finds them in a set of journal records and says so. It refuses
nothing: a control the owner has to argue with mid-session is a control they
will argue with, and the argument happens at the worst moment. A flag is read
in the weekly review, when whatever caused the behaviour is no longer present.

Three things shape every test below.

**A clean run is a result.** An empty sequence back means the records held none
of the three, which on a real trading record is worth reading rather than
assuming. So nothing here may emit a flag meaning "not enough data to tell":
the review would read that as a finding.

**An absent risk is not a small risk.** `TradeRecord.risk_amount` is
``float | None`` because nothing in the tree supplies a rate into ZAR, so on
the owner's own account every record written today carries no money figure at
all. The revenge rule compares one trade's risk against another's, and reading
absent as zero would rank every priced trade above every unpriced one and
report the escalation backwards. The size half of that comparison is skipped
when either side is absent, and the rest of the rule still fires.

**A threshold is a threshold.** Every number these rules turn on lives in a
module constant or in `RiskConfig`, and each is moved in a test rather than
trusted, because a literal that happens to equal a default proves nothing.

Nothing here touches the network or the owner's own journal: every record is
built in the test.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fbe.config import RiskConfig
from fbe.journal import (
    OVERTRADING_TRADES_PER_WEEK,
    REVENGE_WINDOW_MINUTES,
    BlackoutCheck,
    DisciplineFlag,
    TradeRecord,
    discipline_flags,
)
from fbe.types import Conviction, Direction

MONDAY = datetime(2026, 6, 29, 8, 0, tzinfo=UTC)
"""Every record is placed relative to this, so the arithmetic reads by eye."""


ENTRY = 1.0850
"""One price for every record, so a test that moves a price is testing it."""

STOP_DISTANCE = 0.0020
"""Twenty pips, the distance from entry to stop, and so one R in price terms."""


def trade(
    trade_id: str,
    opened_at: datetime,
    *,
    pair: str = "EURUSD",
    closed_at: datetime | None = None,
    duration: timedelta | None = timedelta(minutes=30),
    r_multiple: float | None = 0.6,
    priced: bool = True,
    risk_amount: float | None = 37.0,
    balance: float = 2000.0,
    currency: str = "ZAR",
    direction: Direction = Direction.SHORT,
    agreed_with_bias: bool = True,
) -> TradeRecord:
    """One journal record, varying only what a rule below reads.

    The defaults describe a trade that was opened, closed half an hour later
    for a modest win, agreed with the engine and broke none of the three rules,
    so a test that changes one field is testing that field. Pass
    ``duration=None`` for a position that is still open, which is a different
    record and a different answer to every rule here.

    ``r_multiple`` shapes the exit price as well as the recorded result, and the
    stop sits on the side of the entry that the direction puts it, so the price
    and the money always tell the same story. A fixture where they disagree
    would let a rule read either one and look right.

    ``priced`` false is the owner's own account: `fbe journal add` writes
    ``risk_amount``, ``outcome_zar`` and ``r_multiple`` as ``None`` together,
    because all three need a rate into the account currency that nothing in the
    tree supplies. The price is still there. Passing ``risk_amount=None`` does
    the same thing, since a record cannot hold an R multiple without the risk it
    is a multiple of.
    """
    closed = opened_at + duration if closed_at is None and duration else closed_at
    long = direction is Direction.LONG
    stop = ENTRY - STOP_DISTANCE if long else ENTRY + STOP_DISTANCE
    money = priced and risk_amount is not None
    moved = None if r_multiple is None else r_multiple * STOP_DISTANCE
    exit_price = None
    if closed is not None and moved is not None:
        exit_price = ENTRY + moved if long else ENTRY - moved
    return TradeRecord(
        trade_id=trade_id,
        pair=pair,
        direction=direction,
        opened_at=opened_at,
        entry=ENTRY,
        stop=stop,
        units=1000.0,
        lots=0.01,
        risk_amount=risk_amount if money else None,
        risk_fraction=risk_amount / balance if money and risk_amount else None,
        account_balance_at_entry=balance,
        account_currency=currency,
        conviction=Conviction.MEDIUM,
        base_score=-1.52,
        quote_score=1.84,
        spread_score=-3.36,
        config_digest="7f3c1a9de204",
        closed_at=closed,
        exit_price=exit_price,
        outcome_zar=(
            r_multiple * risk_amount
            if money and closed is not None and r_multiple is not None and risk_amount
            else None
        ),
        r_multiple=r_multiple if money and closed is not None else None,
        agreed_with_bias=agreed_with_bias,
        blackout_check=BlackoutCheck.CLEAR,
    )


def loser(
    trade_id: str = "loss",
    closed_at: datetime | None = None,
    *,
    pair: str = "EURUSD",
    risk_amount: float | None = 37.0,
    balance: float = 2000.0,
    currency: str = "ZAR",
    priced: bool = True,
    r_multiple: float = -1.0,
) -> TradeRecord:
    """A closed trade that lost, which is what starts the revenge window."""
    closed = MONDAY + timedelta(hours=1) if closed_at is None else closed_at
    return trade(
        trade_id,
        opened_at=closed - timedelta(hours=4),
        pair=pair,
        closed_at=closed,
        r_multiple=r_multiple,
        risk_amount=risk_amount,
        balance=balance,
        currency=currency,
        priced=priced,
    )


def kinds(flags: object) -> list[str]:
    return [flag.kind for flag in flags]  # type: ignore[attr-defined]


def of_kind(flags: object, kind: str) -> list[DisciplineFlag]:
    return [flag for flag in flags if flag.kind == kind]  # type: ignore[attr-defined]


# --- a clean run says so ------------------------------------------------------


def test_no_records_is_a_clean_run_rather_than_an_unknown() -> None:
    """Nothing to read is not the same as nothing to say, and this is the first.

    A fresh clone has no journal at all, so this is the normal case for months.
    A flag meaning "too few trades to tell" would read in the review as a
    finding about the trader.
    """
    assert list(discipline_flags(())) == []


def test_a_set_of_records_breaking_none_of_the_rules_is_clean() -> None:
    """One trade a week, each aligned with the engine, none after a loss."""
    records = tuple(
        trade(f"t{index}", MONDAY + timedelta(days=7 * index)) for index in range(4)
    )

    assert list(discipline_flags(records)) == []


# --- revenge -----------------------------------------------------------------


def test_an_entry_inside_the_window_after_a_loss_is_flagged() -> None:
    """The plan's own words are to avoid entering immediately after a loss."""
    loss = loser()
    after = trade("revenge", loss.closed_at + timedelta(minutes=20))

    flags = of_kind(discipline_flags((loss, after)), "revenge")

    assert len(flags) == 1
    assert flags[0].trade_id == "revenge"
    assert flags[0].occurred_at == after.opened_at
    assert "20" in flags[0].detail


@pytest.mark.parametrize(
    "offset, flagged",
    [
        (timedelta(0), True),
        (timedelta(minutes=REVENGE_WINDOW_MINUTES), True),
        (timedelta(minutes=REVENGE_WINDOW_MINUTES, seconds=1), False),
    ],
)
def test_the_window_boundary_is_inclusive(offset: timedelta, flagged: bool) -> None:
    """Both sides of it, because a rule stated in minutes is read at its edge.

    An entry at the moment the loss closed is inside it: closing one trade and
    opening another in the same second is the behaviour, not a coincidence.
    """
    loss = loser()
    after = trade("next", loss.closed_at + offset)

    assert bool(of_kind(discipline_flags((loss, after)), "revenge")) is flagged


def test_an_entry_before_the_loss_closed_is_not_a_reaction_to_it() -> None:
    """A position already on when the other one closed was not opened in anger."""
    loss = loser()
    earlier = trade("earlier", loss.closed_at - timedelta(minutes=10))

    assert of_kind(discipline_flags((loss, earlier)), "revenge") == []


def test_a_loss_is_found_from_the_price_when_no_money_figure_exists() -> None:
    """The only shape of record the owner's own journal holds.

    `fbe journal add` writes ``risk_amount``, ``outcome_zar`` and ``r_multiple``
    as ``None`` together, because valuing a G10 pip in ZAR needs a rate nothing
    in the tree supplies. A rule that recognises a loss only through those three
    finds none on the real file and returns an empty sequence, which the
    docstring and the weekly routine both tell the reader means a clean run.
    """
    loss = loser(priced=False)
    after = trade("after", loss.closed_at + timedelta(minutes=9), priced=False)

    assert (loss.risk_amount, loss.outcome_zar, loss.r_multiple) == (None, None, None)
    assert loss.exit_price is not None

    flags = of_kind(discipline_flags((loss, after)), "revenge")

    assert [flag.trade_id for flag in flags] == ["after"]
    assert (
        flags[0].detail == "entered EURUSD 9 minutes after a loss on EURUSD, same pair"
    )


@pytest.mark.parametrize(
    "direction, result, lost",
    [
        (Direction.LONG, -1.0, True),
        (Direction.LONG, 1.0, False),
        (Direction.SHORT, -1.0, True),
        (Direction.SHORT, 1.0, False),
    ],
)
def test_the_price_reading_carries_the_direction(
    direction: Direction, result: float, lost: bool
) -> None:
    """An exit below the entry is a loss on a long and a win on a short.

    Reading the move without the direction halves the rule: every short that
    worked would start a revenge window and every short that failed would not.
    """
    closed = MONDAY + timedelta(hours=1)
    first = trade(
        "first",
        closed - timedelta(hours=4),
        closed_at=closed,
        direction=direction,
        r_multiple=result,
        priced=False,
    )
    after = trade("after", closed + timedelta(minutes=5), priced=False)

    assert bool(of_kind(discipline_flags((first, after)), "revenge")) is lost


def test_the_recorded_money_outweighs_the_price_when_the_two_disagree() -> None:
    """A hand-edited line where the money says flat and the price says down.

    `fbe journal add` writes the two from the same numbers, so they only part
    company in a file someone edited. The money is still the authority: it is
    the figure that is net of costs, and the price is the fallback for when
    there is no money figure at all, not a second opinion about one there is.
    """
    flat = replace(
        loser("flat", priced=False),
        outcome_zar=0.0,
    )
    after = trade("after", flat.closed_at + timedelta(minutes=5), priced=False)

    assert flat.exit_price is not None and flat.exit_price > flat.entry
    assert of_kind(discipline_flags((flat, after)), "revenge") == []


def test_a_trade_that_exited_at_its_entry_did_not_lose() -> None:
    """Scratched at breakeven, on the price reading as on the other two."""
    flat = trade(
        "flat",
        MONDAY,
        closed_at=MONDAY + timedelta(hours=1),
        r_multiple=0.0,
        priced=False,
    )
    after = trade("after", MONDAY + timedelta(hours=1, minutes=5), priced=False)

    assert flat.exit_price == flat.entry
    assert of_kind(discipline_flags((flat, after)), "revenge") == []


def test_a_record_with_no_side_is_not_read_as_either_one() -> None:
    """`fbe journal add` refuses a neutral direction, and `load` does not.

    Without a side the sign of a price move means nothing, so the record is an
    unknown rather than a loss. Guessing long would make half of them losses.
    """
    sideless = trade(
        "sideless",
        MONDAY,
        closed_at=MONDAY + timedelta(hours=1),
        direction=Direction.NEUTRAL,
        r_multiple=1.0,
        priced=False,
    )
    after = trade("after", MONDAY + timedelta(hours=1, minutes=5), priced=False)

    assert sideless.exit_price is not None and sideless.exit_price < sideless.entry
    assert of_kind(discipline_flags((sideless, after)), "revenge") == []


def test_a_closed_trade_with_no_exit_price_and_no_money_is_not_a_loss() -> None:
    """Three absences are not a result. An unknown does not start the window."""
    unknown = trade(
        "unknown",
        MONDAY,
        closed_at=MONDAY + timedelta(hours=1),
        r_multiple=None,
        priced=False,
    )
    after = trade("after", MONDAY + timedelta(hours=1, minutes=5), priced=False)

    assert unknown.exit_price is None
    assert of_kind(discipline_flags((unknown, after)), "revenge") == []


def test_an_entry_after_a_winning_trade_is_not_revenge() -> None:
    """The rule is about a loss. Trading straight after a win is a different
    behaviour and the plan does not name it."""
    won = trade(
        "win",
        MONDAY - timedelta(hours=4),
        closed_at=MONDAY,
        r_multiple=1.5,
    )
    after = trade("next", MONDAY + timedelta(minutes=20))

    assert of_kind(discipline_flags((won, after)), "revenge") == []


def test_an_entry_after_an_open_trade_is_not_revenge() -> None:
    """A trade with no close has not lost yet, whatever it is showing.

    The running trade is opened inside the window on purpose: reading an open
    position's own entry as the moment it lost would flag every second entry of
    a busy morning, and the loss it names would be one the owner has not taken.
    """
    running = trade("open", MONDAY, duration=None, r_multiple=-1.0)
    after = trade("next", MONDAY + timedelta(minutes=20))

    assert of_kind(discipline_flags((running, after)), "revenge") == []


def test_each_entry_inside_the_window_is_its_own_flag() -> None:
    """Three entries in the twenty minutes after one loss is three reactions.

    Counting the loss rather than the reactions would read in the review as one
    lapse, which is the opposite of what the record shows.
    """
    loss = loser()
    after = tuple(
        trade(
            f"r{index}", loss.closed_at + timedelta(minutes=5 * index), pair=BOOK[index]
        )
        for index in range(1, 4)
    )

    flags = of_kind(discipline_flags((loss, *after)), "revenge")

    assert [flag.trade_id for flag in flags] == [record.trade_id for record in after]


def test_the_flag_reads_as_one_sentence_about_this_entry_and_that_loss() -> None:
    """Every field distinct, because each one is a place the wrong value fits.

    A gap in seconds still contains the minutes as a substring, the two money
    figures are interchangeable, the loss can be printed without its sign, and
    the entry's own pair and result can stand in for the loss's. Each of those
    reads as a plausible sentence, so the whole sentence is asserted.
    """
    loss = loser("loss", MONDAY + timedelta(hours=1), pair="EURUSD", risk_amount=20.0)
    loss = replace(loss, r_multiple=-1.7, outcome_zar=-34.0)
    after = trade(
        "after",
        loss.closed_at + timedelta(minutes=12),
        pair="GBPUSD",
        risk_amount=45.0,
    )

    flags = of_kind(discipline_flags((loss, after)), "revenge")

    assert flags[0].detail == (
        "entered GBPUSD 12 minutes after a -1.7R loss on EURUSD, "
        "larger risk than the trade that lost, ZAR 45.00 against ZAR 20.00"
    )


def test_the_same_pair_reads_before_the_size_when_both_apply() -> None:
    """Both aggravations on one entry, in one sentence."""
    loss = loser(risk_amount=20.0)
    after = trade("after", loss.closed_at + timedelta(minutes=3), risk_amount=25.0)

    flags = of_kind(discipline_flags((loss, after)), "revenge")

    assert flags[0].detail == (
        "entered EURUSD 3 minutes after a -1.0R loss on EURUSD, same pair and "
        "larger risk than the trade that lost, ZAR 25.00 against ZAR 20.00"
    )


def test_the_same_pair_makes_the_flag_say_so() -> None:
    """Going straight back into the pair that just lost is the classic shape."""
    loss = loser(pair="EURUSD")
    same = trade("same", loss.closed_at + timedelta(minutes=5), pair="EURUSD")
    other = trade("other", loss.closed_at + timedelta(minutes=5), pair="GBPUSD")

    same_flag = of_kind(discipline_flags((loss, same)), "revenge")[0]
    other_flag = of_kind(discipline_flags((loss, other)), "revenge")[0]

    assert "same pair" in same_flag.detail
    assert "same pair" not in other_flag.detail


def test_a_larger_risk_than_the_losing_trade_makes_the_flag_say_so() -> None:
    """Sizing up straight after a loss is the other classic shape."""
    loss = loser(risk_amount=20.0)
    bigger = trade("bigger", loss.closed_at + timedelta(minutes=5), risk_amount=40.0)
    smaller = trade("smaller", loss.closed_at + timedelta(minutes=5), risk_amount=10.0)

    bigger_flag = of_kind(discipline_flags((loss, bigger)), "revenge")[0]
    smaller_flag = of_kind(discipline_flags((loss, smaller)), "revenge")[0]

    assert "larger" in bigger_flag.detail
    assert "larger" not in smaller_flag.detail


def test_two_risks_in_different_currencies_are_not_compared() -> None:
    """A journal that spans a change of denomination holds both.

    ZAR 350 and USD 20 are the same position, and a bare comparison of the two
    numbers reports the second trade as four times the size of the first while
    printing one currency symbol in front of both figures. The size half is
    skipped, as it is when a figure is missing, and the rest of the flag stands.
    """
    loss = loser(risk_amount=20.0, currency="USD")
    after = trade(
        "after",
        loss.closed_at + timedelta(minutes=5),
        risk_amount=350.0,
        currency="ZAR",
    )

    flags = of_kind(discipline_flags((loss, after)), "revenge")

    assert len(flags) == 1
    assert "larger" not in flags[0].detail
    assert "ZAR" not in flags[0].detail


def test_a_scalp_closed_in_the_minute_it_opened_is_not_its_own_revenge() -> None:
    """Trade ids are minute resolution, so open and close can share a stamp.

    A trade normally opens before it closes, which puts its own close outside
    the window at the lower end. At the same stamp the gap is zero, which is
    inside it, and the rule would report a trade as a reaction to itself.
    """
    scalp = trade("scalp", MONDAY, closed_at=MONDAY, r_multiple=-1.0, priced=False)

    assert of_kind(discipline_flags((scalp,)), "revenge") == []


def test_the_same_risk_as_the_losing_trade_is_not_an_escalation() -> None:
    """Sizing the same is following the plan, which is what the ladder asks for."""
    loss = loser(risk_amount=20.0)
    same = trade("same", loss.closed_at + timedelta(minutes=5), risk_amount=20.0)

    assert "larger" not in of_kind(discipline_flags((loss, same)), "revenge")[0].detail


def test_a_breakeven_trade_does_not_start_a_revenge_window() -> None:
    """Zero is not a loss. Scratching a trade is not what the rule is about."""
    flat = loser("flat")
    flat = replace(flat, r_multiple=0.0, outcome_zar=0.0)
    after = trade("after", flat.closed_at + timedelta(minutes=5))

    assert of_kind(discipline_flags((flat, after)), "revenge") == []


def test_the_flag_names_the_most_recent_loss_in_the_window() -> None:
    """Two losses inside the window, and the one just closed is the one reacted
    to. Naming the older one would put the wrong pair and the wrong gap in front
    of the reader."""
    old = loser("old", MONDAY, pair="AUDNZD")
    recent = loser("recent", MONDAY + timedelta(minutes=30), pair="USDCAD")
    after = trade("after", recent.closed_at + timedelta(minutes=5), pair="EURUSD")

    detail = of_kind(discipline_flags((old, recent, after)), "revenge")[0].detail

    assert "USDCAD" in detail
    assert "AUDNZD" not in detail


def test_the_size_comparison_is_money_rather_than_its_share_of_the_account() -> None:
    """The same money on a smaller account is the same position, not a bigger one.

    Comparing fractions would flag a trader who kept their size constant while
    their balance fell, which is the drawdown case and a different conversation
    from sizing up after a loss. The flag would also print two identical money
    figures beside the claim that one is larger.
    """
    loss = loser(risk_amount=40.0, balance=4000.0)
    after = trade(
        "after",
        loss.closed_at + timedelta(minutes=5),
        risk_amount=40.0,
        balance=2000.0,
    )

    assert loss.risk_fraction != after.risk_fraction
    assert "larger" not in of_kind(discipline_flags((loss, after)), "revenge")[0].detail


def test_the_size_comparison_is_risk_against_risk_not_risk_against_loss() -> None:
    """A trade cut early lost less than it risked, and the risk is the comparison.

    Reading the realised loss as the size of the losing trade makes any normal
    position look like an escalation after a stop that was managed well.
    """
    loss = loser(risk_amount=40.0, r_multiple=-0.2)
    after = trade("after", loss.closed_at + timedelta(minutes=5), risk_amount=20.0)

    assert loss.outcome_zar is not None and abs(loss.outcome_zar) < 20.0
    assert "larger" not in of_kind(discipline_flags((loss, after)), "revenge")[0].detail


@pytest.mark.parametrize(
    "loss_risk, entry_risk",
    [(None, 40.0), (20.0, None), (None, None)],
)
def test_an_unpriced_trade_is_not_read_as_a_small_one(
    loss_risk: float | None, entry_risk: float | None
) -> None:
    """The owner's own account today, where no record carries a money figure.

    Reading an absent risk as zero would rank every priced trade above every
    unpriced one and report the escalation backwards. The size comparison is
    skipped and the rest of the rule still fires, so the entry is still
    flagged, just without a claim about its size.
    """
    loss = loser(risk_amount=loss_risk)
    after = trade(
        "after", loss.closed_at + timedelta(minutes=5), risk_amount=entry_risk
    )

    flags = of_kind(discipline_flags((loss, after)), "revenge")

    assert len(flags) == 1
    assert "larger" not in flags[0].detail


def test_the_revenge_window_is_the_module_constant() -> None:
    """Moved rather than trusted. A literal equal to the default proves nothing."""
    loss = loser()
    after = trade("after", loss.closed_at + timedelta(minutes=90))

    assert of_kind(discipline_flags((loss, after)), "revenge") == []

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("fbe.journal.REVENGE_WINDOW_MINUTES", 120)

        assert len(of_kind(discipline_flags((loss, after)), "revenge")) == 1


# --- overtrading --------------------------------------------------------------


def week_of(count: int, start: datetime = MONDAY) -> tuple[TradeRecord, ...]:
    """``count`` entries one hour apart, each closed before the next opens."""
    return tuple(
        trade(f"w{index}", start + timedelta(hours=index)) for index in range(count)
    )


def test_more_entries_than_the_week_allows_is_flagged() -> None:
    records = week_of(OVERTRADING_TRADES_PER_WEEK + 1)

    flags = of_kind(discipline_flags(records), "overtrading")

    assert len(flags) == 1
    assert flags[0].trade_id == records[0].trade_id
    assert str(OVERTRADING_TRADES_PER_WEEK + 1) in flags[0].detail


def test_exactly_the_allowance_is_not_overtrading() -> None:
    """The constant is the number above which the run is flagged."""
    assert (
        of_kind(discipline_flags(week_of(OVERTRADING_TRADES_PER_WEEK)), "overtrading")
        == []
    )


def test_one_busy_window_produces_one_flag_rather_than_one_per_entry() -> None:
    """Ten entries in a day is one finding. Counting every window that contains
    them would hand the review eight lines saying the same thing."""
    records = week_of(10)

    assert len(of_kind(discipline_flags(records), "overtrading")) == 1


def test_a_later_run_does_not_swallow_an_earlier_one() -> None:
    """A quiet month between a busy week and a busier one.

    Looking for the densest window in the whole journal rather than in the run
    being flagged reports the worst week ever recorded and nothing before it.
    """
    june = week_of(OVERTRADING_TRADES_PER_WEEK + 1)
    august = week_of(OVERTRADING_TRADES_PER_WEEK + 4, MONDAY + timedelta(days=60))
    august = tuple(
        replace(record, trade_id=f"aug{index}") for index, record in enumerate(august)
    )

    flags = of_kind(discipline_flags((*june, *august)), "overtrading")

    assert [flag.trade_id for flag in flags] == [june[0].trade_id, august[0].trade_id]
    assert [flag.detail.split()[0] for flag in flags] == ["6", "9"]


def test_one_run_is_reported_once_however_its_worst_week_sits_in_it() -> None:
    """Six entries on six days, then six more on the eighth.

    The run breaches first as a window of six and its worst week holds ten,
    which ends after the last of them. Advancing past the window that opened
    the run rather than past the one reported leaves the tail of it uncounted
    and flags the same fortnight twice.
    """
    early = tuple(
        trade(f"d{index}", MONDAY + timedelta(days=index)) for index in range(6)
    )
    late = tuple(
        trade(f"l{index}", MONDAY + timedelta(days=8, hours=index))
        for index in range(6)
    )

    flags = of_kind(discipline_flags((*early, *late)), "overtrading")

    assert len(flags) == 1
    assert flags[0].detail.startswith("10 entries in the seven days from ")


def test_the_window_is_counted_on_open_time_rather_than_on_input_order() -> None:
    """`load` sorts, and a caller building records by hand does not.

    The flag is keyed on the entry that opens the window, so reading the input
    order as the time order keys it on the last entry instead and points the
    review at the wrong end of the week.
    """
    records = week_of(OVERTRADING_TRADES_PER_WEEK + 1)

    flags = of_kind(discipline_flags(tuple(reversed(records))), "overtrading")

    assert len(flags) == 1
    assert flags[0].trade_id == records[0].trade_id
    assert flags[0].occurred_at == records[0].opened_at


def spread_over(span: timedelta) -> tuple[TradeRecord, ...]:
    """Six entries, five bunched at the start and the last ``span`` after them."""
    first = tuple(
        trade(f"n{index}", MONDAY + timedelta(hours=index), pair=BOOK[index])
        for index in range(OVERTRADING_TRADES_PER_WEEK)
    )
    return (*first, trade("last", MONDAY + span, pair=BOOK[5]))


@pytest.mark.parametrize(
    "span, flagged",
    [
        (timedelta(days=7), True),
        (timedelta(days=7, seconds=1), False),
    ],
)
def test_the_week_is_seven_days_and_its_far_edge_is_inclusive(
    span: timedelta, flagged: bool
) -> None:
    """Both sides of the window, because a week stated in days is read at its edge.

    Without this the length is free: a window quietly widened to a fortnight
    flags a selective month as overtrading, and one narrowed to six days misses
    a genuine six-in-seven and reports the week clean.
    """
    records = spread_over(span)

    assert bool(of_kind(discipline_flags(records), "overtrading")) is flagged


def test_the_flag_reports_the_worst_week_in_the_run_rather_than_the_first() -> None:
    """Six entries on six days, then five more on the eighth.

    The first breaching window holds six, the worst true week in that run runs
    from the second day to the ninth and holds nine, and nine is the number the
    review acts on. A flag reading six against an allowance of five describes a
    marginal week that the record does not hold.
    """
    early = tuple(
        trade(f"d{index}", MONDAY + timedelta(days=index)) for index in range(6)
    )
    late = tuple(
        trade(f"l{index}", MONDAY + timedelta(days=8, hours=index))
        for index in range(5)
    )

    flags = of_kind(discipline_flags((*early, *late)), "overtrading")

    assert len(flags) == 1
    assert flags[0].trade_id == "d2"
    assert flags[0].occurred_at == early[2].opened_at
    assert flags[0].detail.startswith("9 entries in the seven days from ")


def test_two_busy_windows_are_two_flags() -> None:
    """A flag closes its own window and the next one is counted from scratch.

    Skipping to the end of the records instead would report the first busy week
    of the journal and nothing after it, for the life of the file.
    """
    june = week_of(OVERTRADING_TRADES_PER_WEEK + 1)
    august = week_of(OVERTRADING_TRADES_PER_WEEK + 1, MONDAY + timedelta(days=60))
    august = tuple(
        replace(record, trade_id=f"aug{index}") for index, record in enumerate(august)
    )

    flags = of_kind(discipline_flags((*june, *august)), "overtrading")

    assert [flag.trade_id for flag in flags] == [june[0].trade_id, august[0].trade_id]


def test_the_window_is_rolling_rather_than_a_calendar_week() -> None:
    """The case a calendar-week reading misses entirely.

    Three entries late on a Sunday and three early the following Tuesday are
    six inside six days, and two calendar weeks of three. A reader told the
    week was clean twice would never see it.
    """
    sunday = datetime(2026, 7, 5, 20, 0, tzinfo=UTC)
    records = tuple(
        trade(f"a{index}", sunday + timedelta(hours=index)) for index in range(3)
    ) + tuple(
        trade(f"b{index}", sunday + timedelta(days=2, hours=index))
        for index in range(3)
    )

    assert len(of_kind(discipline_flags(records), "overtrading")) == 1


def test_entries_spread_beyond_the_window_are_not_overtrading() -> None:
    """Six trades, each eight days after the last."""
    records = tuple(
        trade(f"s{index}", MONDAY + timedelta(days=8 * index))
        for index in range(OVERTRADING_TRADES_PER_WEEK + 1)
    )

    assert of_kind(discipline_flags(records), "overtrading") == []


def test_the_weekly_allowance_is_the_module_constant() -> None:
    records = week_of(OVERTRADING_TRADES_PER_WEEK)

    assert of_kind(discipline_flags(records), "overtrading") == []

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("fbe.journal.OVERTRADING_TRADES_PER_WEEK", 2)

        assert len(of_kind(discipline_flags(records), "overtrading")) == 1


# --- too many positions open at once -----------------------------------------


BOOK = ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCAD", "NZDUSD")
"""Distinct pairs, so a limit read per pair rather than per book does not pass."""


def overlapping(count: int, *, closed: bool = False) -> tuple[TradeRecord, ...]:
    """``count`` trades opened an hour apart in different pairs, open together.

    Open by default, because the live book is the only state
    `max_concurrent_positions` is about and a reading that counts only closed
    history would look correct against closed fixtures and say nothing about
    the tickets currently on.
    """
    return tuple(
        trade(
            f"o{index}",
            MONDAY + timedelta(hours=index),
            pair=BOOK[index % len(BOOK)],
            duration=timedelta(days=3) if closed else None,
        )
        for index in range(count)
    )


def test_more_positions_open_at_once_than_the_limit_allows_is_flagged() -> None:
    """And the flag says the limit was bypassed, because it was.

    `check_limits` refuses this before the trade is taken, so a journal holding
    it means the refusal was overridden or never consulted. That is a different
    fact from a busy week and the review has to be able to tell them apart, so
    the whole sentence is asserted rather than the phrase `check_limits`. A flag
    reading "check_limits reviewed and allowed this" would pass a substring
    check and say the opposite of what happened.

    The sentence says what the limit is for rather than that it ran, because
    `risk._concurrent_check` reports not performed on every run today: it is
    never given an open book to count. A flag claiming a refusal was overridden
    would be describing a gate that has not yet been consulted once.
    """
    limit = RiskConfig().max_concurrent_positions
    records = overlapping(limit + 1)

    flags = [
        flag
        for flag in of_kind(discipline_flags(records), "overtrading")
        if "at once" in flag.detail
    ]

    assert len(flags) == 1
    assert flags[0].trade_id == records[limit].trade_id
    assert flags[0].occurred_at == records[limit].opened_at
    assert flags[0].detail == (
        "4 positions open at once on entering USDJPY, above the 3 in "
        "RiskConfig.max_concurrent_positions, a limit check_limits refuses "
        "when it is told what is already open"
    )


def test_a_position_with_no_close_is_still_an_open_position() -> None:
    """The live book, which is the case the limit exists for.

    A reading that counts only trades with a ``closed_at`` looks right against
    closed history and reports nothing at all about the tickets currently on.
    Both books are also read for when the breach happened: on the entry that
    caused it, not on the exit that ended it, which is a date the review cannot
    act on and which reorders the flag against everything around it.
    """
    limit = RiskConfig().max_concurrent_positions
    for records in (overlapping(limit + 1), overlapping(limit + 1, closed=True)):
        flags = [
            flag
            for flag in of_kind(discipline_flags(records), "overtrading")
            if "at once" in flag.detail
        ]

        assert [flag.trade_id for flag in flags] == [
            record.trade_id for record in records[limit:]
        ]
        assert [flag.occurred_at for flag in flags] == [
            record.opened_at for record in records[limit:]
        ]


def test_the_limit_counts_the_book_rather_than_each_pair() -> None:
    """Four tickets in four pairs is four tickets.

    The limit is about attention, not about exposure to one currency, so a
    count taken per pair would never reach it however many charts are open.
    """
    records = overlapping(RiskConfig().max_concurrent_positions + 1)

    assert len({record.pair for record in records}) == len(records)
    assert [
        flag.trade_id
        for flag in of_kind(discipline_flags(records), "overtrading")
        if "at once" in flag.detail
    ] == [records[-1].trade_id]


def test_every_entry_beyond_the_limit_is_its_own_flag() -> None:
    """Going from four open tickets to six is an escalation, not one event."""
    limit = RiskConfig().max_concurrent_positions
    records = overlapping(limit + 3)

    flags = [
        flag
        for flag in of_kind(discipline_flags(records), "overtrading")
        if "at once" in flag.detail
    ]

    assert [flag.trade_id for flag in flags] == [
        record.trade_id for record in records[limit:]
    ]
    assert [flag.detail.split()[0] for flag in flags] == ["4", "5", "6"]


def test_exactly_the_concurrent_limit_is_not_flagged() -> None:
    limit = RiskConfig().max_concurrent_positions
    records = overlapping(limit)

    assert [
        flag
        for flag in of_kind(discipline_flags(records), "overtrading")
        if "at once" in flag.detail
    ] == []


def test_positions_that_do_not_overlap_are_not_concurrent() -> None:
    """Trades in a row, each closed at the moment the next one opened.

    Back to back is the boundary: a position closed at 10:00 is not still open
    at 10:00, so replacing one ticket with another is one position, not two.
    """
    records = tuple(
        trade(
            f"seq{index}",
            MONDAY + timedelta(hours=6 * index),
            closed_at=MONDAY + timedelta(hours=6 * (index + 1)),
        )
        for index in range(RiskConfig().max_concurrent_positions + 2)
    )

    assert [
        flag
        for flag in of_kind(discipline_flags(records), "overtrading")
        if "at once" in flag.detail
    ] == []


def test_a_scalp_closed_in_the_minute_it_opened_counts_in_its_own_book() -> None:
    """Four tickets on, one of them opened and closed in the same minute.

    The rule that a position closed at a moment is not open at that moment is
    about the other records. Applied to the entering record it drops that
    ticket from its own count, and a book of four reads as three and clears.
    """
    limit = RiskConfig().max_concurrent_positions
    running = overlapping(limit)
    moment = MONDAY + timedelta(hours=limit)
    scalp = trade("scalp", moment, closed_at=moment, pair="NZDCAD")

    flags = [
        flag
        for flag in of_kind(discipline_flags((*running, scalp)), "overtrading")
        if "at once" in flag.detail
    ]

    assert [flag.trade_id for flag in flags] == ["scalp"]
    assert flags[0].detail.startswith("4 positions open at once")


def test_a_ticket_replaced_at_the_moment_the_last_one_closed_is_one_position() -> None:
    """The boundary of the concurrent count, at a limit of one so it bites.

    A position closed at 10:00 is not still open at 10:00. Counting it would
    report a breach every time the owner closes one trade and opens the next in
    the same minute, which is a normal way to rotate a ticket.
    """
    first = trade("first", MONDAY, closed_at=MONDAY + timedelta(hours=6))
    second = trade("second", MONDAY + timedelta(hours=6))
    alone = replace(RiskConfig(), max_concurrent_positions=1)

    assert of_kind(discipline_flags((first, second), alone), "overtrading") == []


def test_the_concurrent_limit_comes_from_the_risk_config() -> None:
    """Passed in rather than defaulted, so a desk that moved it is honoured."""
    records = overlapping(3)

    assert [
        flag
        for flag in of_kind(discipline_flags(records), "overtrading")
        if "at once" in flag.detail
    ] == []

    tighter = replace(RiskConfig(), max_concurrent_positions=2)
    flags = [
        flag
        for flag in of_kind(discipline_flags(records, tighter), "overtrading")
        if "at once" in flag.detail
    ]

    assert len(flags) == 1
    assert "above the 2 in" in flags[0].detail


# --- trading against the engine ----------------------------------------------


def test_a_trade_taken_against_the_engine_is_flagged_as_its_own_group() -> None:
    records = (
        trade("with", MONDAY, agreed_with_bias=True),
        trade("against", MONDAY + timedelta(days=1), agreed_with_bias=False),
    )

    flags = of_kind(discipline_flags(records), "against_bias")

    assert len(flags) == 1
    assert flags[0].trade_id == "against"


def test_the_override_flag_names_the_side_that_was_taken() -> None:
    """Which way the trade went is half of what an override was.

    A flag that prints the opposite side reads in the review as a trade the
    owner did not take, and the engine's lean cannot be argued with from it.
    """
    records = (
        trade("long", MONDAY, direction=Direction.LONG, agreed_with_bias=False),
        trade(
            "short",
            MONDAY + timedelta(days=1),
            direction=Direction.SHORT,
            agreed_with_bias=False,
        ),
    )

    flags = of_kind(discipline_flags(records), "against_bias")

    assert "EURUSD long against" in flags[0].detail
    assert "EURUSD short against" in flags[1].detail
    assert [flag.occurred_at for flag in flags] == [
        record.opened_at for record in records
    ]


def test_each_override_is_its_own_flag() -> None:
    """Counted, so two overrides are two, not one finding about the week."""
    records = (
        trade("one", MONDAY, agreed_with_bias=False),
        trade("two", MONDAY + timedelta(days=1), agreed_with_bias=False),
    )

    flags = of_kind(discipline_flags(records), "against_bias")

    assert [flag.trade_id for flag in flags] == ["one", "two"]


def test_the_override_flag_does_not_call_the_trade_a_mistake() -> None:
    """The engine supplies a lean over days to weeks and the owner may have a
    technical reason to fade it.

    They are flagged so they can be counted and evaluated as a group. If the
    overrides consistently outperform, the model is the problem; if they
    consistently lose, the discipline is. Neither is known yet, and a flag that
    said otherwise would be a claim nobody has measured.
    """
    records = (trade("against", MONDAY, agreed_with_bias=False),)

    detail = of_kind(discipline_flags(records), "against_bias")[0].detail.lower()

    for word in (
        "mistake",
        "wrong",
        "error",
        "should not",
        "bad",
        "breach",
        "lapse",
        "failure",
        "avoidable",
        "undisciplined",
    ):
        assert word not in detail, detail
    assert "counted" in detail


# --- ordering, and what the function does not do ------------------------------


def test_flags_come_back_in_chronological_order() -> None:
    """Supplied out of order, because `load` sorts by open time and a caller
    building records by hand will not."""
    loss = loser("loss", MONDAY + timedelta(days=1))
    revenge = trade("revenge", loss.closed_at + timedelta(minutes=5))
    against = trade("against", MONDAY, agreed_with_bias=False)
    late = trade("late", MONDAY + timedelta(days=5), agreed_with_bias=False)

    flags = discipline_flags((late, revenge, against, loss))

    assert [flag.occurred_at for flag in flags] == sorted(
        flag.occurred_at for flag in flags
    )
    assert kinds(flags) == ["against_bias", "revenge", "against_bias"]


def test_nothing_is_read_from_disk_and_nothing_is_written_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It reads the records it is given, and only those.

    Falling back to `load` on an empty argument would be invisible here: a
    fresh clone has no journal, so the fallback returns nothing and the tests
    stay green while the function reads a file on the owner's own machine that
    the caller never asked for. Writing anything is the same problem in reverse,
    since the journal is append-only and this is a read.
    """
    monkeypatch.setattr("fbe.journal.JOURNAL_DIR", tmp_path)
    monkeypatch.setattr("fbe.journal.JOURNAL_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(
        "fbe.journal.load",
        lambda *args, **kwargs: pytest.fail("discipline_flags read the journal"),
    )

    assert list(discipline_flags(())) == []
    assert discipline_flags((loser(), trade("after", MONDAY + timedelta(hours=1))))
    assert list(tmp_path.iterdir()) == []


def test_the_records_are_not_altered() -> None:
    """It reports. Nothing here refuses, sizes or rewrites anything."""
    records = (loser(), trade("after", MONDAY + timedelta(hours=1, minutes=5)))
    before = tuple(replace(record) for record in records)

    discipline_flags(records)

    assert records == before


def test_one_record_can_carry_more_than_one_flag() -> None:
    """A trade can be revenge and an override at the same time, and the review
    needs both: one is a timing problem and the other is a model disagreement."""
    loss = loser()
    both = trade("both", loss.closed_at + timedelta(minutes=5), agreed_with_bias=False)

    flags = discipline_flags((loss, both))

    assert sorted(kinds(flags)) == ["against_bias", "revenge"]
    assert {flag.trade_id for flag in flags} == {"both"}
