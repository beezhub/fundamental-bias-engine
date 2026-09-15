"""Tests for the pair layer: differencing two currency scores into a bias.

The defect this module has to be protected from is the first row of the prime
directive table, and it is the worst one available here: a pair built the wrong
way round produces a confident, backwards call with nothing in the output
saying so. So the reversal case is tested directly rather than inferred from
the arithmetic, and the pair list is compared against `universe.ALL_PAIRS`
rather than against a list retyped in this file.

The second theme is that every input to `conviction_for` other than the spread
is a reason to doubt the spread. None of them can raise conviction, they
compound, and the order they apply in is load-bearing: a cap applied before two
one-step demotions lands two tiers below the same cap applied after them.

Nothing here reaches the network. Every score is built in the test that uses it.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import pytest

from fbe.bias import (
    agreement,
    build_pair_biases,
    conviction_for,
    direction_for,
)
from fbe.config import Config, ScoringConfig
from fbe.types import Conviction, CurrencyScore, Direction, PillarName, PillarScore
from fbe.universe import ALL_PAIRS, G10, split_pair

ASOF = date(2026, 9, 15)

# Ordered weakest to strongest so a cap and a demotion can be told apart by
# which way they move a tier.
LADDER = (Conviction.NONE, Conviction.LOW, Conviction.MEDIUM, Conviction.HIGH)


def pillar(name: PillarName, currency: str, score: float, weight: float) -> PillarScore:
    return PillarScore(
        pillar=name,
        currency=currency,
        raw=score,
        z=score,
        score=score,
        weight=weight,
        asof=ASOF,
    )


def leg(
    currency: str,
    composite: float,
    *,
    coverage: float = 1.0,
    dispersion: float = 0.0,
    pillars: Mapping[PillarName, float] | None = None,
    weights: Mapping[PillarName, float] | None = None,
) -> CurrencyScore:
    """Build one currency's score.

    Args:
        currency: ISO code.
        composite: The headline, which is what `spread` differences. Set
            independently of the pillar scores on purpose: several tests need a
            spread whose sign is known without having to solve for pillars that
            produce it.
        coverage: Fraction of pillar weight with usable data.
        dispersion: Standard deviation across the pillar scores.
        pillars: Per-pillar score. Defaults to every pillar at the composite,
            which is the uncontroversial case: all seven agree.
        weights: Per-pillar effective weight, already carrying the staleness
            penalty as `agreement` requires. Defaults to `ScoringConfig`'s.

    """
    configured = ScoringConfig().weights
    scores = (
        {name: composite for name in PillarName} if pillars is None else dict(pillars)
    )
    effective = dict(configured) if weights is None else dict(weights)
    return CurrencyScore(
        currency=currency,
        composite=composite,
        pillars={
            name: pillar(name, currency, value, effective.get(name, 0.0))
            for name, value in scores.items()
        },
        asof=ASOF,
        dispersion=dispersion,
        coverage=coverage,
    )


def universe(**composites: float) -> list[CurrencyScore]:
    """One score per G10 currency, defaulting to zero for any not named."""
    return [leg(c, composites.get(c, 0.0)) for c in G10]


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def scoring() -> ScoringConfig:
    return ScoringConfig()


# --- the spread --------------------------------------------------------------


def test_the_spread_is_the_base_composite_less_the_quote(config: Config) -> None:
    """Across the whole universe at once, so no pair is differenced the other
    way round by an indexing slip that a single-pair test would miss."""
    composites = {c: value for value, c in enumerate(G10, start=1)}
    scores = [leg(c, float(v)) for c, v in composites.items()]

    biases = build_pair_biases(scores, config, ASOF)

    for bias in biases:
        base, quote = split_pair(bias.pair)
        assert bias.spread == pytest.approx(composites[base] - composites[quote]), (
            bias.pair
        )


def test_reversing_a_pair_negates_the_spread_and_inverts_the_direction(
    config: Config, scoring: ScoringConfig
) -> None:
    """The most dangerous defect this codebase can ship, tested head on rather
    than inferred from the subtraction.

    `ALL_PAIRS` holds each pair one way round only, so the reversed pair is
    built by swapping which leg carries which composite and asserting the
    output mirrors.
    """
    forward = build_pair_biases(universe(EUR=1.6, USD=0.1), config, ASOF)
    reversed_run = build_pair_biases(universe(EUR=0.1, USD=1.6), config, ASOF)
    ahead = next(b for b in forward if b.pair == "EURUSD")
    behind = next(b for b in reversed_run if b.pair == "EURUSD")

    assert ahead.spread == pytest.approx(-behind.spread)
    assert ahead.direction is Direction.LONG
    assert behind.direction is Direction.SHORT


def test_the_legs_are_carried_onto_the_bias(config: Config) -> None:
    """So a reader can check the subtraction without the currency scores."""
    biases = build_pair_biases(universe(EUR=1.6, USD=0.1), config, ASOF)
    eurusd = next(b for b in biases if b.pair == "EURUSD")

    assert eurusd.base_score == pytest.approx(1.6)
    assert eurusd.quote_score == pytest.approx(0.1)
    assert eurusd.base == "EUR"
    assert eurusd.quote == "USD"


# --- the pair list -----------------------------------------------------------


def test_every_pair_in_the_universe_comes_back_in_order(config: Config) -> None:
    """Compared against the universe rather than a list retyped here, so a pair
    added to `ALL_PAIRS` is covered without editing this test, and the ordering
    convention stays the universe's."""
    biases = build_pair_biases(universe(), config, ASOF)

    assert [b.pair for b in biases] == list(ALL_PAIRS)


def test_the_pair_count_is_the_twenty_eight(config: Config) -> None:
    """Guards the test above, which would pass vacuously against an empty
    `ALL_PAIRS`."""
    assert len(ALL_PAIRS) == 28
    assert len(build_pair_biases(universe(), config, ASOF)) == 28


@pytest.mark.parametrize("absent", ["EUR", "JPY"])
def test_a_currency_missing_from_the_scores_raises(config: Config, absent: str) -> None:
    """Not skipped. A pair silently dropped from the report reads as an absence
    of opportunity, when it is an absence of data, and the two call for
    opposite responses from the trader."""
    partial = [s for s in universe() if s.currency != absent]

    with pytest.raises(KeyError) as caught:
        build_pair_biases(partial, config, ASOF)

    assert absent in str(caught.value)


# --- direction ---------------------------------------------------------------


def test_direction_is_long_at_the_threshold(scoring: ScoringConfig) -> None:
    assert direction_for(scoring.min_spread_low, scoring) is Direction.LONG


def test_direction_is_neutral_just_inside_the_threshold(
    scoring: ScoringConfig,
) -> None:
    assert direction_for(scoring.min_spread_low - 1e-9, scoring) is Direction.NEUTRAL


def test_direction_is_short_at_the_negative_threshold(
    scoring: ScoringConfig,
) -> None:
    assert direction_for(-scoring.min_spread_low, scoring) is Direction.SHORT


def test_direction_is_neutral_just_inside_the_negative_threshold(
    scoring: ScoringConfig,
) -> None:
    assert direction_for(-scoring.min_spread_low + 1e-9, scoring) is Direction.NEUTRAL


def test_direction_is_neutral_at_a_zero_spread(scoring: ScoringConfig) -> None:
    assert direction_for(0.0, scoring) is Direction.NEUTRAL


def test_the_direction_threshold_is_read_from_config() -> None:
    """Overridden rather than asserted at the default, so the assertions above
    cannot be passing against a hardcoded 0.75."""
    wide = ScoringConfig(min_spread_low=2.0)

    assert direction_for(1.0, ScoringConfig()) is Direction.LONG
    assert direction_for(1.0, wide) is Direction.NEUTRAL
    assert direction_for(2.0, wide) is Direction.LONG


# --- the conviction ladder ---------------------------------------------------


def clear(
    spread: float, config: ScoringConfig, **overrides: float | bool
) -> Conviction:
    """Conviction with every demotion switched off unless named.

    Keeps each demotion test to the one input it is about, so a test that fails
    names the rule that broke rather than the fixture.
    """
    inputs: dict[str, float | bool] = {
        "agreement_fraction": 1.0,
        "coverage_fraction": 1.0,
        "dispersion_value": 0.0,
        "event_within_24h": False,
    }
    inputs.update(overrides)
    return conviction_for(
        spread,
        float(inputs["agreement_fraction"]),
        float(inputs["coverage_fraction"]),
        float(inputs["dispersion_value"]),
        bool(inputs["event_within_24h"]),
        config,
    )


def test_the_base_tier_is_none_below_the_low_threshold(
    scoring: ScoringConfig,
) -> None:
    assert clear(scoring.min_spread_low - 1e-9, scoring) is Conviction.NONE


def test_the_base_tier_is_low_at_the_low_threshold(scoring: ScoringConfig) -> None:
    assert clear(scoring.min_spread_low, scoring) is Conviction.LOW


def test_the_base_tier_is_low_just_below_medium(scoring: ScoringConfig) -> None:
    assert clear(scoring.min_spread_medium - 1e-9, scoring) is Conviction.LOW


def test_the_base_tier_is_medium_at_the_medium_threshold(
    scoring: ScoringConfig,
) -> None:
    assert clear(scoring.min_spread_medium, scoring) is Conviction.MEDIUM


def test_the_base_tier_is_medium_just_below_high(scoring: ScoringConfig) -> None:
    assert clear(scoring.min_spread_high - 1e-9, scoring) is Conviction.MEDIUM


def test_the_base_tier_is_high_at_the_high_threshold(
    scoring: ScoringConfig,
) -> None:
    assert clear(scoring.min_spread_high, scoring) is Conviction.HIGH


def test_only_the_magnitude_of_the_spread_sets_the_tier(
    scoring: ScoringConfig,
) -> None:
    """Direction is `direction_for`'s job. A short at 2.6 is as well supported
    as a long at 2.6."""
    assert clear(2.6, scoring) is clear(-2.6, scoring) is Conviction.HIGH


def test_the_spread_thresholds_are_read_from_config() -> None:
    """So the ladder above cannot be passing against hardcoded 0.75, 1.5, 2.5."""
    tight = ScoringConfig(
        min_spread_low=0.25, min_spread_medium=0.50, min_spread_high=0.75
    )

    assert clear(0.80, ScoringConfig()) is Conviction.LOW
    assert clear(0.80, tight) is Conviction.HIGH


# --- the demotions, one at a time --------------------------------------------


def test_weak_agreement_caps_at_low(scoring: ScoringConfig) -> None:
    """One pillar carrying the whole spread. A cap rather than a demotion,
    because the objection does not get worse as the spread widens."""
    assert clear(2.8, scoring) is Conviction.HIGH
    assert (
        clear(2.8, scoring, agreement_fraction=scoring.min_agreement - 0.01)
        is Conviction.LOW
    )


def test_agreement_at_the_threshold_does_not_cap(scoring: ScoringConfig) -> None:
    assert (
        clear(2.8, scoring, agreement_fraction=scoring.min_agreement) is Conviction.HIGH
    )


def test_thin_coverage_demotes_one_step(scoring: ScoringConfig) -> None:
    assert (
        clear(2.8, scoring, coverage_fraction=scoring.coverage_demotion - 0.01)
        is Conviction.MEDIUM
    )


def test_coverage_at_the_threshold_does_not_demote(scoring: ScoringConfig) -> None:
    """Calibrated to the weight vector: missing a 0.15 pillar leaves 0.85 and
    does not demote, while missing the 0.30 monetary pillar leaves 0.70 and
    does."""
    assert (
        clear(2.8, scoring, coverage_fraction=scoring.coverage_demotion)
        is Conviction.HIGH
    )
    assert clear(2.8, scoring, coverage_fraction=0.85) is Conviction.HIGH
    assert clear(2.8, scoring, coverage_fraction=0.70) is Conviction.MEDIUM


def test_wide_dispersion_demotes_one_step(scoring: ScoringConfig) -> None:
    assert (
        clear(2.8, scoring, dispersion_value=scoring.max_dispersion + 0.01)
        is Conviction.MEDIUM
    )


def test_dispersion_at_the_threshold_does_not_demote(
    scoring: ScoringConfig,
) -> None:
    """The comparison is strictly greater, so sitting exactly on the ceiling is
    not yet a contradiction."""
    assert (
        clear(2.8, scoring, dispersion_value=scoring.max_dispersion) is Conviction.HIGH
    )


def test_an_event_within_a_day_caps_at_low(scoring: ScoringConfig) -> None:
    """A position opened today would be held through a repricing the model has
    not seen, and the rate path is what the heaviest pillar measures."""
    assert clear(2.8, scoring, event_within_24h=True) is Conviction.LOW


def test_the_demotion_thresholds_are_read_from_config() -> None:
    """Each of the three overridden away from its default, so none of the
    demotion tests can be passing against a hardcoded number."""
    loose = ScoringConfig(
        min_agreement=0.10, coverage_demotion=0.10, max_dispersion=9.0
    )

    assert clear(2.8, loose, agreement_fraction=0.50) is Conviction.HIGH
    assert clear(2.8, loose, coverage_fraction=0.50) is Conviction.HIGH
    assert clear(2.8, loose, dispersion_value=5.0) is Conviction.HIGH


# --- demotions compound ------------------------------------------------------


def test_every_demotion_at_once_reaches_none(scoring: ScoringConfig) -> None:
    """The issue's own case. The order matters and this is what pins it: the
    agreement cap takes HIGH to LOW first, and the two one-step demotions then
    take LOW to NONE. Applying the demotions first and the cap afterwards
    lands on LOW instead, which is the same four inputs reading two tiers
    apart.
    """
    assert (
        clear(
            2.8,
            scoring,
            agreement_fraction=0.40,
            coverage_fraction=0.70,
            dispersion_value=1.4,
        )
        is Conviction.NONE
    )


def test_two_demotions_compound(scoring: ScoringConfig) -> None:
    assert (
        clear(2.8, scoring, coverage_fraction=0.70, dispersion_value=1.4)
        is Conviction.LOW
    )


def test_conviction_never_rises(scoring: ScoringConfig) -> None:
    """Every input other than the spread is a reason to doubt it, so no
    combination may return a tier above the one the spread earns."""
    for spread in (0.0, 0.8, 1.6, 2.6, 3.0):
        base = clear(spread, scoring)
        for override in (
            {"agreement_fraction": 0.0},
            {"coverage_fraction": 0.0},
            {"dispersion_value": 99.0},
            {"event_within_24h": True},
        ):
            got = clear(spread, scoring, **override)
            assert LADDER.index(got) <= LADDER.index(base), (spread, override)


def test_a_demotion_cannot_go_below_none(scoring: ScoringConfig) -> None:
    """The bottom of the ladder is absorbing, not an index error."""
    assert (
        clear(0.1, scoring, coverage_fraction=0.10, dispersion_value=9.0)
        is Conviction.NONE
    )


# --- the hard floor is not a demotion ----------------------------------------


def test_coverage_below_the_hard_floor_still_receives_a_conviction(
    scoring: ScoringConfig,
) -> None:
    """`min_coverage` is a hard filter in `apply_filters`, not a demotion. At
    that point the question stops being how much to believe the number and
    becomes whether there is one, and that answer belongs in `blockers`."""
    below = scoring.min_coverage - 0.01

    assert clear(2.8, scoring, coverage_fraction=below) is not Conviction.NONE


def test_the_hard_floor_is_not_read_by_conviction_for() -> None:
    """Asserted on the source, because a `min_coverage` comparison that happened
    to agree with `coverage_demotion` at the defaults would pass every
    behavioural test in this file."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "fbe" / "bias.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "conviction_for"
    )
    # The docstring names `min_coverage` to say it is deliberately absent, so
    # it has to come out before the check, or this asserts the opposite of what
    # it means.
    statements = function.body
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    body = ast.unparse(ast.Module(body=statements, type_ignores=[]))

    assert "min_coverage" not in body
    assert "coverage_demotion" in body


# --- direction and conviction never disagree ---------------------------------


def test_conviction_none_forces_a_neutral_direction(config: Config) -> None:
    """A pair the model will not back at any size does not have a direction
    worth printing. The spread here is wide enough for LONG on its own."""
    scores = universe(EUR=1.6, USD=0.1)
    shocked = [
        leg("EUR", 1.6, coverage=0.10, dispersion=9.0) if s.currency == "EUR" else s
        for s in scores
    ]

    biases = build_pair_biases(shocked, config, ASOF)
    eurusd = next(b for b in biases if b.pair == "EURUSD")

    assert eurusd.conviction is Conviction.NONE
    assert eurusd.direction is Direction.NEUTRAL
    assert eurusd.spread > 0


def test_a_backed_pair_keeps_its_direction(config: Config) -> None:
    """The converse, so the test above is not passing because everything comes
    back neutral."""
    biases = build_pair_biases(universe(EUR=1.6, USD=0.1), config, ASOF)
    eurusd = next(b for b in biases if b.pair == "EURUSD")

    assert eurusd.conviction is not Conviction.NONE
    assert eurusd.direction is Direction.LONG


# --- which leg's coverage and dispersion are read ----------------------------


def test_the_worse_covered_leg_decides(config: Config) -> None:
    """The lower rather than the mean. Averaging would let a fully covered
    dollar hide a euro scored on three pillars: 1.0 and 0.60 average to 0.80,
    which is exactly the threshold and would not demote."""
    scores = [
        leg("EUR", 1.6, coverage=0.60) if s.currency == "EUR" else s
        for s in universe(EUR=1.6, USD=-1.4)
    ]

    biases = build_pair_biases(scores, config, ASOF)
    eurusd = next(b for b in biases if b.pair == "EURUSD")

    assert eurusd.spread == pytest.approx(3.0)
    assert eurusd.conviction is Conviction.MEDIUM


def test_the_more_dispersed_leg_decides(config: Config) -> None:
    """The higher rather than the mean, for the mirror reason: one leg whose own
    pillars contradict each other is enough. 0.0 and 2.4 average to 1.2, which
    is exactly the ceiling and would not demote."""
    scores = [
        leg("EUR", 1.6, dispersion=2.4) if s.currency == "EUR" else s
        for s in universe(EUR=1.6, USD=-1.4)
    ]

    biases = build_pair_biases(scores, config, ASOF)
    eurusd = next(b for b in biases if b.pair == "EURUSD")

    assert eurusd.conviction is Conviction.MEDIUM


# --- agreement ---------------------------------------------------------------


def test_all_seven_pillars_agreeing_is_one(config: Config) -> None:
    assert agreement(leg("EUR", 1.6), leg("USD", 0.1)) == pytest.approx(1.0)


def test_agreement_is_weighted_by_pillar_weight_not_counted() -> None:
    """One heavy dissenter against several light agreeing pillars.

    MONETARY at 0.30 dissents; EMPLOYMENT, EXTERNAL, POSITIONING and RISK, worth
    0.10 each, agree. A headcount gives four of five, or 0.80, and passes
    `min_agreement`. The weighted share is 0.40 of 0.70, which is 0.5714, and
    fails it. The two readings put this pair on different sides of the cap.
    """
    base = leg(
        "EUR",
        1.0,
        pillars={
            PillarName.MONETARY: -1.0,
            PillarName.EMPLOYMENT: 1.0,
            PillarName.EXTERNAL: 1.0,
            PillarName.POSITIONING: 1.0,
            PillarName.RISK: 1.0,
        },
    )
    quote = leg(
        "USD",
        0.0,
        pillars={
            PillarName.MONETARY: 1.0,
            PillarName.EMPLOYMENT: 0.0,
            PillarName.EXTERNAL: 0.0,
            PillarName.POSITIONING: 0.0,
            PillarName.RISK: 0.0,
        },
    )

    assert agreement(base, quote) == pytest.approx(0.40 / 0.70, abs=1e-9)
    assert agreement(base, quote) < ScoringConfig().min_agreement


def test_a_pillar_scoring_both_legs_alike_is_excluded_from_both_sides() -> None:
    """It expresses no opinion on this pair. Leaving it in the denominator would
    make a thin run look like a disputed one."""
    shared = {PillarName.MONETARY: 1.0, PillarName.GROWTH: 0.5}
    base = leg("EUR", 1.0, pillars=shared)
    quote = leg("USD", 0.0, pillars={PillarName.MONETARY: 0.0, PillarName.GROWTH: 0.5})

    assert agreement(base, quote) == pytest.approx(1.0)


def test_a_tie_inside_the_epsilon_is_still_a_tie() -> None:
    """A float-equality guard, not a modelling threshold."""
    from fbe.bias import AGREEMENT_TIE_EPSILON

    base = leg("EUR", 1.0, pillars={PillarName.MONETARY: 1.0, PillarName.GROWTH: 1.0})
    quote = leg(
        "USD",
        0.0,
        pillars={
            PillarName.MONETARY: 0.0,
            PillarName.GROWTH: 1.0 - AGREEMENT_TIE_EPSILON / 2,
        },
    )

    assert agreement(base, quote) == pytest.approx(1.0)


def test_the_effective_weights_are_the_ones_used() -> None:
    """`PillarScore.weight` carries the configured weight before the staleness
    penalty and the effective weight after it. A stale pillar counts for less on
    this pair, which is the point: agreement should measure the evidence that
    exists, not the slots on the form."""
    heavy = {PillarName.MONETARY: 0.30, PillarName.GROWTH: 0.15}
    penalised = {PillarName.MONETARY: 0.03, PillarName.GROWTH: 0.15}
    scores = {PillarName.MONETARY: 1.0, PillarName.GROWTH: -1.0}
    quote_scores = {PillarName.MONETARY: 0.0, PillarName.GROWTH: 0.0}

    fresh = agreement(
        leg("EUR", 1.0, pillars=scores, weights=heavy),
        leg("USD", 0.0, pillars=quote_scores, weights=heavy),
    )
    stale = agreement(
        leg("EUR", 1.0, pillars=scores, weights=penalised),
        leg("USD", 0.0, pillars=quote_scores, weights=penalised),
    )

    assert fresh == pytest.approx(0.30 / 0.45)
    assert stale == pytest.approx(0.03 / 0.18)
    assert stale < fresh


def test_no_pillar_with_an_opinion_gives_zero() -> None:
    """Pairs with a coverage figure low enough to block the trade anyway, so
    zero is the honest answer rather than a dodge."""
    identical = {PillarName.MONETARY: 1.0}
    assert (
        agreement(
            leg("EUR", 1.0, pillars=identical),
            leg("USD", 1.0, pillars=identical),
        )
        == 0.0
    )


def test_agreement_reaches_the_bias(config: Config) -> None:
    biases = build_pair_biases(universe(EUR=1.6, USD=0.1), config, ASOF)
    eurusd = next(b for b in biases if b.pair == "EURUSD")

    assert eurusd.agreement == pytest.approx(1.0)


# --- the injected guard ------------------------------------------------------


def test_the_calendar_is_never_imported_here() -> None:
    """The dependency runs one way: the calendar module knows about dates and
    events, this one knows about scores. Asserted on the module's own imports,
    because an import added for one convenience call is how that seam goes."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "fbe" / "bias.py"
    ).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # The alias names as well as the module. `from fbe import
            # calendar_guard` puts the module being imported in the alias, not
            # in `node.module`, and an earlier version of this test read only
            # the latter and so missed exactly that spelling.
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    assert not any("calendar_guard" in name for name in imported), sorted(imported)


def test_no_guard_applies_no_cap(config: Config) -> None:
    """The right default for a backtest and the wrong one for a live run, which
    is why the caller should pass one."""
    biases = build_pair_biases(universe(EUR=1.6, USD=-1.4), config, ASOF, None)
    eurusd = next(b for b in biases if b.pair == "EURUSD")

    assert eurusd.conviction is Conviction.HIGH


def test_a_guard_reporting_an_event_caps_the_pair(config: Config) -> None:
    def due(currency: str, when: date) -> bool:
        return currency == "EUR"

    biases = build_pair_biases(universe(EUR=1.6, USD=-1.4), config, ASOF, due)
    eurusd = next(b for b in biases if b.pair == "EURUSD")
    audcad = next(b for b in biases if b.pair == "AUDCAD")

    assert eurusd.conviction is Conviction.LOW
    assert audcad.conviction is Conviction.NONE


def test_an_event_on_either_leg_caps_the_pair(config: Config) -> None:
    """Either leg, not only the base. A dollar decision tomorrow is held through
    by every pair the dollar appears in, on whichever side."""

    def due(currency: str, when: date) -> bool:
        return currency == "USD"

    biases = build_pair_biases(universe(EUR=1.6, USD=-1.4), config, ASOF, due)
    eurusd = next(b for b in biases if b.pair == "EURUSD")

    assert eurusd.quote == "USD"
    assert eurusd.conviction is Conviction.LOW


def test_the_guard_is_asked_about_the_run_date(config: Config) -> None:
    asked: list[tuple[str, date]] = []

    def record(currency: str, when: date) -> bool:
        asked.append((currency, when))
        return False

    build_pair_biases(universe(), config, ASOF, record)

    assert asked
    assert {when for _, when in asked} == {ASOF}


def test_the_guard_is_asked_only_about_the_currencies_in_play(
    config: Config,
) -> None:
    """A guard is a calendar fetch per currency. Asking about something outside
    the universe would be a wasted call and a sign the pair split is wrong."""
    asked: set[str] = set()

    def record(currency: str, when: date) -> bool:
        asked.add(currency)
        return False

    build_pair_biases(universe(), config, ASOF, record)

    assert asked <= set(G10)


# --- what this issue does not do ---------------------------------------------


def test_nothing_here_sets_tradeable_or_blockers(config: Config) -> None:
    """`apply_filters` owns both and is a separate issue. A bias arriving with
    `tradeable` already False would make that function's output depend on
    whether it had run."""
    biases = build_pair_biases(universe(EUR=1.6, USD=-1.4), config, ASOF)

    assert all(b.tradeable for b in biases)
    assert all(list(b.blockers) == [] for b in biases)


def test_the_run_date_reaches_every_bias(config: Config) -> None:
    biases: Sequence[object] = build_pair_biases(universe(), config, ASOF)

    assert all(b.asof == ASOF for b in biases)
