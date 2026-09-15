"""Tests for the component blend, `_normalise` and `missing_score`.

This is the stage where a pillar stops being several series and becomes one
number, and almost every way it can go wrong produces a plausible figure rather
than an exception. The properties pinned here are the ones whose failure would
be invisible in a report:

- A currency scored on half its components as though it held all of them.
- The freshness discount dropped, so a five-month-old GDP print counts the same
  as yesterday's retail sales.
- The re-standardisation divisor taken from the run instead of from history,
  which amplifies a pillar exactly when its own components disagree.
- The run-local mean left unsubtracted, which is invisible whenever coverage is
  full because the mean is zero by construction there.
- A missing currency coming back with a numeric zero instead of a ``None``,
  which the aggregator reads as evidence of neutrality rather than as absence.

Every expected figure below is computed by hand from the sub-weights and
freshness factors in the test that uses it, never by running the code and
recording what it said.

The divisor is pinned through `blend_sd_history` rather than left to the run
wherever the arithmetic is being asserted. `BasePillar.blend_divisor` takes the
median of the history once there are at least
``ScoringConfig.min_restandardisation_runs`` usable entries, so a history of
twenty identical values fixes the divisor at that value and takes the run's own
standard deviation out of the expected numbers.

Nothing here reaches the network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest

from fbe.config import ScoringConfig
from fbe.pillars.base import MIN_COMPONENT_WEIGHT, BasePillar
from fbe.types import Observation, PillarName

UNIVERSE: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY")
"""Four currencies, which is above `MIN_CROSS_SECTION` so nothing here is
refused for being a thin cross-section. The real universe is eight; the rule
under test does not count currencies."""

STEADY_HISTORY: tuple[float, ...] = (0.5,) * 20
"""Twenty runs whose blend had a standard deviation of 0.5, so `blend_divisor`
takes the rolling path and returns exactly 0.5. Twenty is
``ScoringConfig.min_restandardisation_runs``."""

UNIT_HISTORY: tuple[float, ...] = (1.0,) * 20
"""The same, fixing the divisor at 1.0 so a blend and its z-score differ only by
the run-local mean."""


class Double(BasePillar):
    """A pillar with no data layer, used to drive the blend directly.

    `_extract` and `_transform` are abstract on `BasePillar` and belong to the
    concrete pillars, so they raise here: every test in this file supplies
    component z-scores itself rather than going through a fetch. Sub-weights are
    passed to the constructor so one class covers the single-component case, the
    multi-component case and the degenerate one.
    """

    name = PillarName.GROWTH
    requires: Sequence[str] = ()

    def __init__(
        self,
        weights: Mapping[str, float],
        config: ScoringConfig | None = None,
        blend_sd_history: Sequence[float] | None = None,
    ) -> None:
        super().__init__(config, blend_sd_history)
        self._weights = dict(weights)

    @property
    def component_weights(self) -> Mapping[str, float]:
        return self._weights

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        raise AssertionError("the double has no data layer")

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        raise AssertionError("the double has no data layer")


def even_split() -> Double:
    """Two components at 0.6 and 0.4, with the divisor fixed at 0.5."""
    return Double({"alpha": 0.6, "beta": 0.4}, blend_sd_history=STEADY_HISTORY)


# --- the arithmetic ---------------------------------------------------------


def test_the_blend_is_the_sub_weighted_sum_divided_by_the_history_scale() -> None:
    """The headline formula, with every figure computed by hand.

    ``blend(c)`` is ``sum of u_j * z_j(c)`` and ``z_pillar(c)`` is
    ``(blend(c) - mean(blend)) / blend_divisor``. Coverage is full and the
    sub-weights sum to 1.0, so the mean is zero by construction here and the
    division is the whole of the transform.
    """
    result = even_split().blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
        }
    )

    # blends: 0.6, 0.4, -0.6, -0.4. Mean 0.0. Divisor 0.5 from the history.
    assert result == {
        "USD": pytest.approx(1.2),
        "EUR": pytest.approx(0.8),
        "GBP": pytest.approx(-1.2),
        "JPY": pytest.approx(-0.8),
    }


def test_the_divisor_comes_from_history_and_does_not_move_with_the_run() -> None:
    """The asymmetry `blend_divisor` exists to enforce.

    Doubling every component z-score doubles the blend and doubles the run's own
    standard deviation with it. A run-local divisor would cancel the two and
    return the same z-scores; the rolling divisor does not move, so the
    z-scores double. That is the difference the whole re-standardisation
    argument turns on, and nothing else in this file separates the two.
    """
    pillar = even_split()
    single = {
        "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.0},
        "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
    }
    doubled = {
        component: {currency: value * 2.0 for currency, value in values.items()}
        for component, values in single.items()
    }

    quiet = pillar.blend_components(single)
    loud = pillar.blend_components(doubled)

    assert loud["USD"] == pytest.approx(2.4)
    assert loud["USD"] == pytest.approx(quiet["USD"] * 2.0)


def test_a_run_local_divisor_is_used_only_when_history_is_too_short() -> None:
    """The fallback path, so the test above is not passing on an unused branch.

    With no history the divisor is the run's own standard deviation, and the
    blend of the same four currencies has a population standard deviation of
    ``sqrt(0.26)``. Under the rolling path the same inputs give 1.2 for USD.
    """
    pillar = Double({"alpha": 0.6, "beta": 0.4})

    result = pillar.blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
        }
    )

    # sd of (0.6, 0.4, -0.6, -0.4) with ddof=0 is sqrt(1.04 / 4) = 0.509901951.
    assert result["USD"] == pytest.approx(0.6 / 0.5099019513592785)
    assert result["USD"] != pytest.approx(1.2)


def test_the_worked_growth_figure_reproduces_to_its_published_precision() -> None:
    """The figure `blend_components` publishes in its own docstring.

    A GDP print at freshness 0.600 carrying sub-weight 0.30, against a retail
    sales print at 1.000 carrying 0.20, leaves GDP with 0.474 of the blend where
    the configured sub-weights alone would give it 0.600.

    Asserted as the gap between a currency whose GDP z is 1.0 and one whose GDP
    z is 0.0, with retail flat across the run. The run-local mean cancels in a
    difference, so the figure is the share itself and not the share plus
    whatever centring the rest of the cross-section produced.
    """
    pillar = Double(
        {"gdp_yoy": 0.30, "retail_sales_yoy": 0.20},
        blend_sd_history=UNIT_HISTORY,
    )
    fresh = {currency: 1.000 for currency in UNIVERSE}

    result = pillar.blend_components(
        {
            "gdp_yoy": {"USD": 1.0, "EUR": 0.0, "GBP": 0.0, "JPY": 0.0},
            "retail_sales_yoy": dict.fromkeys(UNIVERSE, 0.0),
        },
        component_freshness={
            "gdp_yoy": {currency: 0.600 for currency in UNIVERSE},
            "retail_sales_yoy": fresh,
        },
    )

    share = result["USD"] - result["EUR"]

    # 0.30 * 0.600 = 0.180 against retail's 0.200, so 0.180 / 0.380.
    assert share == pytest.approx(0.4736842105263158)
    assert round(share, 3) == 0.474
    assert share != pytest.approx(0.600)


def test_a_component_at_zero_freshness_contributes_nothing() -> None:
    """Past its allowance a component drops out, with no special case for it.

    Its discounted sub-weight is already zero, so it leaves both the numerator
    and the renormalisation denominator on its own. Asserted by moving the dead
    component's z-score a long way and showing nothing downstream moves.
    """
    pillar = even_split()
    freshness = {
        "alpha": dict.fromkeys(UNIVERSE, 0.0),
        "beta": dict.fromkeys(UNIVERSE, 1.0),
    }

    calm = pillar.blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
        },
        component_freshness=freshness,
    )
    wild = pillar.blend_components(
        {
            "alpha": {"USD": 99.0, "EUR": -99.0, "GBP": 99.0, "JPY": -99.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
        },
        component_freshness=freshness,
    )

    assert calm == wild
    # Beta alone, renormalised to 1.0: blends 0.0, 1.0, 0.0, -1.0, mean 0.0.
    assert calm["EUR"] == pytest.approx(2.0)


def test_the_run_local_mean_is_subtracted_when_renormalisation_makes_it_nonzero() -> (
    None
):
    """The case that tells a missing subtraction from a harmless one.

    With full coverage every component z has mean zero and the sub-weights sum
    to 1.0, so the blend's mean is zero and forgetting to subtract it changes
    nothing. It is only non-zero once one currency renormalises around an absent
    component, which is what this fixture does: JPY holds alpha alone, so its
    blend is its raw alpha z rather than a fraction of it.
    """
    result = even_split().blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 2.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": None},
        }
    )

    # blends: 0.6, 0.4, -0.6, 2.0. Mean 0.6. Divisor 0.5.
    assert result == {
        "USD": pytest.approx(0.0),
        "EUR": pytest.approx(-0.4),
        "GBP": pytest.approx(-2.4),
        "JPY": pytest.approx(2.8),
    }
    # Without the subtraction USD would read 1.2, which is the whole point.
    assert result["USD"] != pytest.approx(1.2)


def test_a_degenerate_divisor_scores_every_covered_currency_zero() -> None:
    """Below ``1e-9`` the scale is meaningless, so nobody gets a number from it.

    Every currency reporting the same blend is the same finding
    `cross_sectional_z` already treats as zero rather than as missing: the
    pillar sees no difference between them. A currency that had no blend to
    begin with stays ``None``, because a degenerate divisor cannot manufacture
    coverage.

    No history, deliberately. On the rolling path the divisor is 0.5 whatever
    the run does, so this branch would be unreachable and the assertion would
    pass on the ordinary arithmetic instead. The only way the divisor goes to
    zero is the run-local fallback over a cross-section that holds no spread,
    which is what `blend_divisor` filters a history of zeros down to as well.
    """
    result = Double({"alpha": 0.6, "beta": 0.4}).blend_components(
        {
            "alpha": {"USD": 2.0, "EUR": 2.0, "GBP": 2.0, "JPY": None},
            "beta": {"USD": 2.0, "EUR": 2.0, "GBP": 2.0, "JPY": None},
        }
    )

    assert result == {"USD": 0.0, "EUR": 0.0, "GBP": 0.0, "JPY": None}


# --- the floor --------------------------------------------------------------


def test_a_currency_holding_exactly_the_floor_is_absent_not_scored() -> None:
    """The comparison is ``<=`` and the boundary is where it bites.

    Two components at 0.50 each is EMPLOYMENT's shape, the pillar the rule was
    written for: a currency missing either one holds exactly 0.50, and under a
    strict ``<`` the floor could never fire for it at all.
    """
    pillar = Double({"alpha": 0.5, "beta": 0.5}, blend_sd_history=STEADY_HISTORY)

    result = pillar.blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.5},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": -0.5, "JPY": None},
        }
    )

    assert result["JPY"] is None
    assert result["USD"] is not None


def test_a_currency_just_above_the_floor_still_scores() -> None:
    """The converse, so the test above is not passing because everything is
    absent. 0.6 of the sub-weight clears 0.5, and one missing component out of
    two is a repair the renormalisation can make.
    """
    pillar = Double({"alpha": 0.6, "beta": 0.4}, blend_sd_history=STEADY_HISTORY)

    result = pillar.blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 2.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": None},
        }
    )

    assert result["JPY"] is not None


def test_the_floor_is_a_fraction_of_the_weight_the_pillar_declares() -> None:
    """Not of 1.0, which is the same number only while the weights sum to one.

    `blend_components` takes a ``weights`` override, and a caller that passes a
    subset is not thereby putting every currency below the floor. Both
    components here are present for every currency, so every currency holds all
    of the weight on offer and the ratio is 1.0, even though the sum is 0.5.
    """
    pillar = Double({"alpha": 0.3, "beta": 0.2}, blend_sd_history=STEADY_HISTORY)

    result = pillar.blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
        }
    )

    assert all(value is not None for value in result.values())


def test_the_floor_is_judged_before_the_freshness_discount() -> None:
    """Uniform staleness is not substitution, so it must not trip the floor.

    Every component present and every factor at 0.4: the currency holds all of
    the sub-weight, so nothing is being asked to speak for anything absent.
    Judging the floor on the discounted total instead would make the whole
    pillar vanish the moment a shared release crossed 0.5, which turns the
    section 4.1 ramp into the cliff it exists to avoid.
    """
    pillar = even_split()
    stale = {
        "alpha": dict.fromkeys(UNIVERSE, 0.4),
        "beta": dict.fromkeys(UNIVERSE, 0.4),
    }

    result = pillar.blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
        },
        component_freshness=stale,
    )

    assert all(value is not None for value in result.values())
    # A uniform factor cancels in the renormalisation, so the answer is the
    # fully fresh one: 0.6 and 0.4 rescaled by 0.4 / 0.4.
    assert result["USD"] == pytest.approx(1.2)


def test_a_currency_whose_every_component_has_expired_is_absent() -> None:
    """All present, all at zero freshness, so there is no weight left to blend.

    The floor passes, since absence is not what happened, and the currency still
    cannot be scored: dividing by a renormalisation denominator of zero is the
    one arithmetic the discount can produce. It comes back ``None`` rather than
    as a zero that reads like a neutral reading.
    """
    pillar = even_split()

    result = pillar.blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.5},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -0.5},
        },
        component_freshness={
            "alpha": {"JPY": 0.0},
            "beta": {"JPY": 0.0},
        },
    )

    assert result["JPY"] is None
    assert result["USD"] is not None


def test_a_currency_with_no_usable_component_never_comes_back_with_a_number() -> None:
    """No path substitutes or carries a value forward.

    The currency is present in every component mapping and usable in none, which
    is the shape a real outage takes: the key is there because the universe says
    so, and the reading behind it is not.
    """
    result = even_split().blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": None},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": None},
        }
    )

    assert result["JPY"] is None


def test_a_component_absent_from_the_weights_is_ignored_by_the_arithmetic() -> None:
    """How a report-only component reaches `headline_component` unweighted.

    A pillar may emit a component from `_transform` purely so the report can
    quote a number a human recognises. It has no sub-weight, so it must not
    enter the blend, must not count toward the sub-weight a currency holds, and
    must not appear in the result.
    """
    pillar = even_split()
    blended = {
        "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.0},
        "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
    }

    without = pillar.blend_components(blended)
    with_report_only = pillar.blend_components(
        {**blended, "headline": {"USD": 40.0, "EUR": -40.0, "GBP": 7.0, "JPY": 0.0}}
    )

    assert with_report_only == without
    assert set(with_report_only) == set(UNIVERSE)


def test_a_report_only_component_cannot_rescue_a_currency_below_the_floor() -> None:
    """The sharper half of the rule above, which the equality check can miss.

    A currency holding one of two equal components is absent. Handing it a third
    component that carries no sub-weight must not change that, and an
    implementation counting the keys it was given rather than the keys it has
    weights for would score JPY here.
    """
    pillar = Double({"alpha": 0.5, "beta": 0.5}, blend_sd_history=STEADY_HISTORY)

    result = pillar.blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.5},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": -0.5, "JPY": None},
            "headline": dict.fromkeys(UNIVERSE, 3.0),
        }
    )

    assert result["JPY"] is None


def test_a_report_only_component_does_not_put_a_currency_into_the_run() -> None:
    """Which currencies the pillar speaks about is decided by the weighted ones.

    A component with no sub-weight is ignored, and that has to include being
    ignored when the result's key set is worked out. Otherwise a pillar could
    carry a headline number for a currency none of its blended components has
    heard of, and the currency would appear in the output as an absence rather
    than not appearing at all: a coverage gap invented by a report-only field.

    Found by a mutation that collected currencies from every component handed
    in. It survived the test above, because there the report-only component
    carries exactly the same currencies as the weighted ones.
    """
    result = even_split().blend_components(
        {
            "alpha": {"USD": 1.0, "EUR": 0.0, "GBP": -1.0, "JPY": 0.0},
            "beta": {"USD": 0.0, "EUR": 1.0, "GBP": 0.0, "JPY": -1.0},
            "headline": {**dict.fromkeys(UNIVERSE, 3.0), "SEK": 3.0},
        }
    )

    assert "SEK" not in result
    assert set(result) == set(UNIVERSE)


# --- _normalise -------------------------------------------------------------


def test_normalise_blends_a_multi_component_pillar() -> None:
    """The wire from `_transform`'s shape into `blend_components`.

    `_transform` returns ``{currency: {component: value}}`` and
    `blend_components` takes ``{component: {currency: z}}``, so the transpose
    and the per-component z-scoring both live here. The fixture is built so the
    cross-sectional step is doing visible work: the raw alpha values are not
    already z-scores.
    """
    pillar = even_split()
    components: Mapping[str, Mapping[str, float | None]] = {
        "USD": {"alpha": 5.0, "beta": 1.0},
        "EUR": {"alpha": 3.0, "beta": 2.0},
        "GBP": {"alpha": 2.0, "beta": 3.0},
        "JPY": {"alpha": 1.0, "beta": 4.0},
    }

    expected = pillar.blend_components(
        {
            "alpha": BasePillar.cross_sectional_z(
                {c: components[c]["alpha"] for c in UNIVERSE}
            ),
            "beta": BasePillar.cross_sectional_z(
                {c: components[c]["beta"] for c in UNIVERSE}
            ),
        }
    )

    assert pillar._normalise(components) == expected


def test_normalise_falls_through_to_cross_sectional_z_for_one_component() -> None:
    """A single-component pillar is not re-standardised at all.

    POSITIONING and RISK carry one component at 1.0 each. Sending them through
    the blend would divide by `blend_divisor`, which is a correction for having
    several components and has nothing to correct here. The history is set to
    0.5 so the two answers differ by a factor of two and the test can tell them
    apart.
    """
    pillar = Double({"alpha": 1.0}, blend_sd_history=STEADY_HISTORY)
    components: Mapping[str, Mapping[str, float | None]] = {
        "USD": {"alpha": 5.0},
        "EUR": {"alpha": 3.0},
        "GBP": {"alpha": 2.0},
        "JPY": {"alpha": 1.0},
    }

    result = pillar._normalise(components)

    assert result == BasePillar.cross_sectional_z(
        {currency: components[currency]["alpha"] for currency in UNIVERSE}
    )
    # 5.0 against a mean of 2.75 and a population sd of sqrt(2.1875). Straight
    # through the blend the divisor of 0.5 would double it to 3.042555.
    assert result["USD"] == pytest.approx(1.52127765851133)


def test_normalise_ignores_a_component_carrying_no_sub_weight() -> None:
    """The report-only component again, reached through the real path.

    `_transform` emits it, so `_normalise` sees it, and it must not enter the
    blend. Asserted against the same currencies with the component removed.
    """
    pillar = even_split()
    core: dict[str, dict[str, float | None]] = {
        "USD": {"alpha": 5.0, "beta": 1.0},
        "EUR": {"alpha": 3.0, "beta": 2.0},
        "GBP": {"alpha": 2.0, "beta": 3.0},
        "JPY": {"alpha": 1.0, "beta": 4.0},
    }
    padded = {
        currency: {**values, "headline": 100.0 - index}
        for index, (currency, values) in enumerate(core.items())
    }

    assert pillar._normalise(padded) == pillar._normalise(core)


def test_normalise_refuses_a_pillar_that_declares_no_sub_weights() -> None:
    """The degenerate case, which has no honest answer.

    A pillar with an empty `component_weights` has not said what it is built
    from, so there is nothing to blend and nothing to fall through to. Returning
    ``None`` for every currency would report it as a data outage, which is a
    different fact and one a reader would act on differently, so it raises.
    """
    pillar = Double({})

    with pytest.raises(ValueError, match="component_weights"):
        pillar._normalise({"USD": {"alpha": 1.0}, "EUR": {"alpha": 2.0}})


# --- missing_score ----------------------------------------------------------


def test_missing_score_marks_absence_with_none_on_both_raw_and_z(
    asof: date,
) -> None:
    """The markers the aggregator detects absence through.

    A score of ``0.0`` is what the pillar hands the composite, and on its own it
    is indistinguishable from a currency the pillar genuinely reads as neutral.
    ``raw`` and ``z`` being ``None`` is the whole of the difference, so they are
    asserted specifically rather than through the dataclass as a whole.
    """
    pillar = even_split()

    score = pillar.missing_score("AUD", asof, notes="no cpi_yoy for AUD")

    assert score.raw is None
    assert score.z is None
    assert score.score == 0.0
    assert score.currency == "AUD"
    assert score.pillar is PillarName.GROWTH
    assert score.asof == asof


def test_missing_score_carries_the_pillars_configured_weight(asof: date) -> None:
    """The weight comes from config and is not assumed.

    An absent pillar still declares what it would have carried, because the
    scorer needs it to work out how much of the composite is missing. Asserted
    by moving the configured weight and showing the field follows, so the test
    cannot pass against a constant that happens to match the default.
    """
    weights = {name: 0.0 for name in PillarName}
    weights[PillarName.GROWTH] = 0.42
    pillar = Double({"alpha": 1.0}, config=ScoringConfig(weights=weights))

    assert pillar.missing_score("AUD", asof).weight == pytest.approx(0.42)
    assert even_split().missing_score("AUD", asof).weight == pytest.approx(
        ScoringConfig().weights[PillarName.GROWTH]
    )


def test_missing_score_ages_the_pillar_past_its_useful_life(asof: date) -> None:
    """The default marks the pillar as beyond the ramp rather than as fresh.

    ``max_staleness_days + 1`` is past the point where the freshness factor
    reaches zero, which is what makes a missing pillar carry no effective
    weight. A default of ``0`` would report it as the freshest thing in the run.
    """
    config = ScoringConfig()
    pillar = Double({"alpha": 1.0}, config=config)

    assert (
        pillar.missing_score("AUD", asof).staleness_days
        == config.max_staleness_days + 1
    )


def test_missing_score_reads_the_staleness_default_from_config(asof: date) -> None:
    """The wire, not the number. A hardcoded 46 passes the test above.

    `ScoringConfig.max_staleness_days` is 45 by default, so an implementation
    that writes 46 and never reads config is indistinguishable until the config
    moves.
    """
    config = ScoringConfig(max_staleness_days=90)
    pillar = Double({"alpha": 1.0}, config=config)

    assert pillar.missing_score("AUD", asof).staleness_days == 91


def test_missing_score_takes_an_explicit_staleness_override(asof: date) -> None:
    """A caller that knows the real age says so rather than accepting the mark."""
    pillar = even_split()

    assert pillar.missing_score("AUD", asof, staleness_days=3).staleness_days == 3


def test_missing_score_carries_the_reason_it_was_called(asof: date) -> None:
    """The notes reach the report unaltered.

    The caller names the missing indicator rather than saying data was thin, and
    this method must not paraphrase it or drop it: the note is the only place
    the report can say which series was absent.
    """
    pillar = even_split()

    score = pillar.missing_score("AUD", asof, notes="no cot_net_pct_oi for AUD")

    assert score.notes == "no cot_net_pct_oi for AUD"
    assert pillar.missing_score("AUD", asof).notes == ""


def test_missing_score_carries_no_inputs(asof: date) -> None:
    """A currency that could not be scored consumed nothing, so it shows nothing.

    An inputs sequence carrying observations here would put a reading in the
    report's working next to a score that was not computed from it.
    """
    assert tuple(even_split().missing_score("AUD", asof).inputs) == ()


# --- the constant itself ----------------------------------------------------


def test_the_floor_constant_is_the_one_the_comparison_uses() -> None:
    """Guards the fixtures above, which are built around 0.5 by hand.

    Two components at 0.5 each sit exactly on the floor only while the floor is
    0.5. If the constant moves, the boundary tests stop testing the boundary and
    would keep passing, so the premise is asserted rather than assumed.
    """
    assert MIN_COMPONENT_WEIGHT == 0.5
