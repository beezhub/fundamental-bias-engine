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
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
    result = weight_sensitivity(report, observations)

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
    result = weight_sensitivity(report, observations)

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
    result = weight_sensitivity(report, observations)
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
    small = weight_sensitivity(report, observations, step=0.01)
    large = weight_sensitivity(report, observations, step=0.20)

    assert small.step == pytest.approx(0.01)
    assert large.step == pytest.approx(0.20)

    by_pillar_small = {(m.pillar, m.step > 0): m.weight_after for m in small.moves}
    by_pillar_large = {(m.pillar, m.step > 0): m.weight_after for m in large.moves}

    # A step of 0.20 takes four of the seven weights below zero on the way
    # down, so those rows are absent from the larger run by design. Compared
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
    result = weight_sensitivity(report, observations, step=0.5)

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
    perturbed = _renormalised(config.scoring.weights, PillarName.MONETARY, 0.05)
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
    moved_pairs = build_pair_biases(scores, moved_config, report.asof)

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

    move = next(
        m
        for m in weight_sensitivity(report, observations).moves
        if m.pillar is PillarName.MONETARY and m.step > 0
    )

    assert move.directions_changed == directions
    assert move.convictions_changed == convictions
    assert move.largest_rank_move == largest


def test_the_counts_are_never_negative_and_never_exceed_the_universe(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """The bounds, which catch a count taken over the wrong collection.

    Twenty-eight pairs, so a direction count of 30 means the rows were compared
    against something other than the run they came from.
    """
    result = weight_sensitivity(report, observations)

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
    result = weight_sensitivity(report, observations, step=0.20)

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
    result = weight_sensitivity(report, observations)

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
        weight_sensitivity(report, thinned)


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

    weight_sensitivity(report, observations, config=config)

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
    result = weight_sensitivity(report, observations, config=config)

    for move in result.moves:
        assert move.weights is not config.scoring.weights

    dict(result.moves[0].weights)[PillarName.MONETARY] = 99.0
    assert config.scoring.weights[PillarName.MONETARY] == pytest.approx(0.30)


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
    result = weight_sensitivity(report, observations)
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
        weight_sensitivity(report, moved)


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
    result = weight_sensitivity(report, observations)

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
    result = weight_sensitivity(report, observations)

    assert result.asof == report.asof
    assert isinstance(result, WeightSensitivity)
    assert all(isinstance(move, WeightMove) for move in result.moves)


def test_a_pillar_that_scored_nothing_reports_zero_rather_than_a_guess(
    report: BiasReport, observations: Sequence[Observation]
) -> None:
    """A weight on a pillar carrying no score cannot move the ranking.

    Three pillars score nothing on this fixture: EMPLOYMENT, POSITIONING and
    RISK each need a history their inputs do not supply, so
    `fbe.scoring.score_currencies` gives them weight 0.0 and the composite
    renormalises around them. Moving a weight that is already contributing
    nothing changes no composite, and the honest report of that is zero.

    This is asserted rather than left to be noticed because it is the reading
    that misleads: a reader scanning the output sees three pillars the ranking
    does not rest on and concludes the model would be no worse without them.
    What it says is that this run had no data for them. `CROSS_SECTION_NOTE`
    carries the general form of that warning and this test pins the specific
    case the fixture actually produces.
    """
    scored = {
        name
        for currency in report.currencies
        for name, pillar in currency.pillars.items()
        if pillar.weight > 0.0
    }
    unscored = {pillar.value for pillar in PillarName} - scored

    assert unscored, "the fixture now scores every pillar; this test is stale"

    for move in weight_sensitivity(report, observations).moves:
        if move.pillar.value in unscored:
            assert move.directions_changed == 0, move.pillar
            assert move.convictions_changed == 0, move.pillar
            assert move.largest_rank_move == 0, move.pillar
            assert move.largest_rank_move_pair is None, move.pillar
