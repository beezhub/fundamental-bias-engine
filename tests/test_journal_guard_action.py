"""The journal records what the calendar guard said about an open position.

Issue #238, ruled 2026-09-23 and 2026-09-29. `TIGHTEN_BUFFER_R` is a threshold
with no measurement behind it, and its stated calibration is to compare what
holding through a release window cost against what flattening for it cost. That
needs every closed trade to say which instruction the guard last gave, or that
no guard was consulted, and a grouping that keeps all of those apart.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fbe import calendar_guard, journal
from fbe.calendar_guard import OpenPositionAction
from fbe.journal import (
    ConvictionStats,
    TradeRecord,
    append,
    evaluate,
    evaluate_by_guard_action,
    load,
)
from fbe.types import Conviction
from tests.test_journal import record

REPO = Path(__file__).resolve().parents[1]
CLOSED = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)


def closed(
    trade_id: str,
    r_multiple: float,
    guard_action: OpenPositionAction | None,
    conviction: Conviction = Conviction.HIGH,
) -> TradeRecord:
    return record(
        trade_id,
        conviction=conviction,
        closed_at=CLOSED,
        exit_price=1.0900,
        r_multiple=r_multiple,
        exit_reason="stop" if r_multiple < 0 else "target",
        guard_action=guard_action,
    )


def test_the_default_is_no_guard_consulted() -> None:
    """``None`` is an explicit absence, never a reading of HOLD."""
    assert record().guard_action is None


def test_none_is_documented_as_not_consulted_rather_than_hold() -> None:
    source = " ".join(inspect.getsource(TradeRecord).split())
    doc = source.split("guard_action:", 1)[1][:700]

    assert "no guard was consulted" in doc
    assert "not" in doc and "hold" in doc.lower()


def test_three_kinds_of_trade_round_trip_through_the_file(tmp_path: Path) -> None:
    """Flattened for a window, held through one, and one the guard never saw."""
    path = tmp_path / "trades.jsonl"
    for trade in (
        closed("FLAT", -0.2, OpenPositionAction.FLATTEN),
        closed("HELD", 1.4, OpenPositionAction.HOLD),
        closed("PLAIN", -1.0, None),
    ):
        append(trade, path)

    actions = {trade.trade_id: trade.guard_action for trade in load(path=path)}

    assert actions == {
        "FLAT": OpenPositionAction.FLATTEN,
        "HELD": OpenPositionAction.HOLD,
        "PLAIN": None,
    }
    assert isinstance(actions["FLAT"], OpenPositionAction)


def test_a_line_written_before_the_field_existed_reads_as_not_consulted(
    tmp_path: Path,
) -> None:
    """Records already in a journal must keep loading, and must not claim a HOLD."""
    path = tmp_path / "trades.jsonl"
    append(closed("OLD", 1.0, None), path)
    line = json.loads(path.read_text(encoding="utf-8"))
    del line["guard_action"]
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")

    (only,) = load(path=path)

    assert only.guard_action is None


def test_a_guard_action_that_is_not_the_enum_is_refused_at_write(
    tmp_path: Path,
) -> None:
    """A bare string would write a line `load` then refuses, taking the file with it."""
    with pytest.raises(ValueError, match="guard_action"):
        append(closed("BAD", 1.0, "flatten"), tmp_path / "trades.jsonl")  # type: ignore[arg-type]


def test_the_grouping_keeps_flattened_held_and_unguarded_apart(
    tmp_path: Path,
) -> None:
    """The comparison behind `TIGHTEN_BUFFER_R`, run end to end from the file.

    ``None`` is a key, not a filter: the trades no guard saw are the control
    group the comparison needs.
    """
    path = tmp_path / "trades.jsonl"
    for trade in (
        closed("F1", -0.2, OpenPositionAction.FLATTEN),
        closed("F2", 0.4, OpenPositionAction.FLATTEN),
        closed("H1", 1.4, OpenPositionAction.HOLD),
        closed("T1", 0.9, OpenPositionAction.TIGHTEN),
        closed("P1", -1.0, None),
        closed("P2", 2.0, None),
    ):
        append(trade, path)

    groups = evaluate_by_guard_action(load(path=path))

    assert set(groups) == {
        OpenPositionAction.FLATTEN,
        OpenPositionAction.HOLD,
        OpenPositionAction.TIGHTEN,
        None,
    }
    assert groups[OpenPositionAction.FLATTEN].trades == 2
    assert groups[OpenPositionAction.FLATTEN].expectancy_r == pytest.approx(0.1)
    assert groups[OpenPositionAction.HOLD].total_r == pytest.approx(1.4)
    assert groups[None].trades == 2
    assert groups[None].expectancy_r == pytest.approx(0.5)


def test_the_grouping_ignores_open_trades_and_mixes_convictions() -> None:
    """Same exclusions as `evaluate`. A guard group spans conviction levels,
    which is why the statistics carry no conviction label."""
    still_open = record("OPEN", guard_action=OpenPositionAction.FLATTEN)
    groups = evaluate_by_guard_action(
        (
            still_open,
            closed("A", 1.0, OpenPositionAction.FLATTEN, Conviction.LOW),
            closed("B", -1.0, OpenPositionAction.FLATTEN, Conviction.HIGH),
        )
    )

    assert groups[OpenPositionAction.FLATTEN].trades == 2


def test_the_conviction_grouping_is_unchanged() -> None:
    trades = (
        closed("A", 1.0, None, Conviction.LOW),
        closed("B", -1.0, OpenPositionAction.FLATTEN, Conviction.HIGH),
    )

    assert set(evaluate(trades)) == {Conviction.LOW, Conviction.HIGH}


def test_the_statistics_carry_no_key_of_their_own() -> None:
    """The key is the mapping's. A field repeating it would be false for every
    guard group, which mixes conviction levels."""
    assert "conviction" not in {field.name for field in fields(ConvictionStats)}


def test_exit_reason_still_has_exactly_six_values_and_says_six() -> None:
    """The guard's instruction is a circumstance, not a seventh exit."""
    doc = " ".join(inspect.getsource(TradeRecord).split())
    spec = " ".join(
        (REPO / "docs" / "risk-and-execution.md").read_text(encoding="utf-8").split()
    )

    assert "six exit strategies" in doc
    assert "five exit" not in doc
    assert "the plan's six exits" in spec
    assert "five exits" not in spec


def test_the_guard_no_longer_says_the_journal_cannot_hold_the_reason() -> None:
    source = inspect.getsource(calendar_guard.action_for_open_position)

    assert "has no value for a news flatten" not in source
    assert "guard_action" in source


def test_the_spec_says_the_measurement_compares_held_against_flattened() -> None:
    spec = " ".join(
        (REPO / "docs" / "risk-and-execution.md").read_text(encoding="utf-8").split()
    )

    assert "guard_action" in spec
    assert "evaluate_by_guard_action" in spec


def test_the_enum_is_imported_from_the_guard_not_mirrored() -> None:
    """One definition of three member names, not two that can drift."""
    assert journal.OpenPositionAction is calendar_guard.OpenPositionAction
