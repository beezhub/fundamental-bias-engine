"""How much of the pair ranking was resting on one pillar weight.

#287. The seven weights in `fbe.config.ScoringConfig` are priors. They are
reasoned and documented and they are not evidence, so a ranking that reorders
when ``MONETARY`` moves from 0.30 to 0.35 is resting on a number nobody has
measured. This module checks the thing that measures that.

**What these tests are not.** Nothing here asserts that a weight is right, that
a pillar earns its share, or that any figure predicts a return. The quantity
under test is how far the model's own output moves when one of the model's own
choices moves, which is a property of one morning's cross-section. A pillar
whose removal barely moves the ranking is not thereby useless, and one whose
removal moves it a lot is not thereby right. Both halves are asserted below,
because the output is read by someone deciding whether to propose a weight
change and either half alone points them at the wrong conclusion.

The fixture
-----------
`tests/fixtures/make_weight_sensitivity_run.py` writes two files in one pass:
the observations and the report the engine made of them. Both are needed and
they are committed together, because a `fbe.types.BiasReport` carries neither
its observations nor a pointer to them. Every number in the report came out of
`fbe.scoring.score_currencies` and `fbe.bias.build_pair_biases`, which is what
makes the reproduction assertion mean anything: if the report had been written
by hand, reproducing it would say only that two hand-written tables match.

Nothing here reaches the network. Both files are read from disk.

Where a number is checked, it is recomputed here from the weights and the
report rather than read back from the thing under test. An assertion that
compares an implementation to itself passes whatever the implementation does.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from fbe.config import Config, ScoringConfig
from fbe.evaluation import (
    CROSS_SECTION_NOTE,
    DEFAULT_WEIGHT_STEP,
    WeightMove,
    WeightSensitivity,
    weight_sensitivity,
)
from fbe.report import load_report, ranked_pairs
from fbe.types import Frequency, Observation, PillarName

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fbe.types import BiasReport

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPORT = FIXTURES / "bias-2026-03-02.json"
OBSERVATIONS = FIXTURES / "weight_sensitivity_observations.json"


def no_events(currency: str, asof: date) -> bool:
    """The guard the fixture was built with, passed back on every call.

    It answers ``False`` everywhere, which is what `fbe.bias` reads an absent
    guard as, so passing it and passing nothing give the same run. It is
    threaded through anyway because the argument being wired is a property
    worth exercising, and because a later fixture built under a capping guard
    would need it and would otherwise fail in a way that looks like a data
    problem.
    """
    return False


@pytest.fixture
def report() -> BiasReport:
    return load_report(REPORT)


@pytest.fixture
def observations() -> tuple[Observation, ...]:
    """The observations the fixture report was scored from.

    Rebuilt field by field rather than through a loader, so a field added to
    `fbe.types.Observation` fails here with a name rather than being filled
    from its default and quietly changing what the run was scored on.
    """
    rows = json.loads(OBSERVATIONS.read_text())
    return tuple(
        Observation(
            indicator=row["indicator"],
            currency=row["currency"],
            value=row["value"],
            period=date.fromisoformat(row["period"]),
            source=row["source"],
            series_id=row["series_id"],
            unit=row["unit"],
            frequency=Frequency(row["frequency"]),
            released_at=(
                None
                if row["released_at"] is None
                else datetime.fromisoformat(row["released_at"])
            ),
            revision=row["revision"],
        )
        for row in rows
    )


def _renormalised(
    weights: Mapping[PillarName, float], pillar: PillarName, step: float
) -> dict[PillarName, float]:
    """The perturbed vector, computed here rather than asked of the module.

    One pillar takes the step and the other six are scaled by whatever is left,
    which is the only construction that both moves one weight by a stated
    amount and keeps the vector summing to 1.0.
    """
    moved = weights[pillar] + step
    rest = 1.0 - weights[pillar]
    scale = (1.0 - moved) / rest
    return {
        name: (moved if name == pillar else weight * scale)
        for name, weight in weights.items()
    }


# --- criterion 1: the vector still sums to one ------------------------------


def test_every_perturbation_leaves_the_weights_summing_to_one(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 1. A vector that does not sum to 1.0 is not a re-weighting.

    The composite is a weighted sum, so a vector summing to 1.05 raises every
    currency's score by roughly five percent and the spread between two of them
    with it. That moves the ranking through the conviction bands without any
    pillar having changed its mind, which is the reading this whole measurement
    exists to avoid: a fragility that is really an arithmetic mistake.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)

    assert result.moves, "no perturbations were run"
    for move in result.moves:
        assert sum(move.weights.values()) == pytest.approx(1.0, abs=1e-12), move.pillar


def test_each_pillar_is_perturbed_up_and_down(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 1's other half: seven pillars, both directions, fourteen rows.

    Only one direction would hide an asymmetry, and the asymmetry is real: the
    other six weights are scaled by ``(1 - w - step) / (1 - w)`` going up and by
    ``(1 - w + step) / (1 - w)`` coming down, which are not the same factor.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)

    seen = {(move.pillar, move.step > 0) for move in result.moves}
    expected = {(pillar, up) for pillar in PillarName for up in (True, False)}

    assert seen == expected
    assert len(result.moves) == len(expected)


def test_the_moved_weight_moves_by_exactly_the_step(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """The named pillar takes the whole step; the others absorb the remainder.

    Asserted separately from the sum, because a construction that spread the
    step across all seven would also sum to 1.0 and would measure something
    else entirely.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)
    weights = ScoringConfig().weights

    for move in result.moves:
        assert move.weight_before == pytest.approx(weights[move.pillar])
        assert move.weight_after == pytest.approx(weights[move.pillar] + move.step)
        assert move.weights[move.pillar] == pytest.approx(move.weight_after)


# --- criterion 2: the step is a parameter -----------------------------------


def test_the_step_has_a_stated_default_rather_than_a_buried_literal() -> None:
    """Criterion 2. The default is a named constant with the value in one place.

    A literal inside the function is the config-drift defect this repository
    has already paid for once: the number appears in the output, a reader
    quotes it, and the next edit moves it with nothing to fail.
    """
    assert pytest.approx(0.05) == DEFAULT_WEIGHT_STEP


def test_overriding_the_step_changes_the_output(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 2. The parameter is wired, not accepted and ignored.

    Asserted on the perturbed weights rather than on the counts: a step can
    legitimately leave every count unchanged on a cross-section this wide, and
    a test asserting the counts move would fail for a true implementation.
    """
    small = weight_sensitivity(
        report, observations, event_horizon_guard=no_events, step=0.01
    )
    large = weight_sensitivity(
        report, observations, event_horizon_guard=no_events, step=0.20
    )

    assert small.step == pytest.approx(0.01)
    assert large.step == pytest.approx(0.20)

    by_pillar_small = {(m.pillar, m.step > 0): m.weight_after for m in small.moves}
    by_pillar_large = {(m.pillar, m.step > 0): m.weight_after for m in large.moves}

    # A step of 0.20 takes six of the seven weights below zero on the way
    # down, MONETARY at 0.30 being the only one that survives it, so those
    # rows are absent from the larger run by design. Compared
    # over the rows both produced, rather than over the union, which would
    # fail on the exclusion rather than on the step.
    shared = by_pillar_small.keys() & by_pillar_large.keys()

    assert shared, "the two steps share no perturbation to compare"
    for key in shared:
        assert by_pillar_small[key] != pytest.approx(by_pillar_large[key]), key
    assert len(by_pillar_large) < len(by_pillar_small)


def test_a_step_that_cannot_be_applied_is_reported_rather_than_clipped(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """A weight cannot go below zero or above one, and clipping would lie.

    At a step of 0.5 every downward move lands below zero. Clipping to zero
    would report a move of 0.10 as a move of 0.5 and attribute whatever the
    ranking did to the larger number. The pillar is left out and said to be
    left out, which is `docs/decisions/0002-representing-not-known.md` applied
    to a perturbation that could not be made.
    """
    result = weight_sensitivity(
        report, observations, event_horizon_guard=no_events, step=0.5
    )

    assert result.problems, "an impossible step produced no problem line"
    for problem in result.problems:
        assert "0.5" in problem or "0.50" in problem
    for move in result.moves:
        assert 0.0 <= move.weight_after <= 1.0


# --- criterion 3: the three counts ------------------------------------------


def test_the_counts_are_recomputed_against_the_unperturbed_run(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 3, checked by recomputing one row from the report itself.

    ``MONETARY`` up by the default step is rescored here from the same two
    functions, and the three figures are counted off the two pair lists by
    hand. If the module and this test agree, they agree about a number neither
    took from the other.
    """
    from fbe.bias import build_pair_biases
    from fbe.pillars import default_pillars
    from fbe.scoring import score_currencies

    config = Config()
    perturbed = _renormalised(config.scoring.weights, PillarName.INFLATION, 0.05)
    scoring = ScoringConfig(
        weights=perturbed,
        **{
            field: getattr(config.scoring, field)
            for field in ("score_clip", "lookback_years")
        },
    )
    moved_config = Config(
        risk=config.risk,
        scoring=scoring,
        data=config.data,
        broker=config.broker,
    )
    scores = score_currencies(
        observations, default_pillars(scoring), scoring, report.asof
    )
    moved_pairs = build_pair_biases(
        scores, moved_config, report.asof, event_horizon_guard=no_events
    )

    before = {row.pair: row for row in report.pairs}
    after = {row.pair: row for row in moved_pairs}

    directions = sum(
        1 for pair in before if before[pair].direction is not after[pair].direction
    )
    convictions = sum(
        1 for pair in before if before[pair].conviction is not after[pair].conviction
    )
    before_rank = {row.pair: i for i, row in enumerate(ranked_pairs(report.pairs))}
    after_rank = {row.pair: i for i, row in enumerate(ranked_pairs(moved_pairs))}
    largest = max(abs(before_rank[pair] - after_rank[pair]) for pair in before_rank)

    worst = min(
        pair
        for pair in before_rank
        if abs(before_rank[pair] - after_rank[pair]) == largest
    )

    move = next(
        m
        for m in weight_sensitivity(
            report, observations, event_horizon_guard=no_events
        ).moves
        if m.pillar is PillarName.INFLATION and m.step > 0
    )

    # INFLATION up, which is the one row that recomputes all three figures.
    # MONETARY up has both counts at zero, so comparing 0 against 0 checked
    # only the rank move. EXTERNAL up has them equal at 2, so a conviction
    # count that was really the direction count passed against it. Here they
    # are 2 and 3, and the assertion below fails if either drifts.
    assert directions > 0, "the recomputed row has stopped exercising the counts"
    assert convictions != directions, (
        "the recomputed row no longer separates the two counts, so a "
        "conviction count that returns the direction count would pass"
    )
    assert move.directions_changed == directions
    assert move.convictions_changed == convictions
    assert move.largest_rank_move == largest
    assert move.largest_rank_move_pair == worst


def test_the_counts_are_never_negative_and_never_exceed_the_universe(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """The bounds, which catch a count taken over the wrong collection.

    Twenty-eight pairs, so a direction count of 30 means the rows were compared
    against something other than the run they came from.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)

    for move in result.moves:
        assert 0 <= move.directions_changed <= len(report.pairs)
        assert 0 <= move.convictions_changed <= len(report.pairs)
        assert 0 <= move.largest_rank_move < len(report.pairs)


def test_a_pillar_perturbation_moves_something_on_this_fixture(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Guards every other assertion here from passing on an all-zero result.

    A function returning fourteen rows of zeroes satisfies the bounds, the sum
    and the reproduction check. The fixture was built with a pair sitting near
    the neutral boundary precisely so that something moves, and if nothing
    moves the fixture has stopped exercising the measurement.
    """
    result = weight_sensitivity(
        report, observations, event_horizon_guard=no_events, step=0.20
    )

    moved = sum(
        move.directions_changed + move.convictions_changed + move.largest_rank_move
        for move in result.moves
    )

    assert moved > 0


# --- criterion 4: reuse, and the unperturbed run ----------------------------


def test_the_unperturbed_weights_reproduce_the_report_exactly(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 4, and the assertion the whole measurement rests on.

    Every count is a difference against this run. If rescoring at the original
    weights does not land back on the report, the differences include whatever
    else the rescoring did, and no figure in the output means what it says.

    Exact rather than approximate: the same inputs through the same two
    functions on the same machine give the same floats, and a tolerance here
    would hide the reimplementation the criterion is written to prevent.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)

    assert result.baseline_matches_report
    assert [row.pair for row in result.baseline] == [row.pair for row in report.pairs]
    for rebuilt, original in zip(result.baseline, report.pairs, strict=True):
        assert rebuilt.spread == original.spread, rebuilt.pair
        assert rebuilt.direction is original.direction, rebuilt.pair
        assert rebuilt.conviction is original.conviction, rebuilt.pair


def test_a_report_the_observations_do_not_reproduce_is_refused(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """The other half of criterion 4: the mismatch is loud, not silent.

    Handing over last Tuesday's observations with today's report produces a
    complete set of counts, every one of them measuring the change of data
    rather than the change of weight. That is a wrong number that looks right,
    so it refuses instead.
    """
    thinned = tuple(row for row in observations if row.indicator != "cpi_yoy")

    with pytest.raises(ValueError, match="does not reproduce"):
        weight_sensitivity(report, thinned, event_horizon_guard=no_events)


# --- criterion 5: nothing configured is mutated -----------------------------


def test_the_configured_weights_are_not_mutated(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 5. A sensitivity run that wrote a weight back would do it blind.

    `ScoringConfig` is frozen, so the field cannot be rebound. The mapping it
    holds is an ordinary dict and can be written through, which is the hole
    this closes.
    """
    config = Config()
    before = dict(config.scoring.weights)

    weight_sensitivity(
        report, observations, config=config, event_horizon_guard=no_events
    )

    assert config.scoring.weights == before
    assert dict(ScoringConfig().weights) == before


def test_the_perturbed_vector_is_a_copy(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Mutating what comes back must not reach the configuration either.

    A result handing out the live mapping passes the test above and fails the
    first time a caller sorts or edits it.
    """
    config = Config()
    result = weight_sensitivity(
        report, observations, config=config, event_horizon_guard=no_events
    )

    for move in result.moves:
        assert move.weights is not config.scoring.weights

    # Written through the mapping the result handed out, not through a copy of
    # it. `dict(move.weights)[k] = v` builds a new dict and writes to that,
    # which is a no-op that cannot fail, and this assertion was that for its
    # first two revisions.
    live = cast("dict[PillarName, float]", result.moves[0].weights)
    live[PillarName.MONETARY] = 99.0

    assert config.scoring.weights[PillarName.MONETARY] == pytest.approx(0.30)
    assert ScoringConfig().weights[PillarName.MONETARY] == pytest.approx(0.30)
    assert result.moves[1].weights[PillarName.MONETARY] != pytest.approx(99.0)


# --- criterion 6: the run's own observations --------------------------------


def test_the_observations_are_read_rather_than_refetched(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 6. What is passed in is what is scored.

    The function has no fetch path, which is the point: a refetch would read
    revised macro series and fold a data change into a figure labelled as a
    weight change. Asserted by feeding it a different universe and seeing the
    answer follow the observations.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)
    assert result.baseline_matches_report

    # One currency moved, not all of them. Every pillar here normalises
    # cross-sectionally, so doubling the whole universe leaves every z-score
    # exactly where it was and the report still reproduces: a test built on a
    # uniform scaling would pass against a function that refetched everything.
    moved = tuple(
        Observation(
            indicator=row.indicator,
            currency=row.currency,
            value=row.value + 1.5 if row.currency == "CHF" else row.value,
            period=row.period,
            source=row.source,
            series_id=row.series_id,
            unit=row.unit,
            frequency=row.frequency,
            released_at=row.released_at,
            revision=row.revision,
        )
        for row in observations
    )

    with pytest.raises(ValueError, match="does not reproduce"):
        weight_sensitivity(report, moved, event_horizon_guard=no_events)


def test_the_module_imports_no_data_source() -> None:
    """The structural half of criterion 6, which no call can demonstrate.

    A function that fetches only under a branch no test reaches still fetches.
    `fbe.evaluation` importing nothing from `fbe.datasources` is what makes the
    absence of a refetch a property of the module rather than of the inputs
    this file happens to pass.
    """
    source = (Path(__file__).resolve().parents[1] / "src/fbe/evaluation.py").read_text()
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]

    # The import lines rather than the whole file: the module's prose names
    # `fbe.datasources.prices.PricesSource.spot` when explaining what an absent
    # fixing means, and a search of the text would read that as a dependency.
    for line in imports:
        assert "datasources" not in line, line
        assert "httpx" not in line, line


# --- criterion 7: what the figures do not say -------------------------------


def test_the_output_states_that_it_says_nothing_about_returns(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 7, carried on the result rather than left in a docstring.

    The figures will be quoted in a proposal to change a weight. A caveat that
    lives only in the source is one the person reading the numbers never sees.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)

    assert result.note == CROSS_SECTION_NOTE
    assert "one run" in CROSS_SECTION_NOTE
    # "say" rather than "says": the subject is the figures, which is the
    # wording the criterion on #287 uses.
    assert "say nothing about returns" in CROSS_SECTION_NOTE


def test_the_note_states_both_directions_of_the_wrong_conclusion() -> None:
    """Both halves, because either alone points at the opposite mistake.

    Only "a small move does not mean the pillar is useless" invites the reading
    that a large move means the pillar is right, and only the reverse invites
    dropping a pillar for scoring quietly. Triage asked for both on #287 and
    both are here.
    """
    assert "not thereby useless" in CROSS_SECTION_NOTE
    assert "not thereby right" in CROSS_SECTION_NOTE


def test_no_unmeasured_claim_anywhere_in_the_module() -> None:
    """The standing instruction in `CLAUDE.md`, applied to this file's own words.

    This is the module most likely to breach it: it produces numbers about the
    model's own behaviour, which read like results to anyone skimming.
    """
    source = (Path(__file__).resolve().parents[1] / "src/fbe/evaluation.py").read_text()
    added = source[source.index("CROSS_SECTION_NOTE = (") :].lower()

    # Bounded to what #287 added. The module's own opening docstring says it
    # "computes no hit rate, no average and no interval", which is the
    # standing instruction being honoured rather than breached, and a search
    # of the whole file reads the disclaimer as the claim.
    for claim in ("backtested", "proven", "win rate", "hit rate"):
        assert claim not in added, claim
    assert " edge" not in added


# --- the shared ordering ----------------------------------------------------


def test_the_rank_is_the_order_the_report_already_publishes(
    report: BiasReport,
) -> None:
    """The ranking is `fbe.report.ranked_pairs`, not a second one written here.

    A rank move measured on a different order from the one the reader sees is
    a number about a table nobody looks at. The sort is absolute spread, widest
    first, because on the signed spread every short pair falls below every long
    one and the widest disagreement lands in the middle.
    """
    ordered = ranked_pairs(report.pairs)

    assert [row.pair for row in ordered] == [
        row.pair for row in sorted(report.pairs, key=lambda r: (-abs(r.spread), r.pair))
    ]
    assert len(ordered) == len(report.pairs)


def test_the_result_carries_the_asof_of_the_run_it_measured(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """A figure with no date attached outlives the run it describes."""
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)

    assert result.asof == report.asof
    assert isinstance(result, WeightSensitivity)
    assert all(isinstance(move, WeightMove) for move in result.moves)


def test_a_pillar_that_scored_nothing_cannot_move_the_ranking(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """A weight on a pillar carrying no score moves no composite.

    Two pillars score nothing on this fixture. POSITIONING z-scores its own
    history and RISK needs a volatility window, and a single-period fixture
    gives neither, so `fbe.scoring.score_currencies` leaves both at weight 0.0
    and the composite renormalises around them. Moving a weight that is already
    contributing nothing scales the five live weights by one common factor,
    which the composite's own divisor cancels exactly, so every spread is
    unchanged and the order is untouched.

    **It does not follow that the counts are zero, and this is the trap.**
    Raising a dead pillar's weight lowers coverage, because coverage is the
    share of weight that scored. On this run coverage sits exactly at
    ``coverage_demotion``, so a rise in POSITIONING's weight takes every
    currency below it and demotes every pair by one band. Eleven pairs fall to
    NONE, which forces them to NEUTRAL, and the run reports eleven direction
    changes and fourteen conviction changes with a largest rank move of zero.

    That is correct and it is worth reading carefully: the pillar did not
    change any currency's score, it changed how much of the model had an
    opinion. The ranking is the half that cannot move, and it is the half
    asserted here.
    """
    scored = {
        name
        for currency in report.currencies
        for name, pillar in currency.pillars.items()
        if pillar.weight > 0.0
    }
    unscored = {pillar for pillar in PillarName if pillar not in scored}

    assert unscored, "the fixture now scores every pillar; this test is stale"

    for move in weight_sensitivity(
        report, observations, event_horizon_guard=no_events
    ).moves:
        if move.pillar in unscored:
            assert move.largest_rank_move == 0, move.pillar
            assert move.largest_rank_move_pair is None, move.pillar


def test_a_dead_pillar_still_moves_conviction_through_coverage(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """The other half, asserted so the zero above is not read as "no effect".

    A reader who takes ``largest_rank_move == 0`` for a dead pillar as "moving
    this weight does nothing" will be surprised by a run where eleven pairs
    went neutral. Coverage is the channel and it is not obvious from the
    output, so it is pinned here rather than left to be rediscovered.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)
    scored = {
        name
        for currency in report.currencies
        for name, pillar in currency.pillars.items()
        if pillar.weight > 0.0
    }
    upward_dead = [
        move for move in result.moves if move.pillar not in scored and move.step > 0
    ]

    assert upward_dead, "no upward move on a pillar that scored nothing"
    assert any(move.convictions_changed > 0 for move in upward_dead)


def test_the_two_counts_are_not_the_same_number(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 3 asks for three figures, so two of them must be able to differ.

    On a run where only NONE and LOW are reachable,
    `fbe.bias.build_pair_biases` forces NEUTRAL at NONE, so a conviction
    change is the same event as a direction change on every pair. The
    conviction count then carries nothing the direction count does not, and
    an implementation returning the direction count twice passes every other
    test here. The first version of this fixture did exactly that, because
    every currency sat below ``coverage_demotion`` and the blanket demotion
    put MEDIUM out of reach.

    This asserts the fixture can still tell them apart, so the conviction tests
    are measuring something.
    """
    result = weight_sensitivity(report, observations, event_horizon_guard=no_events)
    differing = [
        move
        for move in result.moves
        if move.directions_changed != move.convictions_changed
    ]

    assert differing, "no perturbation separates the two counts on this fixture"


# --- the wire, not the value ------------------------------------------------


def test_the_config_argument_is_read_rather_than_accepted(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """Criterion 5 covers mutation; this covers whether the argument is used.

    Passing a `Config` built from the packaged defaults proves nothing: it is
    equal to what the function falls back to when ``config`` is ``None``, so a
    function that discarded the argument entirely would pass. That is the
    standards' "test the wire, not the value", and it is the defect the
    function's own docstring warns about, because perturbing weights the run
    did not use measures a vector nobody scored with.

    The only honest way to exercise it is a run built under different weights,
    so one is built here and the argument is the only thing that can tell the
    function about it. Under the right config the report reproduces; under the
    default it cannot, which is the same refusal that catches the wrong
    observations.
    """
    from fbe.bias import build_pair_biases
    from fbe.pillars import default_pillars
    from fbe.scoring import score_currencies
    from fbe.types import BiasReport as Report

    packaged = Config()
    tilted = Config(
        risk=packaged.risk,
        scoring=ScoringConfig(
            weights={
                PillarName.MONETARY: 0.10,
                PillarName.INFLATION: 0.10,
                PillarName.GROWTH: 0.30,
                PillarName.EMPLOYMENT: 0.20,
                PillarName.EXTERNAL: 0.10,
                PillarName.POSITIONING: 0.10,
                PillarName.RISK: 0.10,
            }
        ),
        data=packaged.data,
        broker=packaged.broker,
    )
    scores = score_currencies(
        observations, default_pillars(tilted.scoring), tilted.scoring, report.asof
    )
    other = Report(
        asof=report.asof,
        generated_at=report.generated_at,
        currencies=tuple(scores),
        pairs=build_pair_biases(
            scores, tilted, report.asof, event_horizon_guard=no_events
        ),
        config_digest=tilted.digest(),
    )

    # The two runs must actually differ, or the refusal below would be about
    # nothing and would pass against a function that ignored the argument.
    assert [row.spread for row in other.pairs] != [row.spread for row in report.pairs]

    result = weight_sensitivity(
        other, observations, config=tilted, event_horizon_guard=no_events
    )
    seen = {move.pillar: move.weight_before for move in result.moves}

    for pillar, weight in tilted.scoring.weights.items():
        assert seen[pillar] == pytest.approx(weight), pillar

    with pytest.raises(ValueError, match="does not reproduce"):
        weight_sensitivity(other, observations, event_horizon_guard=no_events)


def test_a_report_rescored_without_its_guard_is_refused(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """The guard has to be the one the run used, and the mismatch is loud.

    Passing nothing is not neutral. `fbe.bias._events_within_24h` reads an
    absent guard as ``False`` for every currency and applies no cap, so a run
    built under a guard that found an event carries a cap on those pairs and
    rescoring it without the guard **lifts** that cap and raises their
    conviction. Reported rather than refused, every one of those lifts would be
    counted as something a weight did.

    Built here rather than taken from the fixture, because the committed report
    is scored as a backtest with no events and so cannot show the difference.
    """
    from fbe.bias import build_pair_biases
    from fbe.pillars import default_pillars
    from fbe.scoring import score_currencies
    from fbe.types import BiasReport as Report

    def jpy_event(currency: str, asof: date) -> bool:
        return currency == "JPY"

    config = Config()
    scores = score_currencies(
        observations, default_pillars(config.scoring), config.scoring, report.asof
    )
    capped = build_pair_biases(
        scores, config, report.asof, event_horizon_guard=jpy_event
    )
    guarded = Report(
        asof=report.asof,
        generated_at=report.generated_at,
        currencies=tuple(scores),
        pairs=capped,
        config_digest=config.digest(),
    )

    # The guard has to have changed something, or the refusal below would be
    # about nothing and would pass against a function that ignored the guard.
    assert [row.conviction for row in capped] != [
        row.conviction for row in report.pairs
    ]

    assert weight_sensitivity(
        guarded, observations, event_horizon_guard=jpy_event
    ).baseline_matches_report

    with pytest.raises(ValueError, match="does not reproduce"):
        weight_sensitivity(guarded, observations, event_horizon_guard=no_events)

    # The guard reaches the perturbed rescores too, not only the baseline.
    # Lowering a dead pillar's weight moves no composite and no coverage
    # threshold, so its counts are zero. If the perturbed rescores ran without
    # the guard while the baseline ran with it, the JPY cap would lift on all
    # seven JPY pairs and this row would report them as conviction changes.
    dead = {
        name
        for currency in guarded.currencies
        for name, pillar in currency.pillars.items()
        if pillar.weight == 0.0
    }
    quiet = [
        move
        for move in weight_sensitivity(
            guarded, observations, event_horizon_guard=jpy_event
        ).moves
        if move.pillar in dead and move.step < 0
    ]

    assert quiet, "no downward move on a pillar that scored nothing"
    for move in quiet:
        assert move.directions_changed == 0, move.pillar
        assert move.convictions_changed == 0, move.pillar


def test_a_step_of_zero_or_less_is_refused(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """The degenerate value of the parameter criterion 2 is about.

    A step of zero perturbs nothing, reproduces the baseline fourteen times and
    reports fourteen rows of zeroes. Every one of those zeroes is true and the
    whole output is worthless, which is the shape of answer this repository
    refuses rather than returns.
    """
    for step in (0.0, -0.05):
        with pytest.raises(ValueError, match="above zero"):
            weight_sensitivity(
                report, observations, event_horizon_guard=no_events, step=step
            )


def test_the_reproduction_check_looks_at_more_than_direction(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """How strict the baseline check is, which nothing else pins.

    Both negative cases elsewhere in this file move spreads by more than a
    whole band, so a check comparing direction alone would refuse them too and
    look sound. A guard that loose would admit a run whose spreads had drifted
    a tenth, which is enough to move a pair across a conviction boundary and
    therefore enough to corrupt every count.

    Asserted by reproducing the report through the same two functions with one
    spread nudged below the tolerance a direction-only check would allow, and
    requiring the refusal.
    """
    from fbe.types import BiasReport as Report

    nudged = tuple(
        replace(row, spread=row.spread + 0.05) if index == 0 else row
        for index, row in enumerate(report.pairs)
    )
    drifted = Report(
        asof=report.asof,
        generated_at=report.generated_at,
        currencies=report.currencies,
        pairs=nudged,
        config_digest=report.config_digest,
    )

    # The nudge is small enough to leave the direction and, on this pair, the
    # conviction alone. Only the spread comparison can catch it.
    assert nudged[0].direction is report.pairs[0].direction
    assert nudged[0].conviction is report.pairs[0].conviction

    with pytest.raises(ValueError, match="does not reproduce"):
        weight_sensitivity(drifted, observations, event_horizon_guard=no_events)


def test_the_ranking_tie_break_is_the_pair_name(report: BiasReport) -> None:
    """`ranked_pairs` promises a total order, and the fixture has no ties.

    Two pairs at the same absolute spread sort by `sorted`'s stability, which
    is input order, so the same two runs holding the same spreads in a
    different order print different tables. The fixture cannot show that
    because every absolute spread in it is distinct, so the tie is built here.
    """
    rows = ranked_pairs(report.pairs)
    widest, narrowest = rows[0], rows[-1]
    tied = (
        replace(narrowest, pair="ZZZZZZ", spread=widest.spread),
        widest,
        replace(narrowest, pair="AAAAAA", spread=-widest.spread),
    )

    assert [row.pair for row in ranked_pairs(tied)] == [
        "AAAAAA",
        "ZZZZZZ",
        widest.pair,
    ] or [row.pair for row in ranked_pairs(tied)] == sorted(row.pair for row in tied)
    assert len({abs(row.spread) for row in tied}) == 1


def test_a_run_holding_different_pairs_is_refused() -> None:
    """`_counts` compares two runs by name and refuses a set mismatch.

    Comparing a 27-pair run against a 28-pair one by name would drop the
    missing pair from every count silently and report less movement than
    happened, which is the quiet understatement this repository's prime
    directive is about.
    """
    from fbe.evaluation import _counts

    base = ranked_pairs(load_report(REPORT).pairs)

    with pytest.raises(KeyError, match="same pairs"):
        _counts(base, base[:-1])


def test_the_reported_pair_is_the_first_by_name_when_several_tie() -> None:
    """Which pair is named when more than one travelled the furthest.

    Without a tie-break the answer comes from dict iteration order, which is
    insertion order, which is the order the pairs arrived in. Two runs holding
    the same movement would then name different pairs, and a reader comparing
    two mornings would see a pair change that never happened.

    The fixture cannot show this, because on every perturbation of it exactly
    one pair travels the furthest. So the tie is built: four pairs each move
    one place, and the name is the only thing left to choose between them.
    """
    from fbe.evaluation import _counts

    rows = ranked_pairs(load_report(REPORT).pairs)[:4]
    spreads = [row.spread for row in rows]
    # Swap the top two and the next two, so all four move exactly one place.
    swapped = (
        replace(rows[0], spread=spreads[1]),
        replace(rows[1], spread=spreads[0]),
        replace(rows[2], spread=spreads[3]),
        replace(rows[3], spread=spreads[2]),
    )

    directions, convictions, largest, worst = _counts(rows, swapped)

    moved = {
        row.pair
        for row in rows
        if abs(
            [r.pair for r in ranked_pairs(rows)].index(row.pair)
            - [r.pair for r in ranked_pairs(swapped)].index(row.pair)
        )
        == largest
    }

    assert largest == 1
    assert len(moved) > 1, "the constructed tie has stopped being a tie"
    assert worst == min(moved)
