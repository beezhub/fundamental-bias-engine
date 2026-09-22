"""Tests for the aggregation layer: pillar scores into one composite per currency.

This is where a missing pillar either becomes an honest coverage number or
quietly drags a currency toward neutral, so the properties worth testing are the
ones that would produce a plausible composite from a partial model: a divisor of
1.0 instead of the weight that counted, a staleness discount applied twice, a
dispersion taken about the wrong centre, and a pillar that raises taking the
whole run down with it.

Every expected figure is computed by hand rather than by mirroring the
implementation. The section 7.6 figures are pinned separately in
``tests/test_worked_example.py``; what is here is the arithmetic those figures
are an instance of.

Nothing here reaches the network and nothing here builds an `Observation` from
a real source.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date

import pytest

from fbe.config import ScoringConfig
from fbe.scoring import (
    apply_staleness_penalty,
    composite,
    coverage,
    dispersion,
    score_currencies,
)
from fbe.types import CurrencyScore, Observation, PillarName, PillarScore
from fbe.universe import G10

ASOF = date(2026, 6, 30)

ALL_PILLARS: tuple[PillarName, ...] = (
    PillarName.MONETARY,
    PillarName.INFLATION,
    PillarName.GROWTH,
    PillarName.EMPLOYMENT,
    PillarName.EXTERNAL,
    PillarName.POSITIONING,
    PillarName.RISK,
)


def _score(
    pillar: PillarName,
    score: float,
    weight: float,
    *,
    currency: str = "USD",
    staleness_days: int = 0,
) -> PillarScore:
    """Build one present pillar score.

    ``z`` mirrors ``score`` because absence is carried by ``z is None`` and
    nothing here is absent. Use `_absent` for the other case.
    """
    return PillarScore(
        pillar=pillar,
        currency=currency,
        raw=None,
        z=score,
        score=score,
        weight=weight,
        asof=ASOF,
        staleness_days=staleness_days,
    )


def _absent(pillar: PillarName, weight: float, currency: str = "USD") -> PillarScore:
    """A pillar that could not score this currency: ``z`` is ``None``."""
    return PillarScore(
        pillar=pillar,
        currency=currency,
        raw=None,
        z=None,
        score=0.0,
        weight=weight,
        asof=ASOF,
    )


def _full_set(
    scores: Mapping[PillarName, float], config: ScoringConfig
) -> dict[PillarName, PillarScore]:
    """Every pillar present at its configured weight, scoring as given."""
    return {
        pillar: _score(pillar, scores[pillar], config.weights[pillar])
        for pillar in ALL_PILLARS
    }


@pytest.fixture
def config() -> ScoringConfig:
    return ScoringConfig()


@pytest.fixture
def weights(config: ScoringConfig) -> Mapping[PillarName, float]:
    return config.weights


# --- criterion 1: the discount lands on the weight, never on the score ------


def test_the_penalty_scales_the_weight_by_the_freshness_factor(
    config: ScoringConfig,
) -> None:
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    penalised = apply_staleness_penalty(original, config, freshness_factor=0.4)

    assert penalised.weight == pytest.approx(0.12, abs=1e-12)


def test_the_penalty_leaves_the_score_untouched(config: ScoringConfig) -> None:
    """Discounting both the weight and the score applies the penalty twice."""
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    penalised = apply_staleness_penalty(original, config, freshness_factor=0.4)

    assert penalised.score == pytest.approx(1.5, abs=1e-12)


def test_the_penalty_does_not_mutate_its_input(config: ScoringConfig) -> None:
    """A report needs the original beside the penalised version to explain itself."""
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    apply_staleness_penalty(original, config, freshness_factor=0.4)

    assert original.weight == pytest.approx(0.30, abs=1e-12)


def test_a_full_freshness_factor_changes_nothing(config: ScoringConfig) -> None:
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    penalised = apply_staleness_penalty(original, config, freshness_factor=1.0)

    assert penalised.weight == pytest.approx(0.30, abs=1e-12)
    assert penalised.z == pytest.approx(1.5, abs=1e-12)


def test_an_expired_pillar_loses_its_z_as_well_as_its_weight(
    config: ScoringConfig,
) -> None:
    """Zero weight alone is not enough.

    The rest of the module detects absence through the ``None`` marker on ``z``,
    so a pillar discounted to nothing has to carry that marker or it stays
    visible to anything counting pillars rather than weight.
    """
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    penalised = apply_staleness_penalty(original, config, freshness_factor=0.0)

    assert penalised.weight == pytest.approx(0.0, abs=1e-12)
    assert penalised.z is None


def test_an_expired_pillar_says_why_in_its_notes(config: ScoringConfig) -> None:
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    penalised = apply_staleness_penalty(original, config, freshness_factor=0.0)

    assert penalised.notes
    assert penalised.notes != original.notes


def test_an_expired_pillar_keeps_any_note_it_already_carried(
    config: ScoringConfig,
) -> None:
    original = replace(_score(PillarName.MONETARY, 1.5, 0.30), notes="cpi_yoy missing")

    penalised = apply_staleness_penalty(original, config, freshness_factor=0.0)

    assert "cpi_yoy missing" in penalised.notes


def test_a_pillar_still_inside_its_allowance_keeps_its_z(
    config: ScoringConfig,
) -> None:
    """Only a factor of zero clears the marker, not any discount at all."""
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    penalised = apply_staleness_penalty(original, config, freshness_factor=0.01)

    assert penalised.z == pytest.approx(1.5, abs=1e-12)


def test_a_score_with_no_measured_factor_is_refused(
    config: ScoringConfig,
) -> None:
    """There used to be a fallback here: no factor meant "apply the ramp to
    the age". Since #126 the ramp is derived from the leg that produced each
    observation, and this module does not know the leg, so the fallback could
    only guess a shape. It guessed monthly, which is what gave every quarterly
    series a factor of 0.0. Refusing is the honest answer.
    """
    original = _score(PillarName.MONETARY, 1.5, 0.30, staleness_days=30)

    with pytest.raises(ValueError, match="no freshness factor"):
        apply_staleness_penalty(original, config)


@pytest.mark.parametrize("factor", [1.5, -0.1, 2.0])
def test_a_freshness_factor_outside_the_band_is_refused(
    config: ScoringConfig, factor: float
) -> None:
    """Above 1.0 a pillar would leave with more weight than it was given.

    Coverage would then exceed 1.0 and the composite would be divided by a
    number nobody configured, which is a real answer built on an impossible one.
    """
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    with pytest.raises(ValueError, match="freshness factor"):
        apply_staleness_penalty(original, config, freshness_factor=factor)


def test_the_ends_of_the_band_are_allowed(config: ScoringConfig) -> None:
    """The guard is on the range, not on the endpoints, which both mean something."""
    original = _score(PillarName.MONETARY, 1.5, 0.30)

    assert apply_staleness_penalty(
        original, config, freshness_factor=0.0
    ).weight == pytest.approx(0.0, abs=1e-12)
    assert apply_staleness_penalty(
        original, config, freshness_factor=1.0
    ).weight == pytest.approx(0.30, abs=1e-12)


# --- criteria 2 and 3: the composite, and its divisor -----------------------


def test_the_composite_is_the_effective_weighted_mean(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """Hand-computed: every pillar at 1.0 gives a composite of exactly 1.0."""
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 1.0), config)

    assert composite(scores, weights) == pytest.approx(1.0, abs=1e-12)


def test_a_dropped_pillar_is_renormalised_out_rather_than_shrinking_the_composite(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """The 0.30 monetary pillar drops. The other six all score 1.0.

    Renormalised, the composite is 0.70 / 0.70, which is 1.0. Dividing by 1.0
    instead would give 0.70, which reads as a currency drifting toward neutral
    for a reason that has nothing to do with its fundamentals.
    """
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 1.0), config)
    scores[PillarName.MONETARY] = _absent(PillarName.MONETARY, 0.0)

    assert composite(scores, weights) == pytest.approx(1.0, abs=1e-12)


def test_a_dropped_pillar_changes_the_composite_when_the_others_disagree(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """The guard on the test above, which passes for any divisor when all agree.

    MONETARY at 0.30 scores +2.0 and the other six score 0.0. With it the
    composite is 0.6 / 1.0, which is 0.6. Without it the six remaining score
    0.0 over a coverage of 0.70, which is 0.0.
    """
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 0.0), config)
    scores[PillarName.MONETARY] = _score(PillarName.MONETARY, 2.0, 0.30)
    assert composite(scores, weights) == pytest.approx(0.6, abs=1e-12)

    scores[PillarName.MONETARY] = _absent(PillarName.MONETARY, 0.0)

    assert composite(scores, weights) == pytest.approx(0.0, abs=1e-12)


def test_the_composite_is_hand_computable_on_a_mixed_set(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """0.30*1.0 + 0.15*2.0 + 0.15*(-1.0) + 0.10*0 + 0.10*0 + 0.10*0 + 0.10*(-2.0)

    which is 0.30 + 0.30 - 0.15 - 0.20 = 0.25, over a coverage of 1.0.
    """
    scores = _full_set(
        {
            PillarName.MONETARY: 1.0,
            PillarName.INFLATION: 2.0,
            PillarName.GROWTH: -1.0,
            PillarName.EMPLOYMENT: 0.0,
            PillarName.EXTERNAL: 0.0,
            PillarName.POSITIONING: 0.0,
            PillarName.RISK: -2.0,
        },
        config,
    )

    assert composite(scores, weights) == pytest.approx(0.25, abs=1e-12)


def test_a_composite_with_no_coverage_is_zero(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    scores = {pillar: _absent(pillar, 0.0) for pillar in ALL_PILLARS}

    assert composite(scores, weights) == pytest.approx(0.0, abs=1e-12)


def test_an_empty_set_of_pillars_composites_to_zero(
    weights: Mapping[PillarName, float],
) -> None:
    assert composite({}, weights) == pytest.approx(0.0, abs=1e-12)


def test_the_staleness_discount_is_not_applied_a_second_time(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """A half-weight pillar and a full-weight one scoring the same agree.

    If the composite discounted the score as well as the weight, halving one
    pillar's weight would move the composite even when every pillar says the
    same thing.
    """
    full = _full_set(dict.fromkeys(ALL_PILLARS, 1.5), config)
    halved = dict(full)
    halved[PillarName.MONETARY] = _score(PillarName.MONETARY, 1.5, 0.15)

    assert composite(halved, weights) == pytest.approx(
        composite(full, weights), abs=1e-12
    )


# --- criteria 4 and 5: coverage is a measurement, not a count ---------------


def test_full_coverage_is_one(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 0.5), config)

    assert coverage(scores, weights) == pytest.approx(1.0, abs=1e-12)


def test_a_six_pillar_run_reports_at_most_nine_tenths(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """A pillar absent from the run is a partial model and should say so."""
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 0.5), config)
    del scores[PillarName.MONETARY]

    assert coverage(scores, weights) == pytest.approx(0.70, abs=1e-12)


def test_a_pillar_with_no_z_contributes_nothing_to_coverage(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 0.5), config)
    scores[PillarName.INFLATION] = _absent(PillarName.INFLATION, 0.15)

    assert coverage(scores, weights) == pytest.approx(0.85, abs=1e-12)


def test_a_half_expired_pillar_contributes_half_its_weight(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """This is what makes coverage continuous rather than a headcount."""
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 0.5), config)
    scores[PillarName.MONETARY] = _score(PillarName.MONETARY, 0.5, 0.15)

    assert coverage(scores, weights) == pytest.approx(0.85, abs=1e-12)


def test_coverage_counts_weight_rather_than_pillars(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """Losing the one 0.30 pillar and losing three 0.10 pillars both cost 0.30.

    A headcount would make the second three times worse than the first.
    """
    lost_monetary = _full_set(dict.fromkeys(ALL_PILLARS, 0.5), config)
    lost_monetary[PillarName.MONETARY] = _absent(PillarName.MONETARY, 0.0)

    lost_three = _full_set(dict.fromkeys(ALL_PILLARS, 0.5), config)
    for pillar in (PillarName.EMPLOYMENT, PillarName.POSITIONING, PillarName.RISK):
        lost_three[pillar] = _absent(pillar, 0.0)

    assert coverage(lost_monetary, weights) == pytest.approx(
        coverage(lost_three, weights), abs=1e-12
    )


def test_coverage_with_nothing_usable_is_zero(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    scores = {pillar: _absent(pillar, 0.0) for pillar in ALL_PILLARS}

    assert coverage(scores, weights) == pytest.approx(0.0, abs=1e-12)


# --- criteria 6 and 7: dispersion about the composite -----------------------


def test_pillars_that_agree_have_no_dispersion(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 1.2), config)

    assert dispersion(scores, weights) == pytest.approx(0.0, abs=1e-12)


def test_dispersion_is_hand_computable(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """MONETARY at 0.30 scores +1.0, the other six score 0.0.

    The composite is 0.30. The weighted squared deviations are
    0.30*(0.7)^2 + 0.70*(0.3)^2, which is 0.147 + 0.063 = 0.21, and the square
    root of that is 0.4582575694955840.
    """
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 0.0), config)
    scores[PillarName.MONETARY] = _score(PillarName.MONETARY, 1.0, 0.30)

    assert dispersion(scores, weights) == pytest.approx(0.4582575694955840, abs=1e-12)


def test_dispersion_is_taken_about_the_composite_not_the_unweighted_mean(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """The two centres differ here, so the choice of centre is visible.

    MONETARY at 0.30 scores +1.0 and the six others score 0.0. The composite is
    0.30 and the unweighted mean of the seven scores is 1/7, about 0.142857.
    About the composite the answer is sqrt(0.21); about the unweighted mean it
    is sqrt(0.30*(0.857143)^2 + 0.70*(0.142857)^2), which is about 0.503953.
    """
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 0.0), config)
    scores[PillarName.MONETARY] = _score(PillarName.MONETARY, 1.0, 0.30)

    computed = dispersion(scores, weights)

    assert computed == pytest.approx(0.4582575694955840, abs=1e-12)
    assert computed != pytest.approx(0.5039526306789696, abs=1e-6)


def test_dispersion_is_weighted_rather_than_counted(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """The 0.30 pillar dissenting must register more than a 0.10 pillar doing so."""
    heavy = _full_set(dict.fromkeys(ALL_PILLARS, 0.0), config)
    heavy[PillarName.MONETARY] = _score(PillarName.MONETARY, 1.0, 0.30)

    light = _full_set(dict.fromkeys(ALL_PILLARS, 0.0), config)
    light[PillarName.RISK] = _score(PillarName.RISK, 1.0, 0.10)

    assert dispersion(heavy, weights) > dispersion(light, weights)


def test_dispersion_stays_on_the_band_whatever_the_coverage(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """``w_tilde`` sums to 1.0, so a thinly covered currency is not flattered.

    Two pillars at +1.0 and -1.0 with equal effective weight are one band unit
    from their own composite of 0.0, whether they are the whole run or half of
    a run whose other pillars dropped out.
    """
    thin = {
        PillarName.MONETARY: _score(PillarName.MONETARY, 1.0, 0.30),
        PillarName.INFLATION: _score(PillarName.INFLATION, -1.0, 0.30),
    }

    assert dispersion(thin, weights) == pytest.approx(1.0, abs=1e-12)


def test_one_usable_pillar_disperses_to_zero(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """A floor, not a finding. The coverage figure beside it is the real signal."""
    scores = {pillar: _absent(pillar, 0.0) for pillar in ALL_PILLARS}
    scores[PillarName.MONETARY] = _score(PillarName.MONETARY, 2.5, 0.30)

    assert dispersion(scores, weights) == pytest.approx(0.0, abs=1e-12)


def test_no_usable_pillar_disperses_to_zero(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    scores = {pillar: _absent(pillar, 0.0) for pillar in ALL_PILLARS}

    assert dispersion(scores, weights) == pytest.approx(0.0, abs=1e-12)


# --- the pillar doubles score_currencies runs against -----------------------


class _Double:
    """A pillar that returns whatever it was handed, per currency."""

    def __init__(
        self,
        name: PillarName,
        scores: Mapping[str, float],
        *,
        raises: Exception | None = None,
        absent: Sequence[str] = (),
        omit: Sequence[str] = (),
        staleness_days: int = 0,
        freshness_factor: float | None = 1.0,
    ) -> None:
        self.name = name
        self.requires: Sequence[str] = ()
        self._scores = scores
        self._raises = raises
        self._absent = set(absent)
        self._omit = set(omit)
        self._staleness_days = staleness_days
        # Since #126 a pillar measures its own freshness, because the ramp is
        # derived from the leg that produced each observation and the scorer
        # does not know which leg that was. 1.0 keeps the scores in these
        # tests at their configured weights, which is what they are about;
        # the tests that care about the discount pass their own.
        self._freshness_factor = freshness_factor

    def compute(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, PillarScore]:
        if self._raises is not None:
            raise self._raises
        built: dict[str, PillarScore] = {}
        for currency in currencies:
            if currency in self._omit:
                continue
            value = self._scores.get(currency, 0.0)
            built[currency] = PillarScore(
                pillar=self.name,
                currency=currency,
                raw=None,
                z=None if currency in self._absent else value,
                score=0.0 if currency in self._absent else value,
                weight=0.0,
                asof=asof,
                staleness_days=self._staleness_days,
                freshness_factor=self._freshness_factor,
            )
        return built


def _one_pillar_each(values: Mapping[str, float]) -> list[_Double]:
    """Seven doubles, every one scoring each currency the same."""
    return [_Double(pillar, values) for pillar in ALL_PILLARS]


UNIVERSE: Mapping[str, float] = {
    "USD": 1.5,
    "CHF": 1.0,
    "EUR": 0.5,
    "CAD": 0.0,
    "GBP": -0.5,
    "AUD": -1.0,
    "JPY": -1.5,
    "NZD": -2.0,
}
"""One distinct score per G10 currency, so a ranking cannot pass by accident."""

RANKED = ("USD", "CHF", "EUR", "CAD", "GBP", "AUD", "JPY", "NZD")


# --- criterion 8: the ranking ------------------------------------------------


def test_the_universe_is_ranked_strongest_first(config: ScoringConfig) -> None:
    ranked = score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)

    assert tuple(row.currency for row in ranked) == RANKED
    assert [row.rank for row in ranked] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_the_strongest_currency_is_rank_one(config: ScoringConfig) -> None:
    ranked = score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)

    strongest = max(ranked, key=lambda row: row.composite)
    assert strongest.rank == 1


def test_a_tie_is_broken_by_iso_code(config: ScoringConfig) -> None:
    """Two identical runs must produce byte-identical reports.

    NZD and AUD tie at the top and the other six tie at zero below them, so both
    ends of the ordering are under test rather than only the interesting one.
    """
    tied = {"NZD": 1.0, "AUD": 1.0}

    rows = score_currencies([], _one_pillar_each(tied), config, ASOF)
    ranked = [row.currency for row in rows]

    assert ranked[:2] == ["AUD", "NZD"]
    assert ranked[2:] == sorted(ranked[2:])


def test_every_currency_the_pillars_scored_comes_back(config: ScoringConfig) -> None:
    ranked = score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)

    assert {row.currency for row in ranked} == set(G10)


def test_a_currency_no_observation_mentions_is_still_scored(
    config: ScoringConfig,
) -> None:
    """The universe is the G10, not whichever currencies happened to arrive.

    Deriving it from the observations would also try to score ``GLOBAL``, which
    is a pseudo-currency for cross-market series such as the VIX and is not a
    leg of any pair.
    """
    ranked = score_currencies([], _one_pillar_each({"USD": 1.0}), config, ASOF)

    assert {row.currency for row in ranked} == set(G10)
    assert "GLOBAL" not in {row.currency for row in ranked}


def test_each_row_carries_its_composite_coverage_and_dispersion(
    config: ScoringConfig,
) -> None:
    ranked = score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)

    usd = next(row for row in ranked if row.currency == "USD")
    assert usd.composite == pytest.approx(1.5, abs=1e-12)
    assert usd.coverage == pytest.approx(1.0, abs=1e-12)
    assert usd.dispersion == pytest.approx(0.0, abs=1e-12)
    assert usd.asof == ASOF


# --- criterion 9: two pillars with the same name ----------------------------


def test_two_pillars_sharing_a_name_raise(config: ScoringConfig) -> None:
    """The second would silently overwrite the first in the weight map."""
    pillars = [
        _Double(PillarName.MONETARY, UNIVERSE),
        _Double(PillarName.MONETARY, UNIVERSE),
    ]

    with pytest.raises(ValueError, match="monetary"):
        score_currencies([], pillars, config, ASOF)


def test_the_duplicate_check_does_not_fire_on_a_normal_run(
    config: ScoringConfig,
) -> None:
    assert score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)


# --- criterion 10: a pillar that raises costs its own weight ----------------


def test_a_pillar_that_raises_does_not_end_the_run(config: ScoringConfig) -> None:
    pillars: list[_Double] = _one_pillar_each(UNIVERSE)
    pillars[0] = _Double(PillarName.MONETARY, UNIVERSE, raises=RuntimeError("boom"))

    ranked = score_currencies([], pillars, config, ASOF)

    assert {row.currency for row in ranked} == set(G10)


def test_a_pillar_that_raises_leaves_every_currency_a_neutral_score(
    config: ScoringConfig,
) -> None:
    pillars: list[_Double] = _one_pillar_each(UNIVERSE)
    pillars[0] = _Double(PillarName.MONETARY, UNIVERSE, raises=RuntimeError("boom"))

    ranked = score_currencies([], pillars, config, ASOF)

    for row in ranked:
        failed = row.pillars[PillarName.MONETARY]
        assert failed.z is None, row.currency
        assert failed.score == pytest.approx(0.0, abs=1e-12), row.currency


def test_a_pillar_that_raises_costs_exactly_its_own_weight(
    config: ScoringConfig,
) -> None:
    pillars: list[_Double] = _one_pillar_each(UNIVERSE)
    pillars[0] = _Double(PillarName.MONETARY, UNIVERSE, raises=RuntimeError("boom"))

    ranked = score_currencies([], pillars, config, ASOF)

    for row in ranked:
        assert row.coverage == pytest.approx(0.70, abs=1e-12), row.currency


def test_the_reason_names_the_pillar_and_what_happened(
    config: ScoringConfig,
) -> None:
    """It has to reach a reader, so it says which pillar and why, not "thin data"."""
    pillars: list[_Double] = _one_pillar_each(UNIVERSE)
    pillars[0] = _Double(
        PillarName.MONETARY, UNIVERSE, raises=RuntimeError("registry lookup failed")
    )

    ranked = score_currencies([], pillars, config, ASOF)

    notes = ranked[0].pillars[PillarName.MONETARY].notes
    assert "monetary" in notes.lower()
    assert "registry lookup failed" in notes


def test_the_surviving_pillars_still_produce_a_composite(
    config: ScoringConfig,
) -> None:
    """A partial model is more useful than no model, which is the whole rule."""
    pillars: list[_Double] = _one_pillar_each(UNIVERSE)
    pillars[0] = _Double(PillarName.MONETARY, UNIVERSE, raises=RuntimeError("boom"))

    ranked = score_currencies([], pillars, config, ASOF)

    usd = next(row for row in ranked if row.currency == "USD")
    assert usd.composite == pytest.approx(1.5, abs=1e-12)


def test_a_currency_a_pillar_cannot_score_loses_only_that_weight(
    config: ScoringConfig,
) -> None:
    """One currency absent from one pillar, not the whole pillar failing."""
    pillars: list[_Double] = _one_pillar_each(UNIVERSE)
    pillars[1] = _Double(PillarName.INFLATION, UNIVERSE, absent=["JPY"])

    ranked = score_currencies([], pillars, config, ASOF)

    by_currency = {row.currency: row for row in ranked}
    assert by_currency["JPY"].coverage == pytest.approx(0.85, abs=1e-12)
    assert by_currency["USD"].coverage == pytest.approx(1.0, abs=1e-12)


def test_a_pillar_that_omits_a_currency_still_leaves_a_marked_absence(
    config: ScoringConfig,
) -> None:
    """Dropping the key would lose the fact that the pillar was asked.

    Coverage comes out the same either way, because a pillar missing from the
    mapping contributes nothing and one marked absent contributes nothing. What
    differs is whether a reader can tell that the pillar ran and had nothing to
    say about this currency, or has to infer it from a gap.
    """
    pillars: list[_Double] = _one_pillar_each(UNIVERSE)
    pillars[1] = _Double(PillarName.INFLATION, UNIVERSE, omit=["JPY"])

    ranked = score_currencies([], pillars, config, ASOF)

    jpy = next(row for row in ranked if row.currency == "JPY")
    assert PillarName.INFLATION in jpy.pillars
    assert jpy.pillars[PillarName.INFLATION].z is None
    assert "JPY" in jpy.pillars[PillarName.INFLATION].notes
    assert jpy.coverage == pytest.approx(0.85, abs=1e-12)


def test_an_absent_pillar_carrying_a_score_does_not_reach_the_composite(
    config: ScoringConfig, weights: Mapping[PillarName, float]
) -> None:
    """``z is None`` is the test, not a weight or a score that happens to be zero.

    A pillar discounted to nothing by `apply_staleness_penalty` keeps the score
    it computed, and `_unscored` carries the configured weight, so neither the
    weight nor the score can be relied on to be zero. Only the marker can.
    """
    scores = _full_set(dict.fromkeys(ALL_PILLARS, 0.0), config)
    scores[PillarName.MONETARY] = replace(
        _score(PillarName.MONETARY, 3.0, 0.30), z=None
    )

    assert composite(scores, weights) == pytest.approx(0.0, abs=1e-12)
    assert coverage(scores, weights) == pytest.approx(0.70, abs=1e-12)


def test_a_score_filed_under_the_wrong_currency_is_refused(
    config: ScoringConfig,
) -> None:
    """The currency is stated twice and the two can disagree.

    Taking the mapping's key on trust would file one currency's reading under
    another's name, which inverts that currency's standing with nothing in the
    output to show for it.
    """

    class _Mislabelled(_Double):
        def compute(
            self,
            observations: Sequence[Observation],
            currencies: Sequence[str],
            asof: date,
        ) -> Mapping[str, PillarScore]:
            built = dict(super().compute(observations, currencies, asof))
            built["EUR"] = replace(built["EUR"], currency="USD")
            return built

    pillars: list[_Double] = _one_pillar_each(UNIVERSE)
    pillars[0] = _Mislabelled(PillarName.MONETARY, UNIVERSE)

    with pytest.raises(ValueError, match="under the key EUR"):
        score_currencies([], pillars, config, ASOF)


# --- criterion 11: the weights come from the config -------------------------


def test_the_composite_moves_when_a_configured_weight_moves(
    config: ScoringConfig,
) -> None:
    """Otherwise the suite could pass on a literal that equals a default."""
    values = {"USD": 0.0}
    pillars = [_Double(pillar, values) for pillar in ALL_PILLARS]
    pillars[0] = _Double(PillarName.MONETARY, {"USD": 2.0})

    at_default = score_currencies([], pillars, config, ASOF)[0].composite

    # Still sums to 1.0, so only the share MONETARY carries has changed.
    reweighted = replace(
        config,
        weights={
            PillarName.MONETARY: 0.60,
            PillarName.INFLATION: 0.05,
            PillarName.GROWTH: 0.05,
            PillarName.EMPLOYMENT: 0.05,
            PillarName.EXTERNAL: 0.05,
            PillarName.POSITIONING: 0.10,
            PillarName.RISK: 0.10,
        },
    )
    moved = score_currencies([], pillars, reweighted, ASOF)[0].composite

    assert at_default == pytest.approx(0.6, abs=1e-12)
    assert moved == pytest.approx(1.2, abs=1e-12)


def test_the_weight_on_each_score_is_the_configured_one(
    config: ScoringConfig,
) -> None:
    """The pillar doubles emit a weight of zero, so this cannot come from them."""
    ranked = score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)

    carried = ranked[0].pillars[PillarName.MONETARY].weight
    assert carried == pytest.approx(config.weights[PillarName.MONETARY], abs=1e-12)


def test_no_pillar_weight_appears_as_a_literal_in_the_module(
    config: ScoringConfig,
) -> None:
    """A weight typed twice is a weight that will disagree with itself.

    Checked on the parsed code with every docstring removed, since the prose in
    this module quotes the shipped weights on purpose and a plain text search
    would report those and prove nothing.
    """
    import ast
    import inspect

    import fbe.scoring

    tree = ast.parse(inspect.getsource(fbe.scoring))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = node.body[1:]

    configured = set(config.weights.values())
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, float)
        and node.value in configured
    ]
    assert offenders == []


# --- nothing is substituted for absent data ---------------------------------


def test_a_currency_with_no_usable_pillar_is_still_reported(
    config: ScoringConfig,
) -> None:
    """Dropping it would read as an absence of opportunity, not of data."""
    pillars = [_Double(pillar, UNIVERSE, absent=["JPY"]) for pillar in ALL_PILLARS]

    ranked = score_currencies([], pillars, config, ASOF)

    jpy = next(row for row in ranked if row.currency == "JPY")
    assert jpy.coverage == pytest.approx(0.0, abs=1e-12)
    assert jpy.composite == pytest.approx(0.0, abs=1e-12)


def test_a_zero_composite_from_no_coverage_is_not_a_neutral_reading(
    config: ScoringConfig,
) -> None:
    """The two are told apart by coverage, which is why it rides on every row."""
    absent = [_Double(pillar, UNIVERSE, absent=["JPY"]) for pillar in ALL_PILLARS]
    neutral = _one_pillar_each({**UNIVERSE, "JPY": 0.0})

    from_absence = next(
        row
        for row in score_currencies([], absent, config, ASOF)
        if row.currency == "JPY"
    )
    from_neutrality = next(
        row
        for row in score_currencies([], neutral, config, ASOF)
        if row.currency == "JPY"
    )

    assert from_absence.composite == pytest.approx(from_neutrality.composite, abs=1e-12)
    assert from_absence.coverage != pytest.approx(from_neutrality.coverage, abs=1e-6)


def test_the_run_is_deterministic(config: ScoringConfig) -> None:
    first = score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)
    second = score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)

    assert [(row.currency, row.composite, row.rank) for row in first] == [
        (row.currency, row.composite, row.rank) for row in second
    ]


def test_an_empty_pillar_sequence_scores_nobody(config: ScoringConfig) -> None:
    assert list(score_currencies([], [], config, ASOF)) == []


def test_the_scores_are_currency_scores(config: ScoringConfig) -> None:
    ranked = score_currencies([], _one_pillar_each(UNIVERSE), config, ASOF)

    assert all(isinstance(row, CurrencyScore) for row in ranked)


# --- the signature offers no control that does nothing (#190) ---------------


def test_the_penalty_takes_no_date_parameter() -> None:
    """A date argument here changed nothing, and the Args entry said it did.

    The promise was the defect rather than the arithmetic. A replay that loaded
    stored scores, passed today's date to age them and read the weights back
    would get the original run's weights returned unchanged, with coverage and
    the composite reading exactly as they did on the day. Nothing raised.

    Asserted on the signature rather than by calling, because the failure being
    guarded against is a parameter that exists and is ignored, which no call can
    distinguish from one that is absent.
    """
    parameters = inspect.signature(apply_staleness_penalty).parameters

    assert "asof" not in parameters, sorted(parameters)


def test_the_docstring_names_the_one_route_that_re_ages_a_score() -> None:
    """The replacement for the removed promise has to be findable where it was.

    A reader who reached for `asof` reached for it because the Args entry named
    it. There were two working routes and #126 removed one of them, so the
    guidance has to say which survives rather than leaving a reader to find
    out by passing ``None`` and reading the traceback.
    """
    doc = inspect.getdoc(apply_staleness_penalty) or ""

    assert "asof" not in doc

    # Asserting the name appears anywhere in the docstring passes whether or
    # not the replay guidance exists, because the Args entry names it already.
    # A mutation that deleted the guidance survived exactly that assertion, so
    # scope it to the paragraph that answers the question.
    guidance = doc.partition("Re-ageing a stored score")[2]

    assert guidance, doc
    assert "factor the replay wants" in guidance, guidance
    assert "takes no date" in " ".join(guidance.split()), guidance


def test_the_age_on_the_score_no_longer_re_ages_it(
    config: ScoringConfig,
) -> None:
    """The second route, removed by #126, and pinned so it cannot come back.

    Passing two scores that differ only in ``staleness_days`` used to give two
    different weights, because the age was run through a global ramp here.
    That ramp judged every series as if it published monthly, which is the
    defect. Now the age reaches the expiry note and nothing else, so the same
    measured factor gives the same weight whatever the age says.
    """
    fresh = _score(PillarName.MONETARY, 1.5, 0.30, staleness_days=20)
    stale = _score(PillarName.MONETARY, 1.5, 0.30, staleness_days=100)

    fresh_weight = apply_staleness_penalty(fresh, config, freshness_factor=0.5).weight
    stale_weight = apply_staleness_penalty(stale, config, freshness_factor=0.5).weight

    assert fresh_weight == stale_weight == pytest.approx(0.15, abs=1e-12)


def test_a_stored_score_is_re_aged_by_an_explicit_freshness_factor(
    config: ScoringConfig,
) -> None:
    """The second working route, and the one a replay should prefer.

    The age on the score is judged against the global ramp, which knows no
    per-indicator allowance. A replay that has the allowances passes the factor
    it wants instead, and that number reaches the weight unchanged.
    """
    original = _score(PillarName.MONETARY, 1.5, 0.30, staleness_days=20)

    assert apply_staleness_penalty(
        original, config, freshness_factor=0.25
    ).weight == pytest.approx(0.075, abs=1e-12)
    assert apply_staleness_penalty(
        original, config, freshness_factor=0.75
    ).weight == pytest.approx(0.225, abs=1e-12)
