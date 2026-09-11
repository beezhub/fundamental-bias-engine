"""Tests for the pair layer.

Only `shortlist` is implemented so far, so only `shortlist` is tested. The
point of the first two tests is the wire, not the value: the shortlist length
must follow ``RiskConfig.max_concurrent_positions`` when that number is moved
away from its default, which is the only way to prove the cap is read rather
than hardcoded a second time.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

import pytest

from fbe.bias import shortlist
from fbe.config import Config, RiskConfig
from fbe.types import Conviction, Direction, PairBias
from fbe.universe import split_pair


def _bias(
    pair: str,
    spread: float,
    conviction: Conviction,
    tradeable: bool = True,
    asof: date = date(2026, 6, 30),
) -> PairBias:
    """A tradeable bias with direction derived from the sign of the spread."""
    base, quote = split_pair(pair)
    if conviction is Conviction.NONE:
        direction = Direction.NEUTRAL
    else:
        direction = Direction.LONG if spread > 0 else Direction.SHORT
    return PairBias(
        pair=pair,
        base=base,
        quote=quote,
        spread=spread,
        direction=direction,
        conviction=conviction,
        asof=asof,
        tradeable=tradeable,
    )


def _config_with_cap(default_config: Config, cap: int) -> Config:
    return replace(
        default_config, risk=replace(default_config.risk, max_concurrent_positions=cap)
    )


@pytest.fixture
def five_candidates() -> Sequence[PairBias]:
    """Five tradeable biases, best first.

    The first four share no currency between them, so the leg rule cannot be
    what stops the shortlist short of four. The fifth has to share a leg: eight
    currencies cannot make five disjoint pairs, so it is ranked last and stands
    in for the rest of a real run.
    """
    return (
        _bias("EURUSD", 2.6, Conviction.HIGH),
        _bias("GBPJPY", 1.9, Conviction.MEDIUM),
        _bias("AUDCAD", -1.6, Conviction.MEDIUM),
        _bias("NZDCHF", 1.1, Conviction.LOW),
        _bias("EURGBP", 0.9, Conviction.LOW),
    )


def test_limit_has_no_numeric_default() -> None:
    parameter = inspect.signature(shortlist).parameters["limit"]
    assert parameter.default is inspect.Parameter.empty


def test_cap_of_two_yields_two(
    default_config: Config, five_candidates: Sequence[PairBias]
) -> None:
    config = _config_with_cap(default_config, 2)
    assert default_config.risk.max_concurrent_positions != 2
    picked = shortlist(five_candidates, config.risk.max_concurrent_positions)
    assert [b.pair for b in picked] == ["EURUSD", "GBPJPY"]


def test_cap_of_four_yields_four(
    default_config: Config, five_candidates: Sequence[PairBias]
) -> None:
    config = _config_with_cap(default_config, 4)
    assert default_config.risk.max_concurrent_positions != 4
    picked = shortlist(five_candidates, config.risk.max_concurrent_positions)
    assert [b.pair for b in picked] == ["EURUSD", "GBPJPY", "AUDCAD", "NZDCHF"]


def test_default_cap_reaches_the_function_unchanged(
    default_config: Config, five_candidates: Sequence[PairBias]
) -> None:
    cap = default_config.risk.max_concurrent_positions
    assert cap == RiskConfig().max_concurrent_positions
    assert len(shortlist(five_candidates, cap)) == cap


def test_untradeable_and_none_conviction_are_dropped() -> None:
    biases = (
        _bias("EURUSD", 2.8, Conviction.HIGH, tradeable=False),
        _bias("GBPJPY", 0.3, Conviction.NONE),
        _bias("AUDCAD", 1.0, Conviction.LOW),
    )
    assert [b.pair for b in shortlist(biases, 3)] == ["AUDCAD"]


def test_conviction_outranks_spread_size() -> None:
    biases = (
        _bias("AUDCAD", -2.4, Conviction.LOW),
        _bias("EURUSD", 0.8, Conviction.MEDIUM),
    )
    assert [b.pair for b in shortlist(biases, 2)] == ["EURUSD", "AUDCAD"]


def test_absolute_spread_orders_within_a_conviction_level() -> None:
    biases = (
        _bias("EURUSD", 1.6, Conviction.MEDIUM),
        _bias("AUDCAD", -2.2, Conviction.MEDIUM),
    )
    assert [b.pair for b in shortlist(biases, 2)] == ["AUDCAD", "EURUSD"]


def test_ties_break_on_pair_name() -> None:
    biases = (
        _bias("GBPJPY", 1.7, Conviction.MEDIUM),
        _bias("AUDCAD", -1.7, Conviction.MEDIUM),
    )
    assert [b.pair for b in shortlist(biases, 2)] == ["AUDCAD", "GBPJPY"]


def test_a_shared_leg_in_either_position_is_skipped() -> None:
    """Long AUD/USD and short USD/JPY is one dollar position on two tickets."""
    biases = (
        _bias("AUDUSD", 2.7, Conviction.HIGH),
        _bias("USDJPY", -2.0, Conviction.MEDIUM),
        _bias("EURAUD", -1.8, Conviction.MEDIUM),
        _bias("GBPCAD", 1.0, Conviction.LOW),
    )
    assert [b.pair for b in shortlist(biases, 3)] == ["AUDUSD", "GBPCAD"]


def test_fewer_candidates_than_cap_returns_all_of_them() -> None:
    biases = (_bias("EURUSD", 1.0, Conviction.LOW),)
    assert [b.pair for b in shortlist(biases, 4)] == ["EURUSD"]


def test_empty_input_returns_empty() -> None:
    assert shortlist((), 3) == ()


def test_cap_of_zero_returns_empty(five_candidates: Sequence[PairBias]) -> None:
    assert shortlist(five_candidates, 0) == ()


def test_negative_cap_raises(five_candidates: Sequence[PairBias]) -> None:
    with pytest.raises(ValueError, match="limit"):
        shortlist(five_candidates, -1)
