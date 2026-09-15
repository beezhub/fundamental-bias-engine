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


def test_a_guard_reporting_an_event_caps_only_the_pairs_carrying_it(
    config: Config,
) -> None:
    """The cap reaches the pairs holding that currency and no others.

    AUDCAD carries a real spread here on purpose. An earlier version left it at
    zero, which puts it at the floor of the ladder where a cap cannot show, so
    an implementation capping every pair in the run passed. It now sits at HIGH
    and has somewhere to fall from.
    """

    def due(currency: str, when: date) -> bool:
        return currency == "EUR"

    biases = build_pair_biases(
        universe(EUR=1.6, USD=-1.4, AUD=1.6, CAD=-1.4), config, ASOF, due
    )
    eurusd = next(b for b in biases if b.pair == "EURUSD")
    audcad = next(b for b in biases if b.pair == "AUDCAD")

    assert eurusd.conviction is Conviction.LOW
    assert audcad.spread == pytest.approx(3.0)
    assert audcad.conviction is Conviction.HIGH


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


# --- gaps the review pass found ----------------------------------------------


def test_the_run_config_is_the_one_used(config: Config) -> None:
    """`build_pair_biases` takes a whole `Config` and must read its scoring
    section rather than building a default.

    Nothing else in this file passes a non-default config to it, so a module
    ignoring the run's configuration entirely passed every other test here.
    That is the config-drift row of the prime directive table one layer up: the
    thresholds exist in `ScoringConfig`, and a second copy reached by
    constructing a fresh one would disagree with the run silently.
    """
    scores = universe(EUR=1.6, USD=0.1)
    widened = Config(scoring=ScoringConfig(min_spread_low=2.0, min_spread_medium=3.0))

    default = next(
        b for b in build_pair_biases(scores, config, ASOF) if b.pair == "EURUSD"
    )
    under_widened = next(
        b for b in build_pair_biases(scores, widened, ASOF) if b.pair == "EURUSD"
    )

    assert default.direction is Direction.LONG
    assert default.conviction is Conviction.MEDIUM
    assert under_widened.direction is Direction.NEUTRAL
    assert under_widened.conviction is Conviction.NONE

    # The widened case alone cannot see a direction built from a default
    # config, because it lands at NONE and the forcing rule makes the direction
    # NEUTRAL either way. Narrowing instead separates them: the spread is below
    # the default threshold and above this run's, so the direction is LONG only
    # if this run's config was the one read.
    narrowed = Config(scoring=ScoringConfig(min_spread_low=0.25))
    under_narrowed = next(
        b
        for b in build_pair_biases(universe(EUR=0.5, USD=0.0), narrowed, ASOF)
        if b.pair == "EURUSD"
    )

    assert under_narrowed.conviction is not Conviction.NONE
    assert under_narrowed.direction is Direction.LONG


def test_the_worse_covered_leg_decides_on_either_side(config: Config) -> None:
    """The mirror of `test_the_worse_covered_leg_decides`.

    That one degrades EUR, which is EURUSD's base, so taking the base leg's
    coverage and taking the lower of the two are indistinguishable. Degrading
    the quote leg instead is what separates them, and it is the pair-convention
    rule applied to the leg selectors rather than to the pair string.
    """
    scores = [
        leg("USD", -1.4, coverage=0.60) if s.currency == "USD" else s
        for s in universe(EUR=1.6, USD=-1.4)
    ]

    eurusd = next(
        b for b in build_pair_biases(scores, config, ASOF) if b.pair == "EURUSD"
    )

    assert eurusd.quote == "USD"
    assert eurusd.conviction is Conviction.MEDIUM


def test_the_more_dispersed_leg_decides_on_either_side(config: Config) -> None:
    """The mirror of `test_the_more_dispersed_leg_decides`, same reason."""
    scores = [
        leg("USD", -1.4, dispersion=2.4) if s.currency == "USD" else s
        for s in universe(EUR=1.6, USD=-1.4)
    ]

    eurusd = next(
        b for b in build_pair_biases(scores, config, ASOF) if b.pair == "EURUSD"
    )

    assert eurusd.conviction is Conviction.MEDIUM


def test_the_computed_agreement_reaches_the_conviction(config: Config) -> None:
    """Every other pair in this file has all seven pillars equal to its
    composite, so agreement is 1.0 everywhere and passing a constant 1.0 into
    `conviction_for` was indistinguishable from passing the computed share.

    Here MONETARY dissents at 0.30 against four agreeing pillars worth 0.10
    each, which is 0.571 and below `min_agreement`, so the cap must fire.
    """
    dissenting = {
        PillarName.MONETARY: -3.0,
        PillarName.EMPLOYMENT: 3.0,
        PillarName.EXTERNAL: 3.0,
        PillarName.POSITIONING: 3.0,
        PillarName.RISK: 3.0,
    }
    scores = [
        leg("EUR", 1.6, pillars=dissenting) if s.currency == "EUR" else s
        for s in universe(EUR=1.6, USD=-1.4)
    ]

    eurusd = next(
        b for b in build_pair_biases(scores, config, ASOF) if b.pair == "EURUSD"
    )

    assert eurusd.agreement < ScoringConfig().min_agreement
    assert eurusd.spread == pytest.approx(3.0)
    assert eurusd.conviction is Conviction.LOW


def test_the_event_cap_applies_after_the_demotions(scoring: ScoringConfig) -> None:
    """The fourth rule's position in the ladder, which the other three have
    pinned and this one did not: every event test ran with full coverage and no
    dispersion, so the cap was never combined with a demotion.

    HIGH, demoted once by thin coverage to MEDIUM, then capped to LOW. Moved to
    the front of the sequence the cap would take HIGH to LOW first and the
    demotion would then take it to NONE, one tier lower on the same inputs.
    """
    assert (
        clear(2.8, scoring, coverage_fraction=0.70, event_within_24h=True)
        is Conviction.LOW
    )


def test_a_pillar_only_one_leg_carries_is_excluded() -> None:
    """It has no difference to have an opinion with, so it belongs on neither
    side of the fraction. Substituting a zero for the missing side would invent
    a disagreement out of an absence, which is the rule this repository is
    built around.

    Nothing else in this file gives the two legs different pillar sets.
    """
    base = leg(
        "EUR",
        1.0,
        pillars={PillarName.MONETARY: 1.0, PillarName.GROWTH: -1.0},
        weights={PillarName.MONETARY: 0.30, PillarName.GROWTH: 0.15},
    )
    quote = leg(
        "USD",
        0.0,
        pillars={PillarName.MONETARY: 0.0},
        weights={PillarName.MONETARY: 0.30},
    )

    # Only MONETARY can be differenced, and it agrees with the positive spread.
    assert agreement(base, quote) == pytest.approx(1.0)


def test_the_pair_weight_is_the_mean_of_the_two_legs() -> None:
    """`w_pair(p)` is the mean of the two legs' effective weights, so a pillar
    stale on one side counts for less on this pair than one fresh on both.

    `test_the_effective_weights_are_the_ones_used` hands both legs the same
    mapping, so it cannot tell the mean from either leg's own weight. Here they
    differ, and the three readings give three different answers.
    """
    base = leg(
        "EUR",
        1.0,
        pillars={PillarName.MONETARY: 1.0, PillarName.GROWTH: -1.0},
        weights={PillarName.MONETARY: 0.30, PillarName.GROWTH: 0.15},
    )
    quote = leg(
        "USD",
        0.0,
        pillars={PillarName.MONETARY: 0.0, PillarName.GROWTH: 0.0},
        weights={PillarName.MONETARY: 0.10, PillarName.GROWTH: 0.15},
    )

    # MONETARY agrees at a pair weight of (0.30 + 0.10) / 2 = 0.20, GROWTH
    # dissents at (0.15 + 0.15) / 2 = 0.15. The base leg's own weights would
    # give 0.30 / 0.45 and the quote leg's 0.10 / 0.25.
    assert agreement(base, quote) == pytest.approx(0.20 / 0.35)


def test_a_tie_is_excluded_from_the_denominator_as_well() -> None:
    """The earlier tie test asserts 1.0, which an implementation counting ties
    into both numerator and denominator also returns. A dissenting pillar
    alongside the tie separates them: excluded gives 0.5, counted into both
    gives 0.667."""
    base = leg(
        "EUR",
        1.0,
        pillars={
            PillarName.MONETARY: 1.0,
            PillarName.GROWTH: -1.0,
            PillarName.RISK: 0.5,
        },
        weights={
            PillarName.MONETARY: 0.10,
            PillarName.GROWTH: 0.10,
            PillarName.RISK: 0.10,
        },
    )
    quote = leg(
        "USD",
        0.0,
        pillars={
            PillarName.MONETARY: 0.0,
            PillarName.GROWTH: 0.0,
            PillarName.RISK: 0.5,
        },
        weights={
            PillarName.MONETARY: 0.10,
            PillarName.GROWTH: 0.10,
            PillarName.RISK: 0.10,
        },
    )

    assert agreement(base, quote) == pytest.approx(0.5)


def test_a_zero_spread_gives_no_agreement() -> None:
    """Section 5.3 compares ``sign(d(p))`` against ``sign(spread)``, and a
    spread of exactly zero has sign zero, which no difference can match. A
    boolean test would count every negative-leaning pillar as agreeing with a
    spread that leans neither way, and `PairBias.agreement` is rendered."""
    base = leg("EUR", 1.0, pillars={PillarName.MONETARY: 1.0, PillarName.GROWTH: -1.0})
    quote = leg("USD", 1.0, pillars={PillarName.MONETARY: 0.0, PillarName.GROWTH: 0.0})

    assert base.composite - quote.composite == 0.0
    assert agreement(base, quote) == 0.0


def test_the_guard_is_asked_once_per_currency(config: Config) -> None:
    """Not once per pair. Each currency sits in seven of the 28 pairs, and the
    reason is correctness rather than cost: two calls for one currency could
    disagree inside a run, putting two pairs that share a leg on opposite sides
    of the cap with nothing recording why."""
    calls: list[str] = []

    def record(currency: str, when: date) -> bool:
        calls.append(currency)
        return False

    build_pair_biases(universe(), config, ASOF, record)

    assert len(calls) == len(set(calls)) == len(G10)


def test_a_guard_that_changes_its_mind_still_answers_once(config: Config) -> None:
    """The case the rule above exists for, stated as behaviour rather than as a
    call count: every pair carrying the currency lands on the same side of the
    cap, whichever answer the guard gave first."""
    seen: list[str] = []

    def flip_flop(currency: str, when: date) -> bool:
        seen.append(currency)
        return seen.count(currency) == 1

    biases = build_pair_biases(
        universe(**{c: 1.6 if i % 2 else -1.4 for i, c in enumerate(G10)}),
        config,
        ASOF,
        flip_flop,
    )
    carrying_eur = [b for b in biases if "EUR" in (b.base, b.quote)]

    assert len(carrying_eur) == 7
    assert len({b.conviction for b in carrying_eur if abs(b.spread) >= 2.5}) <= 1


def test_a_pillar_with_no_data_is_excluded_from_the_agreement() -> None:
    """The exclusion that matters in a real run, and the one a review pass
    found missing.

    `test_a_pillar_only_one_leg_carries_is_excluded` tests a shape that cannot
    occur: `BasePillar.compute` returns one `PillarScore` per currency
    including the ones with no usable data, so an absent pillar is present in
    the mapping carrying `missing_score`'s neutral `0.0` with `z` of `None` and
    an effective weight the staleness penalty has taken to zero.

    Left in, its difference is `0.0 - score(other leg)`, a sign the leg with
    data decides on its own, and it is counted as an opinion.
    """
    scores = {PillarName.MONETARY: 2.0, PillarName.POSITIONING: 0.0}
    base = leg(
        "EUR",
        1.0,
        pillars=scores,
        weights={PillarName.MONETARY: 0.30, PillarName.POSITIONING: 0.0},
    )
    quote = leg(
        "USD",
        0.0,
        pillars={PillarName.MONETARY: 0.0, PillarName.POSITIONING: -1.2},
        weights={PillarName.MONETARY: 0.30, PillarName.POSITIONING: 0.10},
    )
    # POSITIONING has no data on the base leg.
    absent = PillarScore(
        pillar=PillarName.POSITIONING,
        currency="EUR",
        raw=None,
        z=None,
        score=0.0,
        weight=0.0,
        asof=ASOF,
    )
    base = CurrencyScore(
        currency="EUR",
        composite=1.0,
        pillars={**base.pillars, PillarName.POSITIONING: absent},
        asof=ASOF,
        coverage=0.90,
    )

    # MONETARY alone is considered, and it agrees with the positive spread.
    # Counting POSITIONING would add 0.05 of agreeing weight on the strength of
    # the quote leg's -1.2 against a neutral that is not a reading.
    assert agreement(base, quote) == pytest.approx(1.0)


def test_a_no_data_pillar_cannot_raise_the_conviction_a_tier(
    scoring: ScoringConfig,
) -> None:
    """The consequence, priced. The worked case in section 5.3 of
    `docs/scoring-spec.md`: a EUR leg missing POSITIONING reads 0.6154 with the
    pillar counted, which clears `min_agreement` and returns MEDIUM, against
    0.5833 with it excluded, which does not and returns LOW.

    `docs/risk-and-execution.md` puts MEDIUM at 1.5% of the account and LOW at
    1.0%, so the difference is half as much again at risk on a pair whose
    seventh pillar had nothing to say.
    """
    eur_scores = {
        PillarName.MONETARY: 1.1,
        PillarName.INFLATION: -2.8,
        PillarName.GROWTH: 2.0,
        PillarName.EMPLOYMENT: -1.0,
        PillarName.EXTERNAL: 0.7,
        PillarName.RISK: -2.2,
    }
    usd_scores = {
        PillarName.MONETARY: 1.1,
        PillarName.INFLATION: 2.7,
        PillarName.GROWTH: 1.1,
        PillarName.EMPLOYMENT: 2.1,
        PillarName.EXTERNAL: 0.3,
        PillarName.POSITIONING: 1.2,
        PillarName.RISK: 2.7,
    }
    weights = ScoringConfig().weights
    absent = PillarScore(
        pillar=PillarName.POSITIONING,
        currency="EUR",
        raw=None,
        z=None,
        score=0.0,
        weight=0.0,
        asof=ASOF,
    )
    base = CurrencyScore(
        currency="EUR",
        composite=sum(eur_scores[p] * weights[p] for p in eur_scores),
        pillars={
            **{
                p: pillar(p, "EUR", value, weights[p])
                for p, value in eur_scores.items()
            },
            PillarName.POSITIONING: absent,
        },
        asof=ASOF,
        coverage=0.90,
    )
    quote = leg(
        "USD", sum(usd_scores[p] * weights[p] for p in usd_scores), pillars=usd_scores
    )
    spread = base.composite - quote.composite
    share = agreement(base, quote)

    assert share == pytest.approx(0.5833, abs=5e-5)
    assert share < scoring.min_agreement
    assert conviction_for(spread, share, 0.90, 0.0, False, scoring) is Conviction.LOW


def test_a_no_data_pillar_on_the_quote_leg_is_excluded_too(
    scoring: ScoringConfig,
) -> None:
    """The mirror. Testing the base leg alone cannot tell "either leg" from
    "the base leg", which is the pair-convention rule again, this time inside
    the agreement loop."""
    absent = PillarScore(
        pillar=PillarName.POSITIONING,
        currency="USD",
        raw=None,
        z=None,
        score=0.0,
        weight=0.0,
        asof=ASOF,
    )
    # The base-side POSITIONING leans the other way from the spread, so
    # counting the pair at all changes the fraction. With it leaning the same
    # way, including it and excluding it both give 1.0 and the test cannot see
    # a guard that checks only the base leg.
    base = leg(
        "EUR",
        1.0,
        pillars={PillarName.MONETARY: 2.0, PillarName.POSITIONING: -1.2},
        weights={PillarName.MONETARY: 0.30, PillarName.POSITIONING: 0.10},
    )
    quote = CurrencyScore(
        currency="USD",
        composite=0.0,
        pillars={
            PillarName.MONETARY: pillar(PillarName.MONETARY, "USD", 0.0, 0.30),
            PillarName.POSITIONING: absent,
        },
        asof=ASOF,
        coverage=0.90,
    )

    # Only MONETARY is considered, at 0.30, and it agrees. Counting POSITIONING
    # would add 0.05 of dissenting weight from a quote-side neutral that is not
    # a reading, giving 0.30 / 0.35.
    assert agreement(base, quote) == pytest.approx(1.0)


def test_a_missing_currency_stops_the_run_before_the_guard_is_called(
    config: Config,
) -> None:
    """The guard may be a calendar fetch per currency, and a run missing a leg
    raises either way, so it should raise before spending them."""
    calls: list[str] = []

    def record(currency: str, when: date) -> bool:
        calls.append(currency)
        return False

    partial = [s for s in universe() if s.currency != "EUR"]

    with pytest.raises(KeyError):
        build_pair_biases(partial, config, ASOF, record)

    assert calls == []
