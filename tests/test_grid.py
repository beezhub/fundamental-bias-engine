"""Tests for `fbe.report._grid`.

A run holds 28 pairs in market convention and the grid has 56 populated cells,
so half of them are mirrors and `_grid` is the single place they are made. The
defect worth guarding is a mirrored cell left in convention: a plausible number
under the wrong row header, in the half of the grid nobody double-checks. Every
assertion here is cell by cell against the convention entry it mirrors, and the
fixtures are built so that a mirror that forgets any one field fails.

The fixture deliberately breaks the arithmetic a real run honours. Direction
does not follow the sign of the spread and conviction does not follow its
width, so a grid that re-derives either from the spread, rather than inverting
the one and carrying the other, fails here and would pass against a
self-consistent run.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pytest

from fbe.report import _grid
from fbe.types import Conviction, Direction, PairBias
from fbe.universe import ALL_PAIRS, G10, split_pair

ASOF = date(2026, 9, 9)

INVERSE = {
    Direction.LONG: Direction.SHORT,
    Direction.SHORT: Direction.LONG,
    Direction.NEUTRAL: Direction.NEUTRAL,
}


def pair_bias(
    pair: str,
    spread: float,
    *,
    direction: Direction,
    conviction: Conviction,
    blockers: Sequence[str] = (),
    tradeable: bool = True,
) -> PairBias:
    """One convention row with legs, scores and agreement all distinct.

    ``base_score`` and ``quote_score`` are chosen so their difference is not
    the spread. A mirror that recomputes the spread from the swapped scores
    rather than negating the field would then disagree with the negation, and
    the test that checks the spread catches it.
    """
    base, quote = split_pair(pair)
    return PairBias(
        pair=pair,
        base=base,
        quote=quote,
        spread=spread,
        direction=direction,
        conviction=conviction,
        asof=ASOF,
        base_score=spread * 0.75 + 0.11,
        quote_score=-spread * 0.2 + 0.07,
        agreement=0.5 + abs(spread) / 10,
        tradeable=tradeable,
        blockers=tuple(blockers),
    )


def full_run() -> tuple[PairBias, ...]:
    """All 28 pairs, every direction and conviction represented, none derived.

    The direction cycles independently of the sign and the conviction cycles
    independently of the width, so no field can be reconstructed from another.
    """
    directions = (Direction.LONG, Direction.SHORT, Direction.NEUTRAL)
    convictions = (Conviction.HIGH, Conviction.MEDIUM, Conviction.LOW, Conviction.NONE)
    rows = []
    for index, pair in enumerate(ALL_PAIRS):
        spread = ((index * 7) % 11 - 5) * 0.37
        rows.append(
            pair_bias(
                pair,
                spread,
                direction=directions[index % 3],
                conviction=convictions[index % 4],
                blockers=("event:unchecked",) if index % 5 == 0 else (),
                tradeable=index % 5 != 0,
            )
        )
    return tuple(rows)


def test_the_axes_are_the_universe_in_its_own_order() -> None:
    grid = _grid(full_run())

    assert tuple(grid) == G10
    for base in G10:
        assert tuple(grid[base]) == G10


def test_a_convention_cell_is_the_row_it_was_given() -> None:
    """The upper half is passed through, not rebuilt."""
    run = full_run()
    grid = _grid(run)

    for row in run:
        assert grid[row.base][row.quote] == row


def test_a_mirrored_cell_is_the_convention_row_read_the_other_way() -> None:
    """Every field, cell by cell. Spread negated, legs and scores swapped,
    direction inverted, pair rewritten for the cell it sits in."""
    run = full_run()
    grid = _grid(run)

    for row in run:
        mirror = grid[row.quote][row.base]
        assert mirror is not None
        assert mirror.spread == -row.spread
        assert mirror.base == row.quote
        assert mirror.quote == row.base
        assert mirror.base_score == row.quote_score
        assert mirror.quote_score == row.base_score
        assert mirror.pair == row.quote + row.base
        assert mirror.direction is INVERSE[row.direction]


def test_a_mirrored_cell_keeps_the_conviction() -> None:
    """Conviction grades how much the model believes the spread, and the belief
    does not change when the pair is written the other way round."""
    run = full_run()
    grid = _grid(run)

    for row in run:
        mirror = grid[row.quote][row.base]
        assert mirror is not None
        assert mirror.conviction is row.conviction


def test_a_mirrored_cell_keeps_everything_that_has_no_side() -> None:
    """Agreement, the run date, tradeability and the blockers do not depend on
    which way round the pair is quoted."""
    run = full_run()
    grid = _grid(run)

    for row in run:
        mirror = grid[row.quote][row.base]
        assert mirror is not None
        assert mirror.agreement == row.agreement
        assert mirror.asof == row.asof
        assert mirror.tradeable is row.tradeable
        assert tuple(mirror.blockers) == tuple(row.blockers)


@pytest.mark.parametrize("pair", ALL_PAIRS)
def test_every_pair_and_its_mirror_are_opposites(pair: str) -> None:
    """The property over all 28 pairs: the spreads sum to zero and the
    directions are opposites unless both are neutral."""
    grid = _grid(full_run())
    base, quote = split_pair(pair)
    cell = grid[base][quote]
    mirror = grid[quote][base]

    assert cell is not None and mirror is not None
    assert cell.spread == -mirror.spread
    if cell.direction is Direction.NEUTRAL:
        assert mirror.direction is Direction.NEUTRAL
    else:
        assert cell.direction is not mirror.direction
        assert Direction.NEUTRAL not in (cell.direction, mirror.direction)


def test_a_neutral_pair_mirrors_as_neutral_with_the_spread_still_negated() -> None:
    """Neutral has no side to invert. The spread is still a number with a
    sign, and it still flips."""
    row = pair_bias(
        "EURUSD", 0.42, direction=Direction.NEUTRAL, conviction=Conviction.NONE
    )
    grid = _grid((row,))
    mirror = grid["USD"]["EUR"]

    assert mirror is not None
    assert mirror.direction is Direction.NEUTRAL
    assert mirror.spread == -0.42


def test_the_diagonal_is_none_and_never_a_zero_spread() -> None:
    """A currency has no bias against itself."""
    grid = _grid(full_run())

    for currency in G10:
        assert grid[currency][currency] is None


def test_a_full_run_populates_fifty_six_cells() -> None:
    grid = _grid(full_run())

    populated = sum(1 for base in G10 for quote in G10 if grid[base][quote] is not None)
    assert populated == 56


def test_a_missing_pair_leaves_both_its_cells_empty() -> None:
    """Neither half is filled for the other. A cell whose partner is absent
    would be a number the run never produced."""
    run = tuple(row for row in full_run() if row.pair != "EURGBP")
    grid = _grid(run)

    assert grid["EUR"]["GBP"] is None
    assert grid["GBP"]["EUR"] is None
    populated = sum(1 for base in G10 for quote in G10 if grid[base][quote] is not None)
    assert populated == 54


def test_an_empty_run_is_an_empty_grid_with_the_axes_intact() -> None:
    grid = _grid(())

    assert tuple(grid) == G10
    assert all(grid[base][quote] is None for base in G10 for quote in G10)


def test_a_pair_given_both_ways_round_is_refused() -> None:
    """Two rows for one cell is a run that contradicts itself, and picking one
    silently would print whichever came last."""
    rows = (
        pair_bias("EURUSD", 1.1, direction=Direction.LONG, conviction=Conviction.HIGH),
        pair_bias("USDEUR", 0.3, direction=Direction.LONG, conviction=Conviction.LOW),
    )

    with pytest.raises(ValueError, match="EURUSD"):
        _grid(rows)


def test_a_currency_outside_the_universe_is_refused() -> None:
    """The axes are `G10`, so a leg outside it has no cell. Dropping it would
    lose a pair without a word."""
    row = pair_bias("SEKUSD", 0.9, direction=Direction.LONG, conviction=Conviction.LOW)

    with pytest.raises(ValueError, match="SEK"):
        _grid((row,))


def test_the_mirror_is_derived_from_the_row_and_not_from_the_sign() -> None:
    """A row whose direction contradicts its sign: the mirror inverts the
    direction the engine recorded, not the one the sign would suggest."""
    row = pair_bias(
        "GBPUSD", -1.3, direction=Direction.LONG, conviction=Conviction.MEDIUM
    )
    grid = _grid((row,))
    mirror = grid["USD"]["GBP"]

    assert mirror is not None
    assert mirror.direction is Direction.SHORT
    assert mirror.spread == 1.3
    assert mirror.conviction is Conviction.MEDIUM
