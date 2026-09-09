"""Tests for the traded universe.

The pair grid is load-bearing. If ``ALL_PAIRS`` ever contains ``USDEUR``
instead of ``EURUSD``, every bias on that pair is silently inverted and the
report reads backwards without erroring. These tests exist to make that
impossible to ship.
"""

from __future__ import annotations

import pytest

from fbe.universe import (
    ALL_PAIRS,
    CURRENCIES,
    G10,
    MAJORS,
    CurrencyMeta,
    meta,
    pair_name,
    split_pair,
)

EXPECTED_PAIR_COUNT = 28
"""8 currencies choose 2."""

CONVENTION: tuple[tuple[str, str], ...] = (
    ("EURUSD", "USDEUR"),
    ("GBPUSD", "USDGBP"),
    ("USDJPY", "JPYUSD"),
    ("USDCHF", "CHFUSD"),
    ("USDCAD", "CADUSD"),
    ("AUDUSD", "USDAUD"),
    ("NZDUSD", "USDNZD"),
    ("EURGBP", "GBPEUR"),
    ("AUDNZD", "NZDAUD"),
)
"""(correct, inverted) pairs. The market quotes the first of each."""


def test_universe_is_the_eight_g10_currencies() -> None:
    assert len(G10) == 8
    assert len(set(G10)) == 8
    assert all(len(code) == 3 and code.isupper() for code in G10)


def test_there_are_exactly_28_pairs() -> None:
    assert len(ALL_PAIRS) == EXPECTED_PAIR_COUNT


def test_every_pair_is_six_characters() -> None:
    assert all(len(pair) == 6 for pair in ALL_PAIRS)


def test_no_duplicate_pairs() -> None:
    assert len(set(ALL_PAIRS)) == len(ALL_PAIRS)


def test_no_self_pairs() -> None:
    for pair in ALL_PAIRS:
        base, quote = split_pair(pair)
        assert base != quote, f"{pair} pairs a currency with itself"


def test_no_pair_appears_in_both_directions() -> None:
    seen = {frozenset(split_pair(pair)) for pair in ALL_PAIRS}
    assert len(seen) == len(ALL_PAIRS)


def test_both_legs_of_every_pair_are_in_g10() -> None:
    for pair in ALL_PAIRS:
        base, quote = split_pair(pair)
        assert base in G10, f"{pair} has a base outside G10"
        assert quote in G10, f"{pair} has a quote outside G10"


def test_every_g10_currency_appears_in_seven_pairs() -> None:
    for code in G10:
        appearances = [p for p in ALL_PAIRS if code in split_pair(p)]
        assert len(appearances) == 7, f"{code} appears in {len(appearances)} pairs"


@pytest.mark.parametrize(("correct", "inverted"), CONVENTION)
def test_quoting_convention(correct: str, inverted: str) -> None:
    assert correct in ALL_PAIRS, f"expected {correct} in ALL_PAIRS"
    assert inverted not in ALL_PAIRS, f"{inverted} inverts the market convention"


def test_pair_name_joins_base_and_quote() -> None:
    assert pair_name("EUR", "USD") == "EURUSD"
    assert pair_name("USD", "JPY") == "USDJPY"


def test_split_pair_round_trips_with_pair_name() -> None:
    for pair in ALL_PAIRS:
        base, quote = split_pair(pair)
        assert pair_name(base, quote) == pair


def test_split_pair_rejects_a_wrong_length_string() -> None:
    with pytest.raises(ValueError, match="six-character"):
        split_pair("EURUS")


def test_every_major_is_in_all_pairs() -> None:
    for pair in MAJORS:
        assert pair in ALL_PAIRS, f"{pair} is a major but missing from ALL_PAIRS"


def test_every_major_has_a_usd_leg() -> None:
    for pair in MAJORS:
        assert "USD" in split_pair(pair), f"{pair} is listed as a major without USD"


def test_majors_has_no_duplicates() -> None:
    assert len(set(MAJORS)) == len(MAJORS)


def test_every_g10_code_has_currency_metadata() -> None:
    for code in G10:
        assert code in CURRENCIES, f"{code} has no CurrencyMeta"
        assert isinstance(CURRENCIES[code], CurrencyMeta)


def test_currencies_maps_no_codes_outside_g10() -> None:
    assert set(CURRENCIES) == set(G10)


def test_currency_metadata_is_self_consistent() -> None:
    for code, entry in CURRENCIES.items():
        assert entry.code == code, f"{code} keyed against meta for {entry.code}"
        assert entry.name
        assert entry.central_bank
        assert 0.0 < entry.inflation_target < 10.0
        assert -1.0 <= entry.risk_beta <= 1.0


def test_meta_looks_up_a_currency() -> None:
    assert meta("USD").central_bank == "Federal Reserve"


def test_meta_is_case_insensitive() -> None:
    assert meta("usd") is meta("USD")


def test_meta_raises_a_clear_error_for_an_unknown_code() -> None:
    with pytest.raises(KeyError) as excinfo:
        meta("ZAR")
    message = str(excinfo.value)
    assert "ZAR" in message
    assert "outside the scored universe" in message
