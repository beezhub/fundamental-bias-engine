"""`fbe journal import` reads the broker's open-positions export into the journal.

Issue #348, from proposal #347 and the owner's own design. The owner places
every trade by hand, and the export carries everything but the stop loss, so
the command reads the file and asks for the stop, one trade at a time. The
ticket becomes the trade's ID, which also fixes two trades in one pair and
minute overwriting each other.

The fixture uses the export's real header with made-up tickets and prices:
the owner's own positions are a private record and never enter the repository.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fbe.broker_export import (
    OPEN_POSITIONS_HEADER,
    ExportFormatError,
    read_open_positions,
)
from fbe.cli import _EntryView, app
from fbe.journal import BlackoutCheck, load
from fbe.types import Conviction, Direction

runner = CliRunner()

HEADER = ",".join(OPEN_POSITIONS_HEADER)
ROWS = (
    "1000000003,2026-10-06T16:43:56,sell,0.01,EURAUDm,1.61196,",
    "1000000002,2026-10-06T15:37:29,buy,0.01,USDJPYm,158.062,",
    "1000000001,2026-10-06T15:37:14,buy,0.01,USDJPYm,158.072,",
)


def export(tmp_path: Path, *rows: str, header: str = HEADER) -> Path:
    path = tmp_path / "open.csv"
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


# --- the reader ----------------------------------------------------------------------


def test_the_real_header_is_the_contract() -> None:
    assert OPEN_POSITIONS_HEADER == (
        "ticket",
        "opening_time_utc",
        "type",
        "original_position_size",
        "symbol",
        "opening_price",
        "commission",
    )


def test_each_row_becomes_a_position(tmp_path: Path) -> None:
    positions = read_open_positions(export(tmp_path, *ROWS))

    first = positions[0]
    assert len(positions) == 3
    assert first.ticket == "1000000003"
    assert first.pair == "EURAUD"
    assert first.direction is Direction.SHORT
    assert first.lots == 0.01
    assert first.entry == 1.61196
    assert first.opened_at == datetime(2026, 10, 6, 16, 43, 56, tzinfo=UTC)
    assert positions[1].direction is Direction.LONG


def test_a_changed_header_is_refused_naming_the_difference(tmp_path: Path) -> None:
    changed = HEADER.replace("opening_price", "open_price")

    with pytest.raises(ExportFormatError, match="opening_price"):
        read_open_positions(export(tmp_path, *ROWS, header=changed))


def test_a_symbol_outside_the_universe_is_refused_naming_its_row(
    tmp_path: Path,
) -> None:
    gold = "1000000009,2026-10-06T10:00:00,buy,0.01,XAUUSDm,2400.5,"

    with pytest.raises(ExportFormatError, match="row 2.*XAUUSD"):
        read_open_positions(export(tmp_path, gold))


def test_a_side_that_is_neither_buy_nor_sell_is_refused(tmp_path: Path) -> None:
    odd = "1000000009,2026-10-06T10:00:00,buy_limit,0.01,EURUSDm,1.08,"

    with pytest.raises(ExportFormatError, match="buy_limit"):
        read_open_positions(export(tmp_path, odd))


# --- the command ---------------------------------------------------------------------


SNAPSHOT = _EntryView(
    conviction=Conviction.MEDIUM,
    base_score=0.83,
    quote_score=-0.67,
    spread_score=1.50,
    base_pillars={},
    quote_pillars={},
    config_digest="abc123def456",
    agreed_with_bias=True,
    direction=Direction.LONG,
)


@pytest.fixture
def journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A USD account of $100, the owner's, with the bias chain replaced by a
    fixed snapshot so these tests are about the import, not the scoring."""
    config = tmp_path / "config.yaml"
    config.write_text(
        f"data:\n"
        f"  journal_dir: {tmp_path}\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"risk:\n"
        f"  account_currency: USD\n"
        f"  account_balance: 100.0\n"
        f"broker:\n"
        f"  name: exness-standard\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "fbe.cli._entry_view",
        lambda config, pair, direction, run_date, trade_id: SNAPSHOT,
    )
    return tmp_path


def run(journal_dir: Path, args: list[str], answers: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["--config", str(journal_dir / "config.yaml"), "journal", *args],
        input=answers,
    )
    output = result.output
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        output = f"{output}\n{result.exception!r}"
    return result.exit_code, output


def recorded(journal_dir: Path) -> dict[str, object]:
    return {r.trade_id: r for r in load(path=journal_dir / "trades.jsonl")}


def test_it_asks_for_the_stop_and_records_by_ticket(journal_dir: Path) -> None:
    path = export(journal_dir, ROWS[0])

    code, out = run(journal_dir, ["import", str(path)], "1.61500\n1.60500\n")

    assert code == 0, out
    assert "1 open position in the file, 0 already in the journal." in out
    assert "EURAUD sell 0.01 at 1.61196, ticket 1000000003" in out
    trade = recorded(journal_dir)["1000000003"]
    assert trade.stop == 1.615  # type: ignore[attr-defined]
    assert trade.target == 1.605  # type: ignore[attr-defined]
    assert trade.direction is Direction.SHORT  # type: ignore[attr-defined]
    assert trade.account_currency == "USD"  # type: ignore[attr-defined]


def test_the_take_profit_is_optional(journal_dir: Path) -> None:
    path = export(journal_dir, ROWS[0])

    run(journal_dir, ["import", str(path)], "1.61500\n\n")

    assert recorded(journal_dir)["1000000003"].target is None  # type: ignore[attr-defined]


def test_enter_skips_a_trade_and_records_nothing_for_it(journal_dir: Path) -> None:
    path = export(journal_dir, ROWS[0], ROWS[1])

    code, out = run(journal_dir, ["import", str(path)], "\n158.000\n\n")

    assert code == 0, out
    assert set(recorded(journal_dir)) == {"1000000002"}
    assert "Skipped" in out


def test_a_stop_on_the_wrong_side_is_refused_and_asked_again(journal_dir: Path) -> None:
    """A sell's stop is above the entry. 1.60 below it is a slip, not a stop."""
    path = export(journal_dir, ROWS[0])

    code, out = run(journal_dir, ["import", str(path)], "1.60000\n1.61500\n\n")

    assert code == 0, out
    assert "above the entry" in out
    assert recorded(journal_dir)["1000000003"].stop == 1.615  # type: ignore[attr-defined]


def test_the_risk_the_stop_implies_is_shown_in_pips(journal_dir: Path) -> None:
    """EURAUD sell at 1.61196 with a stop at 1.61500 risks 30.4 pips."""
    path = export(journal_dir, ROWS[0])

    _, out = run(journal_dir, ["import", str(path)], "1.61500\n\n")

    assert "1R = 30.4 pips" in out


def test_running_it_again_skips_what_is_already_recorded(journal_dir: Path) -> None:
    path = export(journal_dir, *ROWS)
    run(journal_dir, ["import", str(path)], "1.61500\n\n158.000\n\n158.000\n\n")

    code, out = run(journal_dir, ["import", str(path)], "")

    assert code == 0, out
    assert "3 open positions in the file, 3 already in the journal." in out
    assert len(recorded(journal_dir)) == 3


def test_two_trades_in_one_minute_are_both_kept(journal_dir: Path) -> None:
    """The owner's case: USDJPY at 15:37:14 and 15:37:29. By pair and minute
    they shared an ID and the second overwrote the first."""
    path = export(journal_dir, ROWS[1], ROWS[2])

    run(journal_dir, ["import", str(path)], "158.000\n\n158.000\n\n")

    trades = recorded(journal_dir)
    assert set(trades) == {"1000000001", "1000000002"}
    assert {t.entry for t in trades.values()} == {158.062, 158.072}  # type: ignore[attr-defined]


def test_an_imported_trade_takes_the_journal_add_path(
    journal_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bias snapshot at entry, the entry-time news check and the broker,
    as for a hand-entered trade."""
    monkeypatch.setattr(
        "fbe.cli._entry_blackout",
        lambda config, pair, when: (BlackoutCheck.CLEAR, None),
    )
    path = export(journal_dir, ROWS[0])

    run(journal_dir, ["import", str(path)], "1.61500\n\n")

    trade = recorded(journal_dir)["1000000003"]
    assert trade.config_digest == "abc123def456"  # type: ignore[attr-defined]
    assert trade.conviction is Conviction.MEDIUM  # type: ignore[attr-defined]
    assert trade.blackout_check is BlackoutCheck.CLEAR  # type: ignore[attr-defined]
    assert trade.broker == "exness-standard"  # type: ignore[attr-defined]
    assert trade.opened_at == datetime(2026, 10, 6, 16, 43, 56, tzinfo=UTC)  # type: ignore[attr-defined]


def test_a_changed_file_is_refused_before_anything_is_asked(journal_dir: Path) -> None:
    path = export(journal_dir, *ROWS, header=HEADER.replace("symbol", "instrument"))

    code, out = run(journal_dir, ["import", str(path)], "")

    assert code != 0
    assert "symbol" in out
    assert not (journal_dir / "trades.jsonl").exists()


# --- journal add gains --ticket and --target ------------------------------------------


def test_journal_add_keeps_two_same_minute_trades_apart_by_ticket(
    journal_dir: Path,
) -> None:
    common = ["add", "USDJPY", "-d", "long", "-s", "158.000", "--lots", "0.01"]
    run(
        journal_dir,
        [*common, "-e", "158.072", "--opened", "2026-10-06 15:37", "--ticket", "A1"],
        "",
    )
    run(
        journal_dir,
        [*common, "-e", "158.062", "--opened", "2026-10-06 15:37", "--ticket", "A2"],
        "",
    )

    assert set(recorded(journal_dir)) == {"A1", "A2"}


def test_journal_add_records_a_target(journal_dir: Path) -> None:
    run(
        journal_dir,
        [
            "add",
            "EURUSD",
            "-d",
            "long",
            "-e",
            "1.0850",
            "-s",
            "1.0820",
            "--lots",
            "0.01",
            "--opened",
            "2026-10-06 09:00",
            "--target",
            "1.0910",
        ],
        "",
    )

    (trade,) = recorded(journal_dir).values()
    assert trade.target == 1.091  # type: ignore[attr-defined]
