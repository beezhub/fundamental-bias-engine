"""Per-limit outcomes, and the ticket that has to report the ones that did not run.

`check_limits` answered with ``list[str]``, empty meaning every limit passed. On
this account nothing supplies today's realised profit and loss and nothing holds
an equity peak, and no command supplied open positions at all, so an empty list
was mostly a record of checks that never ran. ADR 0002 rule 2: an unchecked
input produces a marker, not a pass. A pass is what authorises a trade.

Two kinds of test live here.

The contract tests run unguarded. They are about the shape of the answer, not
its content: the signature, the enumeration of limits, the fact that a report
holding a not-performed outcome cannot report itself all clear, and the fact
that a `fbe.types.PositionSize` cannot be mistaken for an open position.

The behaviour tests were guarded with a strict expected failure on
``NotImplementedError``. Strict was the point: the guard removes itself. The
guard on `fbe.risk.check_limits` has now done its job and is gone, and the
assertions underneath run against the real function. The three ticket tests keep
theirs, because `fbe.cli.size` is still scaffolded for Phase 4 and its contract
is in flight on #45.

Money figures are ZAR throughout, on the plan's R2,000 balance, so the
percentages in the assertions are the ones the trading plan quotes.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import fbe
from fbe.cli import app
from fbe.config import RiskConfig
from fbe.journal import TradeRecord
from fbe.risk import (
    LIMIT_CONCURRENT,
    LIMIT_CORRELATED,
    LIMIT_DAILY_LOSS,
    LIMIT_DRAWDOWN,
    LIMITS,
    LimitCheck,
    LimitReport,
    LimitStatus,
    OpenPosition,
    PositionRisk,
    check_limits,
)
from fbe.types import Conviction, Direction, PositionSize

PACKAGE_ROOT = Path(fbe.__file__).resolve().parent
RISK_DOC = Path(__file__).resolve().parents[1] / "docs" / "risk-and-execution.md"

BALANCE = 2000.0
"""The plan's account balance in ZAR. R20 at 1%, R40 at 2%."""

MAX_RISK = 40.0
"""Two percent of R2,000, the top of the plan's band and the configured maximum."""

JOURNAL_WRITTEN_AT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
"""Fixed modification time for the fixture journal, at midday UTC.

Midday so that a machine running several hours either side of UTC still renders
the same calendar date, and the assertion does not depend on the developer's
timezone.
"""

scaffolded_cli = pytest.mark.xfail(
    raises=NotImplementedError,
    strict=True,
    reason="fbe.cli.size is scaffolded; see docs/roadmap.md Phase 4",
)


def _proposed(
    pair: str = "USDJPY",
    realised_risk_amount: float = 35.81,
    balance: float = BALANCE,
) -> PositionSize:
    """Example B from section 2 of the risk doc: USDJPY short, 2% of R2,000.

    R40.00 intended, R35.81 actually on the book after 1,117 units round down to
    1,000 at a 0.01 lot minimum. The realised figure is the one every limit
    reads, because it is the one that can be lost.
    """
    return PositionSize(
        pair=pair,
        account_currency="ZAR",
        account_balance=balance,
        risk_fraction=0.02,
        risk_amount=40.0,
        realised_risk_amount=realised_risk_amount,
        entry=155.00,
        stop=155.30,
        stop_distance_pips=30.0,
        units=1000.0,
        lots=0.01,
        notional=18500.0,
    )


def _open_record(
    pair: str,
    risk_amount: float = MAX_RISK,
    balance: float = BALANCE,
    closed_at: datetime | None = None,
) -> TradeRecord:
    """One journal record, shaped as the journal holds it.

    ``risk_amount`` on a `fbe.journal.TradeRecord` is the realised figure, per
    that field's docstring. Only ``pair``, ``risk_amount`` and
    ``account_balance_at_entry`` are read by the limit checks; everything else
    here exists because the record requires it.
    """
    return TradeRecord(
        trade_id=f"{pair}-20260911T0900",
        pair=pair,
        direction=Direction.LONG,
        opened_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
        closed_at=closed_at,
        entry=1.0850,
        stop=1.0825,
        units=400.0,
        lots=0.004,
        risk_amount=risk_amount,
        risk_fraction=risk_amount / balance,
        account_balance_at_entry=balance,
        conviction=Conviction.HIGH,
        base_score=0.81,
        quote_score=-0.61,
        spread_score=1.42,
        config_digest="abc123",
    )


def _status(report: LimitReport, limit: str) -> LimitStatus:
    """The outcome recorded for one limit, failing loudly if it is absent."""
    for check in report.checks:
        if check.limit == limit:
            return check.status
    raise AssertionError(f"{limit} is not in the report: {report.checks}")


def _detail(report: LimitReport, limit: str) -> str:
    for check in report.checks:
        if check.limit == limit:
            return check.detail
    raise AssertionError(f"{limit} is not in the report: {report.checks}")


# --- the contract, which holds before anything is implemented ---------------


def test_the_two_untracked_inputs_default_to_not_tracked() -> None:
    """Criterion 1. ``0.0`` is a reading, so it cannot be the default.

    A default of 0.0 for ``realised_pnl_today`` is the exact anti-pattern ADR
    0002 rule 1 names: flat on the day and not tracked at all produced the same
    argument and therefore the same answer.
    """
    signature = inspect.signature(check_limits, eval_str=True)

    assert signature.parameters["realised_pnl_today"].default is None
    assert signature.parameters["realised_pnl_today"].annotation == float | None
    assert signature.parameters["equity_peak"].default is None
    assert signature.parameters["equity_peak"].annotation == float | None


def test_open_positions_can_say_not_known_and_has_no_default() -> None:
    """Criterion 4. Not known is expressible, and the caller has to state it.

    No default, because a caller that forgets the argument should not silently
    get "no positions open" from a function whose answer authorises a trade.
    """
    parameter = inspect.signature(check_limits, eval_str=True).parameters[
        "open_positions"
    ]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation == Sequence[OpenPosition] | None


def test_the_result_is_no_longer_a_list_of_reasons() -> None:
    """Criterion 2. ``list[str]`` with empty meaning pass is gone."""
    assert (
        inspect.signature(check_limits, eval_str=True).return_annotation is LimitReport
    )


def test_every_limit_is_enumerated_with_the_reason_it_exists() -> None:
    """A limit nobody understands is a limit that gets disabled when inconvenient."""
    assert tuple(LIMITS) == (
        LIMIT_CONCURRENT,
        LIMIT_CORRELATED,
        LIMIT_DAILY_LOSS,
        LIMIT_DRAWDOWN,
    )
    for limit, reason in LIMITS.items():
        assert hasattr(RiskConfig(), limit), f"{limit} is not a RiskConfig field"
        assert reason.strip(), f"{limit} has no stated reason"


def _doc_limit_table() -> list[str]:
    """The config keys named in section 4's limit table, in document order.

    Parsed rather than restated, for the reason ``tests/test_blockers.py`` gives:
    a second hand-written copy here would be a third place for the list to drift.
    """
    section = RISK_DOC.read_text().split("## 4. Limits")[1].split("\n## ")[0]
    rows = re.findall(r"^\|\s*`([^`]+)`\s*\|", section, re.MULTILINE)
    assert rows, f"no limit rows parsed from section 4:\n{section[:400]}"
    return rows


def test_the_doc_table_and_the_code_enumeration_match() -> None:
    assert _doc_limit_table() == list(LIMITS)


def test_a_report_holding_a_not_performed_outcome_is_not_all_clear() -> None:
    """Criterion 1, the half that matters. Not performed is not a pass.

    This is the whole issue in one assertion. Three of four limits cleared and
    one never ran is not "every limit passed", and no consumer may read it as
    one.
    """
    report = LimitReport(
        checks=(
            LimitCheck(LIMIT_CONCURRENT, LimitStatus.CLEAR, "0 open, limit 3"),
            LimitCheck(LIMIT_CORRELATED, LimitStatus.CLEAR, "USD 1.8%, limit 4.0%"),
            LimitCheck(LIMIT_DAILY_LOSS, LimitStatus.CLEAR, "ZAR 0.00 today"),
            LimitCheck(LIMIT_DRAWDOWN, LimitStatus.NOT_PERFORMED, "no equity peak"),
        )
    )

    assert report.all_clear is False
    assert report.breached == ()
    assert tuple(check.limit for check in report.not_performed) == (LIMIT_DRAWDOWN,)


def test_a_report_is_all_clear_only_when_every_limit_was_performed_and_passed() -> None:
    checks = tuple(LimitCheck(limit, LimitStatus.CLEAR, "checked") for limit in LIMITS)

    assert LimitReport(checks=checks).all_clear is True


def test_a_report_missing_a_limit_is_refused() -> None:
    """A partial report is the old defect in a new shape.

    Omitting a limit rather than reporting it as not performed would let the
    same silent pass back in through the consumer, which would iterate what it
    was given and find nothing wrong.
    """
    with pytest.raises(ValueError, match=LIMIT_DRAWDOWN):
        LimitReport(
            checks=tuple(
                LimitCheck(limit, LimitStatus.CLEAR, "checked")
                for limit in LIMITS
                if limit != LIMIT_DRAWDOWN
            )
        )


def test_a_journal_record_is_an_open_position_and_a_position_size_is_not() -> None:
    """Criterion 3. The check asks for the three fields it reads, and no more.

    A `fbe.types.PositionSize` deliberately does not satisfy the view. Its
    ``risk_amount`` is the INTENDED figure before rounding, and a structural
    match on that name would quietly size every limit off the wrong number, so
    the conversion goes through `fbe.risk.PositionRisk.from_position_size`,
    which reads ``realised_risk_amount``.
    """
    assert isinstance(_open_record("EURUSD"), OpenPosition)
    assert not isinstance(_proposed(), OpenPosition)


def test_the_proposed_position_converts_on_its_realised_risk() -> None:
    view = PositionRisk.from_position_size(_proposed())

    assert isinstance(view, OpenPosition)
    assert view.pair == "USDJPY"
    assert view.risk_amount == pytest.approx(35.81)
    assert view.account_balance_at_entry == pytest.approx(BALANCE)


def test_the_risk_module_does_not_import_the_journal() -> None:
    """Criterion 7. The command layer reads the journal and hands over the view.

    Asserted rather than trusted, because the import is the kind of shortcut
    that arrives with a sensible-looking commit message.
    """
    tree = ast.parse((PACKAGE_ROOT / "risk.py").read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert "fbe.journal" not in imported
    assert "journal" not in imported


def test_the_docstring_says_what_none_means() -> None:
    """The docstring is the deliverable on a stub, and this is what it must say."""
    docstring = (check_limits.__doc__ or "").lower()

    assert "not tracked" in docstring
    assert "not performed" in docstring
    assert "a reading" in docstring


# --- behaviour ---------------------------------------------------------------


def test_unknown_open_positions_do_not_clear_the_position_limits() -> None:
    """Criterion 4. Nothing to check against is not the same as nothing to find."""
    report = check_limits(None, _proposed(), RiskConfig())

    assert _status(report, LIMIT_CONCURRENT) is LimitStatus.NOT_PERFORMED
    assert _status(report, LIMIT_CORRELATED) is LimitStatus.NOT_PERFORMED
    assert report.all_clear is False


def test_known_empty_open_positions_clear_the_position_limits() -> None:
    """The other half of criterion 4. A read journal with no open trades passes."""
    report = check_limits((), _proposed(), RiskConfig())

    assert _status(report, LIMIT_CONCURRENT) is LimitStatus.CLEAR
    assert _status(report, LIMIT_CORRELATED) is LimitStatus.CLEAR


def test_the_concurrent_limit_breaches_at_the_configured_count() -> None:
    """Tests the wire: the count comes from config, not from a literal 3."""
    opens = [_open_record(pair) for pair in ("EURUSD", "GBPUSD")]
    config = RiskConfig(max_concurrent_positions=2)

    report = check_limits(opens, _proposed(), config)

    assert _status(report, LIMIT_CONCURRENT) is LimitStatus.BREACHED
    assert _status(report, LIMIT_CONCURRENT) is not LimitStatus.NOT_PERFORMED
    assert (
        _status(check_limits(opens, _proposed(), RiskConfig()), LIMIT_CONCURRENT)
        is LimitStatus.CLEAR
    )


def test_two_longs_against_the_dollar_are_one_short_dollar_bet() -> None:
    """Section 4's worked example, in rand.

    Two open longs at the configured maximum per-trade risk, R40.00 each on a
    R2,000 balance, put 2% on each of their legs. USD carries both, so it is
    already at the 4% cap. A third USD ticket at R40.00 takes USD to
    R120.00 / R2,000 = 6.0%, and the reason has to name the currency and the
    figure or the owner cannot tell which ticket to drop.

    The proposed ticket is passed at ``MAX_RISK`` rather than taking the
    helper's default, which is example B's realised R35.81 and would make the
    sum R115.81 / R2,000 = 5.8%. The document's worked example is the one this
    test claims to reproduce, so it has to be given the document's numbers.
    """
    opens = [_open_record("EURUSD"), _open_record("GBPUSD")]

    report = check_limits(
        opens, _proposed("AUDUSD", realised_risk_amount=MAX_RISK), RiskConfig()
    )

    assert _status(report, LIMIT_CORRELATED) is LimitStatus.BREACHED
    detail = _detail(report, LIMIT_CORRELATED)
    assert "USD" in detail
    assert re.search(r"6(\.\d+)?\s*%|0\.06", detail), detail


def test_the_correlated_limit_reads_the_configured_ceiling() -> None:
    """Tests the wire. Same book, a wider ceiling, a different answer."""
    opens = [_open_record("EURUSD"), _open_record("GBPUSD")]

    report = check_limits(
        opens, _proposed("AUDUSD"), RiskConfig(max_correlated_exposure=0.07)
    )

    assert _status(report, LIMIT_CORRELATED) is LimitStatus.CLEAR


def test_an_untracked_daily_loss_is_not_performed() -> None:
    report = check_limits((), _proposed(), RiskConfig())

    assert _status(report, LIMIT_DAILY_LOSS) is LimitStatus.NOT_PERFORMED


def test_a_flat_day_is_a_reading_and_clears_the_daily_loss_limit() -> None:
    """``0.0`` means flat, which is a fact about the account, not a silence."""
    report = check_limits((), _proposed(), RiskConfig(), realised_pnl_today=0.0)

    assert _status(report, LIMIT_DAILY_LOSS) is LimitStatus.CLEAR


def test_the_daily_loss_limit_breaches_at_four_percent_of_balance() -> None:
    """R80.00 lost on R2,000 is the 4% default. Negative is a loss."""
    report = check_limits((), _proposed(), RiskConfig(), realised_pnl_today=-80.0)

    assert _status(report, LIMIT_DAILY_LOSS) is LimitStatus.BREACHED
    assert (
        _status(
            check_limits((), _proposed(), RiskConfig(), realised_pnl_today=-79.99),
            LIMIT_DAILY_LOSS,
        )
        is LimitStatus.CLEAR
    )


def test_an_untracked_equity_peak_is_not_performed() -> None:
    """Nothing in this repository holds a peak, so this is every run today."""
    report = check_limits((), _proposed(), RiskConfig())

    assert _status(report, LIMIT_DRAWDOWN) is LimitStatus.NOT_PERFORMED


def test_a_balance_at_its_peak_clears_the_drawdown_limit() -> None:
    report = check_limits((), _proposed(), RiskConfig(), equity_peak=BALANCE)

    assert _status(report, LIMIT_DRAWDOWN) is LimitStatus.CLEAR


def test_the_drawdown_limit_breaches_ten_percent_below_the_peak() -> None:
    """R2,000 against a R2,223 peak is 10.03% down. R2,223 x 0.9 = R2,000.70."""
    report = check_limits((), _proposed(), RiskConfig(), equity_peak=2223.0)

    assert _status(report, LIMIT_DRAWDOWN) is LimitStatus.BREACHED
    assert (
        _status(
            check_limits((), _proposed(), RiskConfig(), equity_peak=2222.0),
            LIMIT_DRAWDOWN,
        )
        is LimitStatus.CLEAR
    )


# --- what a live sizing run can actually supply, per #46 ---------------------


def test_a_caller_with_nothing_to_supply_gets_four_not_performed() -> None:
    """The state of every live run today, and the answer this issue turns on.

    The owner's answer to Q1 on #46: trades are not journalled by hand at entry,
    they arrive as a broker statement once a day. So the journal describes
    completed days and knows nothing about the book right now, and a caller with
    an honest view of what it holds passes ``None`` three times. All four limits
    come back not performed, and the report is not all clear.

    This is the outcome the old ``list[str]`` contract could not express: it
    returned an empty list, and empty meant take the trade.
    """
    report = check_limits(None, _proposed(), RiskConfig())

    assert (
        tuple(check.status for check in report.checks)
        == (LimitStatus.NOT_PERFORMED,) * 4
    )
    assert report.all_clear is False
    assert report.breached == ()
    assert len(report.not_performed) == 4


def test_every_not_performed_detail_names_the_input_it_wanted() -> None:
    """A not-performed line that does not say what was missing cannot be acted on.

    The owner's next step is to check that one limit by hand, and which
    terminal screen to open differs per limit. A bare "not performed" sends
    them to all four.
    """
    report = check_limits(None, _proposed(), RiskConfig())

    assert "open" in _detail(report, LIMIT_CONCURRENT).lower()
    assert "open" in _detail(report, LIMIT_CORRELATED).lower()
    assert "realised_pnl_today" in _detail(report, LIMIT_DAILY_LOSS)
    assert "equity_peak" in _detail(report, LIMIT_DRAWDOWN)


def test_a_flat_day_and_an_untracked_day_are_different_answers() -> None:
    """The pair of readings ADR 0002 rule 1 names, asserted side by side.

    ``0.0`` is a fact about the account: nothing closed, the day is flat.
    ``None`` is the absence of that fact. Collapsing them is the original
    defect, and it is the one #46's second amended criterion asks to be pinned,
    so the two calls are in one test rather than two files apart.
    """
    flat = check_limits((), _proposed(), RiskConfig(), realised_pnl_today=0.0)
    untracked = check_limits((), _proposed(), RiskConfig(), realised_pnl_today=None)

    assert _status(flat, LIMIT_DAILY_LOSS) is LimitStatus.CLEAR
    assert _status(untracked, LIMIT_DAILY_LOSS) is LimitStatus.NOT_PERFORMED
    assert tuple(check.limit for check in flat.not_performed) == (LIMIT_DRAWDOWN,)
    assert tuple(check.limit for check in untracked.not_performed) == (
        LIMIT_DAILY_LOSS,
        LIMIT_DRAWDOWN,
    )


def test_no_check_comes_back_without_something_to_read() -> None:
    """`LimitCheck.detail` is documented as never empty, on every path.

    A check with nothing to say about itself is indistinguishable from one that
    was never run, which is the distinction this whole report exists to make.
    """
    reports = (
        check_limits(None, _proposed(), RiskConfig()),
        check_limits((), _proposed(), RiskConfig(), 0.0, BALANCE),
        check_limits(
            [_open_record("EURUSD"), _open_record("GBPUSD")],
            _proposed("AUDUSD", realised_risk_amount=MAX_RISK),
            RiskConfig(),
            -80.0,
            2223.0,
        ),
    )

    for report in reports:
        assert tuple(check.limit for check in report.checks) == tuple(LIMITS)
        for check in report.checks:
            assert check.detail.strip(), check


def test_landing_exactly_on_the_correlated_ceiling_is_not_a_breach() -> None:
    """Strictly above, per the docstring. Holding the maximum is not exceeding it.

    One open EURUSD at R40.00 puts 2% on USD. A proposed AUDUSD at R40.00 adds
    2%, which lands USD on the configured 4% exactly. An off-by-one here refuses
    a trade the plan allows, every time the book is full but legal.
    """
    report = check_limits(
        [_open_record("EURUSD")],
        _proposed("AUDUSD", realised_risk_amount=MAX_RISK),
        RiskConfig(),
    )

    assert _status(report, LIMIT_CORRELATED) is LimitStatus.CLEAR
    assert "4.0%" in _detail(report, LIMIT_CORRELATED)


def test_the_concurrent_limit_breaches_on_the_count_that_leaves_no_room() -> None:
    """At the ceiling, not past it: the question is whether one more fits."""
    one_open = [_open_record("EURUSD")]

    assert (
        _status(
            check_limits(one_open, _proposed(), RiskConfig(max_concurrent_positions=1)),
            LIMIT_CONCURRENT,
        )
        is LimitStatus.BREACHED
    )
    assert (
        _status(
            check_limits(one_open, _proposed(), RiskConfig(max_concurrent_positions=2)),
            LIMIT_CONCURRENT,
        )
        is LimitStatus.CLEAR
    )


def test_the_daily_loss_limit_reads_the_configured_fraction() -> None:
    """Tests the wire. Same loss, a tighter limit, a different answer."""
    at_fifty_down = -50.0

    assert (
        _status(
            check_limits(
                (), _proposed(), RiskConfig(max_daily_loss=0.01), at_fifty_down
            ),
            LIMIT_DAILY_LOSS,
        )
        is LimitStatus.BREACHED
    )
    assert (
        _status(
            check_limits((), _proposed(), RiskConfig(), at_fifty_down),
            LIMIT_DAILY_LOSS,
        )
        is LimitStatus.CLEAR
    )


def test_the_drawdown_limit_reads_the_configured_fraction() -> None:
    """Tests the wire. R2,000 against a R2,100 peak is 4.8% down."""
    peak = 2100.0

    assert (
        _status(
            check_limits(
                (), _proposed(), RiskConfig(max_drawdown_pause=0.01), equity_peak=peak
            ),
            LIMIT_DRAWDOWN,
        )
        is LimitStatus.BREACHED
    )
    assert (
        _status(
            check_limits((), _proposed(), RiskConfig(), equity_peak=peak),
            LIMIT_DRAWDOWN,
        )
        is LimitStatus.CLEAR
    )


def test_the_drawdown_limit_breaches_on_the_threshold_itself() -> None:
    """At or beyond, not past. The boundary is chosen so it is exact in binary.

    A 50% pause against a R4,000 peak puts the threshold on R2,000.00 exactly,
    which is the balance. ``<`` instead of ``<=`` would clear it, and the
    ordinary fixture cannot tell the two apart: R2,223 x 0.9 is R2,000.70, which
    a strictly-less comparison breaches as well.
    """
    report = check_limits(
        (),
        _proposed(),
        RiskConfig(max_drawdown_pause=0.5),
        equity_peak=4000.0,
    )

    assert _status(report, LIMIT_DRAWDOWN) is LimitStatus.BREACHED


def test_the_money_limits_use_the_balance_the_size_was_derived_from() -> None:
    """Sizing against one balance and checking against another is a silent gap.

    ``--balance`` on the command line overrides the configured figure for one
    ticket. If the limits kept reading `RiskConfig.account_balance`, a R2,000
    ticket on an account configured at R10,000 would be measured against R400 of
    daily loss rather than R80, and the stop for the session would not fire
    until five times the intended loss.
    """
    config = RiskConfig(account_balance=10_000.0)

    report = check_limits((), _proposed(balance=BALANCE), config, -80.0, 2223.0)

    assert _status(report, LIMIT_DAILY_LOSS) is LimitStatus.BREACHED
    assert _status(report, LIMIT_DRAWDOWN) is LimitStatus.BREACHED


def test_the_proposed_position_is_weighed_on_what_it_actually_risks() -> None:
    """The intended figure is not the one on the book, and the gap decides this.

    R40.00 intended rounds down to R20.00 realised on this ticket. Against a
    5.5% ceiling with USD already carrying 4%, the realised figure clears at 5%
    and the intended one would breach at 6%. Reading the wrong field here
    refuses trades on exposure the account does not carry.
    """
    opens = [_open_record("EURUSD"), _open_record("GBPUSD")]
    proposed = _proposed("AUDUSD", realised_risk_amount=20.0)

    report = check_limits(opens, proposed, RiskConfig(max_correlated_exposure=0.055))

    assert proposed.risk_amount == pytest.approx(40.0)
    assert _status(report, LIMIT_CORRELATED) is LimitStatus.CLEAR
    assert "5.0%" in _detail(report, LIMIT_CORRELATED)


def test_the_correlated_breach_names_the_currency_and_both_figures() -> None:
    """Which ticket to drop is the decision the line has to support."""
    opens = [_open_record("EURUSD"), _open_record("GBPUSD")]

    detail = _detail(
        check_limits(
            opens, _proposed("AUDUSD", realised_risk_amount=MAX_RISK), RiskConfig()
        ),
        LIMIT_CORRELATED,
    )

    assert "USD" in detail
    assert "6.0%" in detail
    assert "4.0%" in detail


@pytest.mark.parametrize("balance", [0.0, -100.0, float("nan"), float("inf")])
def test_a_balance_that_is_not_money_is_refused(balance: float) -> None:
    """Every money comparison here is a share of the balance.

    A zero has no fraction to report, a negative one inverts both money limits,
    and a NaN passes every comparison by failing it. A refusal is the only
    honest answer, and it is the same rule `position_size` already applies to
    the configured balance.
    """
    with pytest.raises(ValueError, match="account_balance"):
        check_limits((), _proposed(balance=balance), RiskConfig())


@pytest.mark.parametrize("peak", [0.0, -1.0, float("nan")])
def test_an_equity_peak_that_is_not_money_is_refused(peak: float) -> None:
    """Absent is ``None`` and is answered with not performed; nonsense is refused.

    The two must not collapse into each other. A peak of 0.0 would make the
    drawdown threshold 0.0 and clear every balance forever, which is a limit
    that reports clear on no evidence.
    """
    with pytest.raises(ValueError, match="equity_peak"):
        check_limits((), _proposed(), RiskConfig(), equity_peak=peak)


@pytest.mark.parametrize("pnl", [float("nan"), float("inf"), float("-inf")])
def test_a_daily_loss_that_is_not_a_number_is_refused(pnl: float) -> None:
    """Absence is ``None``. A NaN is a different failure and is not quieter.

    A NaN compares False against the threshold, so the daily-loss limit would
    report clear on a figure nobody can read, which is the one outcome that
    authorises a trade. Reporting it as not performed would be honest about the
    limit and silent about the caller, and the caller is the thing that is
    broken.
    """
    with pytest.raises(ValueError, match="realised_pnl_today"):
        check_limits((), _proposed(), RiskConfig(), realised_pnl_today=pnl)


def test_a_flat_day_survives_the_refusal_that_a_peak_of_zero_gets() -> None:
    """The asymmetry between the two money inputs, stated as a test.

    Zero is a reading for a day's profit and loss and nonsense for an equity
    peak, so one validation cannot serve both. A shared check that refused zero
    everywhere would delete the flat-day reading, which is the case ADR 0002
    rule 1 exists to preserve.
    """
    flat = check_limits((), _proposed(), RiskConfig(), realised_pnl_today=0.0)

    assert _status(flat, LIMIT_DAILY_LOSS) is LimitStatus.CLEAR
    with pytest.raises(ValueError, match="equity_peak"):
        check_limits((), _proposed(), RiskConfig(), equity_peak=0.0)


def test_a_supplied_input_is_never_reported_as_not_performed() -> None:
    """The other direction of the same rule: what was checked says it was checked."""
    report = check_limits(
        [_open_record("EURUSD")],
        _proposed(),
        RiskConfig(),
        realised_pnl_today=-10.0,
        equity_peak=2100.0,
    )

    assert report.not_performed == ()
    assert report.all_clear is True


# --- what the documents have to say about all this ---------------------------


def _risk_section(heading: str) -> str:
    """One section of the risk document, by its heading, with runs of whitespace
    collapsed to single spaces.

    The document is hard-wrapped at 80 columns, so a phrase can fall either side
    of a line break, and a test that asserted on the raw text would pass or fail
    on where a sentence happened to wrap. Normalising here means the assertions
    below are about what the section says rather than about its layout.
    """
    body = RISK_DOC.read_text()
    assert heading in body, heading
    section = body.split(heading)[1].split("\n## ")[0]
    return " ".join(section.split())


def test_section_four_says_the_journal_describes_completed_days() -> None:
    """Criterion 3. The document may not claim a limit is automated when it is not.

    Before #46 this section said `fbe size` counts the journal's open records
    and that the concurrent and correlated limits are performed on that basis.
    The owner's answer to Q1 makes that false: the journal arrives as a daily
    broker statement, so it describes completed days and is blind to the book
    the trade is being sized into.
    """
    section = _risk_section("## 4. Limits, and what each one is for").lower()

    assert "completed days" in section
    assert "all four" in section
    assert "by hand" in section
    assert "daily statement" in section


def test_section_four_no_longer_claims_two_limits_are_performed() -> None:
    """The specific sentence that was wrong, asserted gone rather than assumed."""
    section = _risk_section("## 4. Limits, and what each one is for").lower()

    assert "the concurrent and correlated limits are performed" not in section


def test_section_eight_tells_the_owner_which_limits_are_theirs() -> None:
    """Criterion 1. Q1's policy is stated where the box is ticked.

    "Today that is the last two every time" was true when two limits had no
    source. It is now all four, and a checklist that understates which boxes
    the reader owns is worse than one that says nothing.
    """
    section = _risk_section("## 8. Pre-trade checklist").lower()

    assert "all four" in section
    assert "last two every time" not in section


# --- the ticket, guarded until Phase 4 --------------------------------------


def _journal_line(record: TradeRecord) -> str:
    """One JSONL line, written by hand because `fbe.journal.append` is scaffolded."""
    payload: dict[str, Any] = {
        "trade_id": record.trade_id,
        "pair": record.pair,
        "direction": record.direction.value,
        "opened_at": record.opened_at.isoformat(),
        "closed_at": record.closed_at.isoformat() if record.closed_at else None,
        "entry": record.entry,
        "stop": record.stop,
        "units": record.units,
        "lots": record.lots,
        "risk_amount": record.risk_amount,
        "risk_fraction": record.risk_fraction,
        "account_balance_at_entry": record.account_balance_at_entry,
        "account_currency": record.account_currency,
        "conviction": record.conviction.value,
        "base_score": record.base_score,
        "quote_score": record.quote_score,
        "spread_score": record.spread_score,
        "config_digest": record.config_digest,
    }
    return json.dumps(payload)


@pytest.fixture
def fixture_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A journal holding one open trade and one closed one, at a fixed write time."""
    path = tmp_path / "trades.jsonl"
    path.write_text(
        "\n".join(
            (
                _journal_line(_open_record("EURUSD")),
                _journal_line(
                    _open_record(
                        "GBPUSD",
                        closed_at=datetime(2026, 9, 11, 11, 0, tzinfo=UTC),
                    )
                ),
            )
        )
        + "\n"
    )
    stamp = JOURNAL_WRITTEN_AT.timestamp()
    os.utime(path, (stamp, stamp))
    monkeypatch.setattr("fbe.journal.JOURNAL_PATH", path)
    return path


def _size(pair: str = "USDJPY") -> str:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["size", pair, "--entry", "155.00", "--stop", "155.30"],
        catch_exceptions=False,
    )
    return result.stdout


@scaffolded_cli
def test_the_ticket_states_what_the_limits_were_checked_against(
    fixture_journal: Path,
) -> None:
    """Criteria 5 and 8. The basis is printed, and not performed says so.

    Four things have to be on the ticket: how many open positions were counted,
    where they were read from, when that file was last written, and one line per
    limit. Without the first three, a reader cannot tell a clear concurrent
    limit from a journal that was three days stale.
    """
    output = _size()

    assert re.search(r"\b1\b[^\n]*open", output, re.IGNORECASE), output
    assert str(fixture_journal) in output
    assert "2026-09-11" in output
    for limit in LIMITS:
        assert limit in output, f"{limit} is missing from the ticket:\n{output}"
    assert "not performed" in output.lower()


@scaffolded_cli
def test_an_unreadable_journal_says_open_positions_are_not_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 9, first half. A corrupt file is not an empty book.

    `fbe.journal.load` raises on an unparseable line rather than skipping it, and
    the ticket has to carry that through as not known rather than as zero.
    """
    path = tmp_path / "trades.jsonl"
    path.write_text("{not json at all\n")
    monkeypatch.setattr("fbe.journal.JOURNAL_PATH", path)

    output = _size()

    assert "not known" in output.lower()
    assert output.lower().count("not performed") >= 2


@scaffolded_cli
def test_an_absent_journal_is_zero_open_positions_and_names_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 9, second half. A fresh install legitimately has no trades."""
    path = tmp_path / "trades.jsonl"
    monkeypatch.setattr("fbe.journal.JOURNAL_PATH", path)

    output = _size()

    assert re.search(r"\b0\b[^\n]*open", output, re.IGNORECASE), output
    assert str(path) in output
