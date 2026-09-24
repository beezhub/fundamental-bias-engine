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


def trade(
    trade_id: str,
    opened_at: datetime,
    *,
    pair: str = "EURUSD",
    closed_at: datetime | None = None,
    duration: timedelta | None = timedelta(minutes=30),
    r_multiple: float | None = 0.6,
    risk_amount: float | None = 37.0,
    agreed_with_bias: bool = True,
) -> TradeRecord:
    """One journal record, varying only what a rule below reads.

    The defaults describe a trade that was opened, closed half an hour later
    for a modest win, agreed with the engine and broke none of the three rules,
    so a test that changes one field is testing that field. Pass
    ``duration=None`` for a position that is still open, which is a different
    record and a different answer to every rule here.
    """
    closed = opened_at + duration if closed_at is None and duration else closed_at
    return TradeRecord(
        trade_id=trade_id,
        pair=pair,
        direction=Direction.SHORT,
        opened_at=opened_at,
        entry=1.0850,
        stop=1.0870,
        units=1000.0,
        lots=0.01,
        risk_amount=risk_amount,
        risk_fraction=None if risk_amount is None else risk_amount / 2000.0,
        account_balance_at_entry=2000.0,
        conviction=Conviction.MEDIUM,
        base_score=-1.52,
        quote_score=1.84,
        spread_score=-3.36,
        config_digest="7f3c1a9de204",
        closed_at=closed,
        exit_price=1.0800 if closed is not None else None,
        outcome_zar=None if r_multiple is None else r_multiple * (risk_amount or 0.0),
        r_multiple=None if closed is None else r_multiple,
        agreed_with_bias=agreed_with_bias,
        blackout_check=BlackoutCheck.CLEAR,
    )


def loser(
    trade_id: str = "loss",
    closed_at: datetime | None = None,
    *,
    pair: str = "EURUSD",
    risk_amount: float | None = 37.0,
) -> TradeRecord:
    """A closed trade that lost, which is what starts the revenge window."""
    closed = MONDAY + timedelta(hours=1) if closed_at is None else closed_at
    return trade(
        trade_id,
        opened_at=closed - timedelta(hours=4),
        pair=pair,
        closed_at=closed,
        r_multiple=-1.0,
        risk_amount=risk_amount,
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


def overlapping(count: int) -> tuple[TradeRecord, ...]:
    """``count`` trades opened an hour apart and all still open together."""
    return tuple(
        trade(
            f"o{index}",
            MONDAY + timedelta(hours=index),
            closed_at=MONDAY + timedelta(days=3),
        )
        for index in range(count)
    )


def test_more_positions_open_at_once_than_the_limit_allows_is_flagged() -> None:
    """And the flag says the limit was bypassed, because it was.

    `check_limits` refuses this before the trade is taken, so a journal holding
    it means the refusal was overridden or never consulted. That is a different
    fact from a busy week and the review has to be able to tell them apart.
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
    assert "check_limits" in flags[0].detail


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


# --- trading against the engine ----------------------------------------------


def test_a_trade_taken_against_the_engine_is_flagged_as_its_own_group() -> None:
    records = (
        trade("with", MONDAY, agreed_with_bias=True),
        trade("against", MONDAY + timedelta(days=1), agreed_with_bias=False),
    )

    flags = of_kind(discipline_flags(records), "against_bias")

    assert len(flags) == 1
    assert flags[0].trade_id == "against"


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

    for word in ("mistake", "wrong", "error", "should not", "bad"):
        assert word not in detail, detail
    assert "counted" in detail or "group" in detail


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
