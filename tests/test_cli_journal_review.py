"""`fbe journal review`, the weekly look back over the journal.

Issue #265, ruled 2026-10-06. The command reads the journal, prints the plain
numbers, then the two splits that change behaviour: followed the plan or broke
it, with the engine's bias or against it. Every statistic comes from
`fbe.journal`, never from the command. Every figure carries its sample, and a
bucket under `EVIDENCE_THRESHOLD_TRADES` closed trades is marked a record, not
evidence, which on this journal is every bucket for months.

The journal lives in ``tmp_path`` through ``data.journal_dir``, never the real
one, and nothing reaches the network.
"""

from __future__ import annotations

import ast
import csv
import inspect
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fbe import cli
from fbe.cli import app
from fbe.journal import (
    TradeRecord,
    append,
    evaluate_by_bias_agreement,
    evaluate_by_plan,
    summarise,
)
from fbe.types import Conviction
from tests.test_journal import record

runner = CliRunner()
TODAY = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]


def opened(day: int, month: int = 10) -> datetime:
    return datetime(2026, month, day, 9, 0, tzinfo=UTC)


def closed_trade(
    trade_id: str,
    pair: str,
    day: int,
    r: float,
    zar: float | None,
    *,
    conviction: Conviction = Conviction.MEDIUM,
    followed: bool = True,
    agreed: bool = True,
    tags: tuple[str, ...] = (),
    month: int = 10,
) -> TradeRecord:
    return record(
        trade_id,
        pair=pair,
        opened_at=opened(day, month),
        conviction=conviction,
        closed_at=opened(day, month).replace(hour=15),
        exit_price=1.0900,
        r_multiple=r,
        outcome_zar=zar,
        followed_plan=followed,
        agreed_with_bias=agreed,
        tags=tags,
    )


WEEK = (
    closed_trade(
        "T1", "EURUSD", 1, 1.5, 30.00, conviction=Conviction.HIGH, tags=("channel",)
    ),
    closed_trade("T2", "GBPUSD", 2, -1.0, -20.00),
    closed_trade(
        "T3",
        "USDJPY",
        5,
        2.0,
        40.00,
        conviction=Conviction.LOW,
        agreed=False,
        tags=("channel",),
    ),
    closed_trade("T4", "AUDUSD", 5, -1.0, -20.00, followed=False),
    record(
        "T5",
        pair="EURGBP",
        opened_at=opened(5),
        conviction=Conviction.HIGH,
        tags=("breakout",),
    ),
)
OLD = closed_trade("T0", "NZDUSD", 15, 1.0, 20.00, month=9)


def journal_of(tmp_path: Path, *records: TradeRecord) -> Path:
    path = tmp_path / "trades.jsonl"
    for item in records:
        append(item, path)
    return path


def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, str]:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"data:\n  journal_dir: {tmp_path}\n  cache_dir: {tmp_path / 'cache'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("fbe.cli._now", lambda: TODAY)
    result = runner.invoke(app, ["--config", str(config), "journal", "review", *args])
    return result.exit_code, result.output


@pytest.fixture
def week(tmp_path: Path) -> Path:
    return journal_of(tmp_path, OLD, *WEEK)


# --- the statistics live in the journal module ----------------------------------------


def test_the_plan_split_is_computed_by_the_journal() -> None:
    split = evaluate_by_plan(WEEK)

    assert split[True].trades == 3
    assert split[True].total_zar == pytest.approx(50.00)
    assert split[True].expectancy_r == pytest.approx(2.5 / 3)
    assert split[False].trades == 1
    assert split[False].total_zar == pytest.approx(-20.00)


def test_the_bias_split_is_computed_by_the_journal() -> None:
    split = evaluate_by_bias_agreement(WEEK)

    assert split[True].trades == 3
    assert split[True].total_zar == pytest.approx(-10.00)
    assert split[False].trades == 1
    assert split[False].expectancy_r == pytest.approx(2.0)


def test_the_summary_covers_every_closed_trade_and_no_open_one() -> None:
    whole = summarise(WEEK)

    assert whole is not None
    assert whole.trades == 4
    assert whole.wins == 2
    assert whole.total_zar == pytest.approx(30.00)
    assert whole.expectancy_r == pytest.approx(0.375)


def test_an_empty_window_has_no_summary_rather_than_zeros() -> None:
    assert summarise((WEEK[-1],)) is None


def test_one_unconverted_trade_makes_the_money_total_unknown() -> None:
    """A partial sum would read as the bucket's total."""
    unconverted = closed_trade("TX", "EURJPY", 5, 1.0, None)

    whole = summarise((*WEEK, unconverted))

    assert whole is not None and whole.total_zar is None


def test_the_command_computes_no_statistic_of_its_own() -> None:
    """Criterion 1. The review functions sum, average and count nothing."""
    names = {
        name
        for name in dir(cli)
        if name == "journal_review" or name.startswith("_review")
    }
    assert "journal_review" in names
    for name in names:
        tree = ast.parse(inspect.getsource(getattr(cli, name)).lstrip())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not called & {"sum", "mean", "fmean", "median", "statistics"}, name


# --- the table ------------------------------------------------------------------------


def test_both_splits_are_printed_with_their_counts(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = run(tmp_path, monkeypatch)

    assert code == 0, out
    assert re.search(r"Followed the plan\s+3 trades\s+ZAR \+50\.00\s+avg \+0\.83R", out)
    assert re.search(r"Broke the plan\s+1 trade\s+ZAR -20\.00\s+avg -1\.00R", out)
    assert re.search(r"With engine bias\s+3 trades\s+ZAR -10\.00\s+avg -0\.17R", out)
    assert re.search(r"Against the bias\s+1 trade\s+ZAR \+40\.00\s+avg \+2\.00R", out)


def test_the_plain_numbers_come_first(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = run(tmp_path, monkeypatch)

    assert "7 days to 2026-10-06: 5 trades, 4 closed, 1 open" in out
    assert re.search(r"Closed P&L\s+ZAR \+30\.00", out)
    assert "Win rate 50%" in out
    assert "+0.38R" in out


def test_every_bucket_here_is_marked_a_record_not_evidence(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every bucket is under 30 closed trades, so every row says so, and the
    win rate carries its interval."""
    _, out = run(tmp_path, monkeypatch)

    rows = [line for line in out.splitlines() if "avg " in line or "Win rate" in line]
    assert rows and all("record" in line for line in rows)
    assert "not evidence" in out
    assert re.search(r"Win rate 50% \(\d+% to \d+%\)", out)


def test_conviction_bands_are_listed_with_their_samples(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = run(tmp_path, monkeypatch)

    assert re.search(r"high\s+1 trade\b", out)
    assert re.search(r"medium\s+2 trades\b", out)
    assert re.search(r"low\s+1 trade\b", out)


def test_open_trades_are_listed(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = run(tmp_path, monkeypatch)

    assert "Open: EURGBP long from 1.0850, stop 1.0825, opened 2026-10-05" in out


def test_an_unknown_money_total_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_of(tmp_path, *WEEK, closed_trade("TX", "EURJPY", 5, 1.0, None))

    _, out = run(tmp_path, monkeypatch)

    assert re.search(r"Closed P&L\s+ZAR unknown", out)


def test_nothing_printed_claims_an_edge(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = run(tmp_path, monkeypatch)

    for word in ("edge", "proven", "backtest", "guaranteed", "outperform"):
        assert word not in out.lower(), word


# --- empty ----------------------------------------------------------------------------


def test_an_empty_journal_says_so_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal state of a fresh clone: no file at all."""
    code, out = run(tmp_path, monkeypatch)

    assert code == 0, out
    assert "no trades" in out.lower()
    assert "%" not in out and "ZAR" not in out


def test_a_window_with_no_trades_says_so_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_of(tmp_path, OLD)

    code, out = run(tmp_path, monkeypatch)

    assert code == 0, out
    assert "No trades opened in the 7 days to 2026-10-06" in out


# --- filters --------------------------------------------------------------------------


def test_days_widens_the_window(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = run(tmp_path, monkeypatch, "--days", "30")

    assert "30 days to 2026-10-06: 6 trades, 5 closed, 1 open" in out


def test_pair_filters_and_repeats(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, one = run(tmp_path, monkeypatch, "--pair", "EURUSD")
    _, two = run(tmp_path, monkeypatch, "-p", "eurusd", "-p", "GBPUSD")

    assert "1 trade, 1 closed, 0 open" in one
    assert "2 trades, 2 closed, 0 open" in two


def test_tag_filters(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = run(tmp_path, monkeypatch, "--tag", "channel")

    assert "2 trades, 2 closed, 0 open" in out


def test_open_only_lists_open_trades_and_no_figures(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = run(tmp_path, monkeypatch, "--open-only")

    assert "1 trade, 0 closed, 1 open" in out
    assert "Open: EURGBP" in out
    assert "Followed the plan" not in out


# --- the machine formats carry the table's numbers ------------------------------------


def test_json_carries_the_same_numbers(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = run(tmp_path, monkeypatch, "--format", "json")
    payload = json.loads(out)

    assert code == 0
    assert payload["window"] == {"days": 7, "to": "2026-10-06"}
    assert (payload["trades"], payload["closed"], payload["open"]) == (5, 4, 1)
    assert payload["summary"]["total_zar"] == pytest.approx(30.00)
    assert payload["by_plan"]["followed"]["trades"] == 3
    assert payload["by_plan"]["broke"]["total_zar"] == pytest.approx(-20.00)
    assert payload["by_bias"]["against"]["expectancy_r"] == pytest.approx(2.0)
    assert payload["by_conviction"]["medium"]["trades"] == 2
    assert payload["summary"]["below_evidence_threshold"] is True
    assert [trade["trade_id"] for trade in payload["open_trades"]] == ["T5"]


def test_csv_carries_the_same_numbers(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = run(tmp_path, monkeypatch, "--format", "csv")
    rows = {(row["group"], row["key"]): row for row in csv.DictReader(io.StringIO(out))}

    assert float(rows[("summary", "all")]["total_zar"]) == pytest.approx(30.00)
    assert rows[("plan", "followed")]["trades"] == "3"
    assert rows[("bias", "with")]["trades"] == "3"
    assert float(rows[("bias", "against")]["expectancy_r"]) == pytest.approx(2.0)
    assert rows[("conviction", "low")]["trades"] == "1"


# --- the published example is a fixture -----------------------------------------------


def test_the_published_example_reproduces(
    week: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``docs/interfaces.md``'s example is this journal's output, line for line."""
    text = (REPO / "docs" / "interfaces.md").read_text(encoding="utf-8")
    start = text.index("### `fbe journal review`")
    block = text.index("```console\n$ fbe journal review --days 7\n", start)
    body = text[block + len("```console\n$ fbe journal review --days 7\n") :]
    published = body[: body.index("```")].rstrip("\n").split("\n")

    _, out = run(tmp_path, monkeypatch, "--days", "7")

    assert out.rstrip("\n").split("\n") == published
