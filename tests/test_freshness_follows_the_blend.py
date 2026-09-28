"""Freshness is weighed over what blended, not over what had observations: #172.

Two functions were deriving "which components are present" independently.
`component_freshness` counts a component in when **any** of its indicators has
observations; `blend_components` counts it in when it **produced a z**. Those
part company for a component built from more than one indicator, and MONETARY's
`real_policy_rate` names two.

With `cpi_yoy` absent the component cannot be transformed, never enters the
blend and moves no score. `component_freshness` still reported it, at the
`policy_rate` half's freshness, and `pillar_freshness` averaged it in at its
full sub-weight. The pillar then carried more weight into the composite than its
data justifies, and pillar weight feeds coverage, coverage caps conviction and
conviction gates size.

The asymmetry is the part that cannot be defended either way: a `cpi_yoy` that
is present but expired drags the mean down, while a `cpi_yoy` that is missing
entirely does not, so the better-documented run is penalised and the blind one
is not.

The ruling on the issue is option 2: the blend says which components contributed
and the weight is computed over that set, rather than two functions deriving the
same fact twice. So the tests here mostly assert agreement between the two
rather than a number in isolation, and the all-pillars test exists because the
two pillars that bypass the blend have to state their set rather than inherit
one.

Nothing here reaches the network.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

import pytest

from fbe.config import ScoringConfig
from fbe.datasources.registry import (
    CYCLE_DAYS,
    GLOBAL,
    INDICATORS,
    full_weight_age,
    publication_lag,
)
from fbe.pillars import default_pillars
from fbe.pillars.base import MIN_COMPONENT_WEIGHT, MIN_CROSS_SECTION, BasePillar
from fbe.pillars.employment import EmploymentPillar
from fbe.pillars.inflation import InflationPillar
from fbe.pillars.monetary import MonetaryPillar
from fbe.pillars.positioning import PositioningPillar
from fbe.pillars.risk import RiskPillar
from fbe.types import Frequency, Observation, PillarName, PillarScore
from fbe.universe import G10

ASOF = date(2026, 9, 10)
"""The run date from the issue."""

HISTORY = 60
"""Periods of history per leg in the all-pillars fixture."""

MONETARY_AGES = {
    "policy_rate": 5,
    "yield_2y": 7,
    "yield_2y_chg_1m": 7,
    "yield_2y_chg_3m": 7,
}
"""Ages that give the issue's shape under the ramp as it stands.

Every MONETARY leg is daily with a full-weight age of 5 days and an allowance of
9, so `fbe.scoring.freshness` gives 1.0 at 5 days and ``(9 - 7) / (9 - 5)`` at
7. A fresh policy rate beside discounted yields is what makes the defect
visible: including `real_policy_rate` at the policy rate's own factor pulls the
mean up.

The issue quotes 1.0 and 0.45, which were that shape under the ramp #126 and
#222 replaced. The arithmetic below is recomputed against the current one rather
than copied, which is the point of the exercise.
"""

FRESH = 0.15 * 1.0 + 0.70 * 0.5
"""Sub-weight times factor over the components that blend: 0.15 + 0.35."""

BLENDED_WEIGHT = 0.85
"""Sub-weight of those four components. ``real_policy_rate``'s 0.15 is the rest."""

EXPECTED_FRESHNESS = FRESH / BLENDED_WEIGHT
"""0.588235..., the sub-weighted mean over what actually blended."""

REPORTED_BEFORE = FRESH + 0.15 * 1.0
"""0.65, the mean the defect reported: ``real_policy_rate`` at the fresh half's
factor, over the whole 1.0 of sub-weight."""


def observation(
    indicator: str,
    currency: str,
    value: float,
    age_days: int,
    frequency: Frequency = Frequency.DAILY,
) -> Observation:
    """One observation, aged ``age_days`` before the run."""
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=ASOF - timedelta(days=age_days),
        source="fred",
        series_id=indicator,
        unit="percent",
        frequency=frequency,
    )


def monetary_run(
    cpi_age: int | None = None, cpi_currencies: int = len(G10)
) -> list[Observation]:
    """The issue's run: every currency with rates, and `cpi_yoy` as asked for.

    Args:
        cpi_age: Age of the CPI print in days, or ``None`` for no CPI at all,
            which is the reported case.
        cpi_currencies: How many currencies get one, for the cross-section
            floor case.
    """
    rows = [
        observation(name, code, 1.0 + 0.25 * index + 0.1 * offset, age)
        for index, code in enumerate(G10)
        for offset, (name, age) in enumerate(MONETARY_AGES.items())
    ]
    if cpi_age is not None:
        rows += [
            observation("cpi_yoy", code, 2.0 + 0.1 * index, cpi_age, Frequency.MONTHLY)
            for index, code in enumerate(list(G10)[:cpi_currencies])
        ]
    return rows


def scored(observations: list[Observation]) -> dict[str, PillarScore]:
    return dict(MonetaryPillar().compute(observations, list(G10), ASOF))


def factors(score: PillarScore) -> dict[str, float]:
    """The per-component freshness measurements the score carries."""
    return {
        key.removeprefix("freshness."): value
        for key, value in score.diagnostics.items()
        if key.startswith("freshness.")
    }


# --- the reproduction from the issue ----------------------------------------


def test_a_half_absent_component_no_longer_carries_weight() -> None:
    """The issue's case, recomputed against the ramp as it stands.

    ``real_policy_rate`` cannot be transformed without a CPI print, so it never
    blends and moves no score. Weighing it in at the policy rate's own freshness
    hands MONETARY more weight than its data justifies, on a currency the model
    can see less of than it thinks.
    """
    score = scored(monetary_run())["USD"]

    assert score.freshness_factor == pytest.approx(EXPECTED_FRESHNESS)
    assert score.freshness_factor != pytest.approx(REPORTED_BEFORE)


def test_the_effective_weight_is_the_one_the_composite_should_read() -> None:
    """The same figure where it lands: pillar weight times the factor.

    `scoring.score_currencies` multiplies the configured weight by this, so the
    0.0173 between the two readings is real weight in the composite.
    """
    score = scored(monetary_run())["USD"]
    weight = ScoringConfig().weights[score.pillar]

    assert weight * score.freshness_factor == pytest.approx(0.30 * EXPECTED_FRESHNESS)
    assert weight * REPORTED_BEFORE == pytest.approx(0.195)


def test_an_absent_cpi_gives_no_more_weight_than_an_expired_one() -> None:
    """The asymmetry the issue calls indefensible either way.

    A 400-day-old CPI puts ``real_policy_rate`` in the blend at a freshness of
    zero, which is no contribution at all, and an absent CPI keeps it out
    entirely. Both are "this component moved nothing", so both must weigh the
    same.
    """
    absent = scored(monetary_run())["USD"]
    expired = scored(monetary_run(cpi_age=400))["USD"]

    assert expired.freshness_factor == pytest.approx(absent.freshness_factor)
    assert absent.freshness_factor == pytest.approx(EXPECTED_FRESHNESS)


def test_a_fresh_cpi_does_carry_its_component() -> None:
    """The fix is not a ban on the component, only on counting it unearned.

    60 days rather than a smaller number because `BasePillar._visible` dates an
    unstamped monthly print by its period plus a 45-day publication lag, so a
    20-day-old CPI has not been published yet and the component would be absent
    for the reason this test is not about.
    """
    score = scored(monetary_run(cpi_age=60))["USD"]

    assert factors(score)["real_policy_rate"] > 0.0
    assert score.freshness_factor == pytest.approx(
        FRESH + 0.15 * factors(score)["real_policy_rate"]
    )


def test_the_measurement_of_what_was_there_is_unchanged() -> None:
    """`component_freshness` still reports the half that is present.

    The ruling left it alone deliberately: the per-component factors are a true
    measurement of the observations, and only their use in the weight was wrong.
    A reader looking at diagnostics still sees that a policy rate was fresh.
    """
    score = scored(monetary_run())["USD"]

    assert "real_policy_rate" in factors(score)
    assert factors(score)["real_policy_rate"] == pytest.approx(1.0)


# --- the two definitions agree ----------------------------------------------


def test_the_blend_says_which_components_it_used() -> None:
    """Criterion 1: one authoritative set rather than two derivations."""
    pillar = MonetaryPillar()
    pillar.compute(monetary_run(), list(G10), ASOF)

    assert pillar.last_contributing["USD"] == frozenset(MONETARY_AGES)


def test_an_expired_component_blends_but_carries_no_weight() -> None:
    """The two questions the fix keeps apart, on one component.

    A 400-day-old CPI still produces a real policy rate, so the component is in
    the blend and `last_contributing` says so: it moved the score. It has also
    missed a whole release cycle, which `fbe.scoring.freshness` defines as no
    longer counting toward coverage, so it is out of the weight. Keeping its
    sub-weight in the denominator at a factor of zero would charge it twice and
    make an expired component cost more than an absent one.
    """
    pillar = MonetaryPillar()
    score = dict(pillar.compute(monetary_run(cpi_age=400), list(G10), ASOF))["USD"]

    assert "real_policy_rate" in pillar.last_contributing["USD"]
    assert factors(score)["real_policy_rate"] == pytest.approx(0.0)
    assert score.freshness_factor == pytest.approx(EXPECTED_FRESHNESS)


def test_a_component_below_the_cross_section_floor_contributes_nothing() -> None:
    """The second instance the ruling names, which option 1 would have missed.

    `cross_sectional_z` refuses a component whose cross-section is below
    `MIN_CROSS_SECTION` and returns ``None`` for every currency, so
    ``real_policy_rate`` blends nowhere. The two currencies that do have a CPI
    print still have observations for it, so the old rule counted its freshness
    for them and the pillar took weight for a component that moved no score
    anywhere.
    """
    assert MIN_CROSS_SECTION == 3
    pillar = MonetaryPillar()
    scores = dict(
        pillar.compute(monetary_run(cpi_age=20, cpi_currencies=2), list(G10), ASOF)
    )
    with_cpi = list(G10)[0]

    assert "real_policy_rate" in factors(scores[with_cpi])
    assert "real_policy_rate" not in pillar.last_contributing[with_cpi]
    assert scores[with_cpi].freshness_factor == pytest.approx(EXPECTED_FRESHNESS)


def test_a_wholly_absent_component_is_unchanged() -> None:
    """Criterion 5. Absence is still handled once, by the floor and the
    renormalisation, and is not charged again as staleness."""
    rows = [row for row in monetary_run() if row.indicator != "yield_2y_chg_3m"]
    pillar = MonetaryPillar()
    score = dict(pillar.compute(rows, list(G10), ASOF))["USD"]

    assert "yield_2y_chg_3m" not in factors(score)
    assert "yield_2y_chg_3m" not in pillar.last_contributing["USD"]
    assert score.freshness_factor == pytest.approx((0.15 * 1.0 + 0.45 * 0.5) / 0.60)


def test_a_pillar_left_holding_too_little_fresh_weight_takes_none() -> None:
    """The other side of the asymmetry, which the exclusion above opens.

    Lifting an expired component out of both sides of the mean is what makes an
    expired `cpi_yoy` cost the same as an absent one. Taken alone it also lets a
    pillar renormalise onto whatever is left: `policy_rate` fresh with all four
    other components expired would report 1.0 and take MONETARY's whole 0.30
    into the composite, on a score blended 0.85 from data the registry has
    stopped counting. The same pillar with those four absent instead falls under
    `MIN_COMPONENT_WEIGHT` in `blend_components` and does not speak at all, so
    the same constant has to decide both or the fix trades one asymmetry for a
    larger one.
    """
    pillar = MonetaryPillar()
    expired = {name: 100 for name in MONETARY_AGES} | {"policy_rate": 5}
    rows = [
        observation(name, code, 1.0 + 0.25 * index + 0.1 * offset, age)
        for index, code in enumerate(G10)
        for offset, (name, age) in enumerate(expired.items())
    ]

    score = dict(pillar.compute(rows, list(G10), ASOF))["USD"]
    measured = factors(score)

    assert measured["policy_rate"] == pytest.approx(1.0)
    assert all(
        measured[name] == pytest.approx(0.0)
        for name in MONETARY_AGES
        if name != "policy_rate"
    )
    assert pillar.last_contributing["USD"] == frozenset(MONETARY_AGES)
    assert score.freshness_factor == 0.0


class _UnnormalisedPillar(BasePillar):
    """A pillar whose sub-weights declare their own scale rather than summing to 1.0.

    `BasePillar.blend_components` already judges its floor as a fraction of what
    the weight map declares, for the stated reason that passing a subset must not
    put every currency under it. `pillar_freshness` has to read the same map the
    same way. A denominator hardcoded at 1.0 agrees with every pillar shipped
    today and disagrees with the next one written, which is the config-drift
    failure in miniature: the same quantity derived in one place and assumed in
    another.

    It borrows `PillarName.INFLATION` and that pillar's two indicators because a
    name and real legs are required, and inventing either would mean editing
    `types.py` for a fixture.
    """

    name = PillarName.INFLATION
    requires: Sequence[str] = ("cpi_yoy", "core_cpi_yoy")
    component_indicators: Mapping[str, tuple[str, ...]] = {
        "alpha": ("cpi_yoy",),
        "beta": ("core_cpi_yoy",),
    }

    @property
    def component_weights(self) -> Mapping[str, float]:
        return {"alpha": 1.0, "beta": 1.0}

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Unused. This pillar exists to be asked one question about weights."""
        raise NotImplementedError(
            "fbe.pillars.base._UnnormalisedPillar._transform is a test fixture"
        )


def _leg(indicator: str, age_days: int) -> list[Observation]:
    """One currency's slice for ``indicator``, aged as asked."""
    ref = INDICATORS[indicator].series["USD"]
    return [observation(indicator, "USD", 2.0, age_days, ref.frequency)]


def test_the_floor_reads_the_scale_the_weights_declare() -> None:
    """Half of a map summing to 2.0 is half, not twice the floor.

    `alpha` is fresh and `beta` has missed a cycle, so the counted share is 1.0
    of a declared 2.0. That is exactly `MIN_COMPONENT_WEIGHT` and the pillar
    takes no weight. Read against a denominator of 1.0 the same run would come
    out at a share of 1.0 and report full freshness.
    """
    pillar = _UnnormalisedPillar()
    extracted = {
        "cpi_yoy": _leg("cpi_yoy", 30),
        "core_cpi_yoy": _leg("core_cpi_yoy", 900),
    }

    assert sum(pillar.component_weights.values()) == 2.0
    assert pillar.component_freshness(extracted, ASOF)["beta"] == pytest.approx(0.0)
    assert pillar.pillar_freshness(extracted, ASOF, ("alpha", "beta")) == 0.0


def test_the_floor_is_reached_at_the_boundary_not_past_it() -> None:
    """EMPLOYMENT's two components are 0.50 each, so the boundary is live.

    `blend_components` refuses at ``<=`` rather than ``<``, and this is the same
    constant deciding the same question one stage later. A pillar left standing
    on exactly half its sub-weight is a coin toss reported as a reading, and the
    two floors disagreeing about the boundary is how one run would be refused by
    the blend and admitted by the weight.
    """
    pillar = EmploymentPillar()
    assert pillar.component_weights == {"unemployment_6m": 0.5, "employment_trend": 0.5}
    extracted = {
        "unemployment_rate": _leg("unemployment_rate", 30),
        "employment_chg": _leg("employment_chg", 900),
        "employment_level": _leg("employment_level", 900),
    }

    measured = pillar.component_freshness(extracted, ASOF)
    assert measured["unemployment_6m"] > 0.0
    assert measured["employment_trend"] == pytest.approx(0.0)
    assert (
        pillar.pillar_freshness(extracted, ASOF, tuple(pillar.component_weights)) == 0.0
    )


def test_the_floor_is_a_fraction_of_the_declared_sub_weight() -> None:
    """`MIN_COMPONENT_WEIGHT` is a fraction, not a number of components.

    INFLATION's two components are 0.40 and 0.60, so core alone clears the floor
    and headline alone does not. A floor counted in components rather than in
    sub-weight would treat the two the same, and a pillar carrying 0.40 of its
    weight would speak at full strength.
    """
    pillar = InflationPillar()
    fresh = {
        indicator: [
            observation(
                indicator,
                "USD",
                2.0,
                publication_lag(
                    INDICATORS[indicator].series["USD"],
                    INDICATORS[indicator].series["USD"].frequency,
                ),
                INDICATORS[indicator].series["USD"].frequency,
            )
        ]
        for indicator in ("cpi_yoy", "core_cpi_yoy")
    }

    assert pillar.component_weights == {"cpi_gap": 0.40, "core_gap": 0.60}
    assert pillar.pillar_freshness(fresh, ASOF, ("core_gap",)) == pytest.approx(1.0)
    assert pillar.pillar_freshness(fresh, ASOF, ("cpi_gap",)) == 0.0


# --- what the set says for a currency the pillar could not score ------------
#
# In each case below the currency ends with no z, so `compute` takes the absent
# branch and `pillar_freshness` is never reached. The set still has to be empty
# rather than left holding the components that were tried. It is the record of
# what moved a score, three consumers already read it through
# `last_contributing.get(...)`, and a set that outlives the score it describes
# is the kind of plausible wrong answer this repository treats as worse than a
# raise.


def _spread(component: str, currencies: list[str]) -> dict[str, float | None]:
    """One component's z-scores, distinct so no cross-section is degenerate."""
    return {code: 0.5 * index - 1.0 for index, code in enumerate(currencies)}


def test_a_currency_below_the_component_floor_contributes_nothing() -> None:
    """Holding too little sub-weight is not the same as holding it staled.

    USD carries `policy_rate` alone, which is 0.15 of the declared sub-weight
    and under `MIN_COMPONENT_WEIGHT`, so `blend_components` returns ``None`` for
    it while the other seven blend normally.
    """
    assert MIN_COMPONENT_WEIGHT == 0.5
    pillar = MonetaryPillar()
    codes = list(G10)
    component_z: dict[str, dict[str, float | None]] = {
        component: _spread(component, codes) for component in pillar.component_weights
    }
    for component in component_z:
        if component != "policy_rate":
            component_z[component]["USD"] = None

    blends = pillar.blend_components(component_z)

    assert blends["USD"] is None
    assert pillar.last_contributing["USD"] == frozenset()
    assert pillar.last_contributing["EUR"] == frozenset(pillar.component_weights)


def test_a_currency_whose_every_component_expired_contributes_nothing() -> None:
    """The other `None` branch: enough sub-weight, none of it still counting.

    USD holds every component, so it clears the floor, and every one of them is
    at a freshness of 0.0, so there is no weight left to renormalise over. That
    is an absence rather than a reading of zero, and the set has to say so.
    """
    pillar = MonetaryPillar()
    codes = list(G10)
    component_z: dict[str, dict[str, float | None]] = {
        component: _spread(component, codes) for component in pillar.component_weights
    }
    expired = {component: {"USD": 0.0} for component in pillar.component_weights}

    blends = pillar.blend_components(component_z, component_freshness=expired)

    assert blends["USD"] is None
    assert pillar.last_contributing["USD"] == frozenset()
    assert pillar.last_contributing["EUR"] == frozenset(pillar.component_weights)


def test_a_cross_section_too_thin_to_stand_leaves_every_set_empty() -> None:
    """ADR 0008. Nobody is scored, so nobody contributed.

    The components were all there and all fresh. They still moved no score,
    because there is no cross-section to standardise them against, and a set
    naming them would hand the pillar weight for a run it refused to make.
    """
    pillar = MonetaryPillar()
    codes = list(G10)[:2]
    component_z: dict[str, dict[str, float | None]] = {
        component: _spread(component, codes) for component in pillar.component_weights
    }

    blends = pillar.blend_components(component_z)

    assert len(codes) < MIN_CROSS_SECTION
    assert set(blends) == set(codes)
    assert all(blend is None for blend in blends.values())
    assert pillar.last_contributing == dict.fromkeys(codes, frozenset())


def test_positioning_reports_nothing_for_a_currency_it_could_not_score() -> None:
    """The override has to answer the `None` case, not just the scored one.

    POSITIONING z-scores each currency against its own past, so a currency with
    no usable Commitments of Traders history is absent while its neighbours are
    scored. An override that names its component unconditionally would take the
    pillar's full weight on that currency.
    """
    pillar = PositioningPillar()
    scored_codes = list(G10)[:4]
    rows = [
        observation(
            "cot_net_pct_oi",
            code,
            5.0 + 1.5 * index + 0.4 * step,
            7 + step * CYCLE_DAYS[Frequency.WEEKLY],
            Frequency.WEEKLY,
        )
        for index, code in enumerate(scored_codes)
        for step in range(HISTORY)
    ]

    scores = dict(pillar.compute(rows, list(G10), ASOF))

    assert scores[scored_codes[0]].z is not None
    assert pillar.last_contributing[scored_codes[0]] == frozenset(
        {"positioning_response"}
    )
    for code in list(G10)[4:]:
        assert scores[code].z is None, code
        assert pillar.last_contributing[code] == frozenset(), code


def test_risk_reports_nothing_for_anyone_when_the_regime_is_unreadable() -> None:
    """RISK fails for all eight together or for none, and the set follows.

    Its two series are worldwide, so half a regime is not a partial answer: the
    pillar returns ``None`` for every currency. Naming its component anyway
    would take 0.10 of weight across the whole universe on a dead feed, which is
    the largest single miscount available here.
    """
    pillar = RiskPillar()
    cycle = CYCLE_DAYS[Frequency.DAILY]
    rows = [
        observation("world_equity_index", GLOBAL, 100.0 + 0.5 * step, 1 + step * cycle)
        for step in range(4 * 365)
    ]

    scores = dict(pillar.compute(rows, list(G10), ASOF))

    assert all(score.z is None for score in scores.values())
    assert pillar.last_contributing == dict.fromkeys(G10, frozenset())


def test_a_run_that_raises_does_not_leave_the_previous_run_readable() -> None:
    """`compute` clears the set before it starts, as it does `last_blend_sd`.

    The attribute is run-scoped, so a run that dies partway through must not
    leave the last run's answer standing where a caller reads it as this one's.
    That is the same reason the two blend diagnostics beside it are reset, and
    it is the failure mode a stale mapping would produce: not a raise, a
    confident set describing a run that did not happen.
    """
    pillar = MonetaryPillar()
    pillar.compute(monetary_run(), list(G10), ASOF)
    assert pillar.last_contributing["USD"]

    def _die(components: object) -> dict[str, float | None]:
        raise RuntimeError("the feed died mid-run")

    pillar._normalise = _die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        pillar.compute(monetary_run(), list(G10), ASOF)

    assert pillar.last_contributing == {}


# --- every pillar, including the two that bypass the blend ------------------


def universe_observations() -> list[Observation]:
    """A history per required indicator, deep enough for every pillar to score.

    Three things have to hold or a pillar scores nobody and the agreement below
    asserts nothing about it. The newest observation must clear the leg's
    publication lag, since `BasePillar._visible` dates an unstamped observation
    by its period plus that lag. The values must vary across currencies, since
    every component is standardised against its own cross-section. And there
    must be history: POSITIONING z-scores a series against its own past, and
    RISK reads a drawdown and a volatility z over `ScoringConfig.lookback_years`,
    so a single print each leaves both unscorable.

    The first indicator of each pillar is aged one day past its full-weight age,
    so its factor is below 1.0 and a mean taken over the wrong set of components
    differs from a mean taken over the right one.
    """
    rows: list[Observation] = []
    for pillar in default_pillars():
        for offset, indicator in enumerate(pillar.requires):
            spec = INDICATORS[indicator]
            worldwide = GLOBAL in spec.series
            codes = [GLOBAL] if worldwide else list(G10)
            for index, code in enumerate(codes):
                ref = spec.series.get(code)
                frequency = ref.frequency if ref is not None else spec.frequency
                newest = (
                    full_weight_age(ref, frequency) + 1
                    if offset == 0
                    else publication_lag(ref, frequency)
                )
                cycle = CYCLE_DAYS[frequency]
                # The global series carry four years, which is what a
                # volatility z-score over the configured lookback needs. The
                # per-currency ones need only enough to have a past.
                depth = max(HISTORY, -(-4 * 365 // cycle)) if worldwide else HISTORY
                rows += [
                    observation(
                        indicator,
                        code,
                        1.0 + 0.37 * index + 0.11 * offset + 0.05 * step,
                        newest + step * cycle,
                        frequency,
                    )
                    for step in range(depth)
                ]
    return rows


@pytest.mark.parametrize("pillar", default_pillars(), ids=lambda p: p.name.value)
def test_every_pillar_weighs_freshness_over_the_set_it_reports(
    pillar: BasePillar,
) -> None:
    """Criterion 2, across `default_pillars`.

    `PositioningPillar` and `RiskPillar` compute a score directly and never call
    `blend_components`, so they have to state their contributing set rather than
    inherit one. An override that answers the signature and not the contract
    fails in one of two silent ways: report nothing and the pillar loses all its
    weight, or report everything and the defect is back in the one pillar that
    skipped the fix. This asserts the weight equals the mean over the set the
    pillar itself reports, which neither survives.
    """
    scores = dict(pillar.compute(universe_observations(), list(G10), ASOF))
    weights = pillar.component_weights

    scored_currencies = [code for code, score in scores.items() if score.z is not None]
    assert scored_currencies, f"{pillar.name.value} scored nobody"

    for code in scored_currencies:
        reported = pillar.last_contributing.get(code, frozenset())
        assert reported, f"{pillar.name.value} reported no components for {code}"
        assert reported <= set(weights), (
            f"{pillar.name.value} named an unknown component"
        )

        measured = factors(scores[code])
        # The components that both blended and still count toward coverage: a
        # component at zero freshness, or one the pillar never measured, leaves
        # both sides of the mean rather than entering the denominator.
        counted = {
            component: measured[component]
            for component in reported
            if measured.get(component, 0.0) > 0.0
        }
        share = sum(weights[component] for component in counted)
        expected = (
            sum(weights[component] * phi for component, phi in counted.items()) / share
            if share > 0.0
            else 0.0
        )
        assert scores[code].freshness_factor == pytest.approx(expected), code


# --- the path no default pillar takes today ---------------------------------


class _OneComponentPillar(BasePillar):
    """A single-component pillar that inherits `_normalise` rather than overriding it.

    `BasePillar._normalise` branches at one component and calls
    `cross_sectional_z` directly, so it never reaches `blend_components` and
    nothing downstream would know what it used. No pillar in `default_pillars`
    takes that branch, because POSITIONING and RISK are the only single-component
    pillars and both override the method. The branch is still the one the next
    such pillar inherits, and a pillar that reports no contributing set takes a
    freshness factor of 0.0 and loses its whole weight in the composite without
    raising. This exists so that failure is a test failure rather than a quiet
    one in a later release.

    It borrows `PillarName.INFLATION` because a name is required and inventing
    one would mean editing `types.py` for a fixture.
    """

    name = PillarName.INFLATION
    requires: Sequence[str] = ("cpi_yoy",)
    component_indicators: Mapping[str, tuple[str, ...]] = {"cpi_gap": ("cpi_yoy",)}
    headline_component = "cpi_gap"

    @property
    def component_weights(self) -> Mapping[str, float]:
        return {"cpi_gap": 1.0}

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Pass the newest `cpi_yoy` through as the one component."""
        return {
            currency: {
                "cpi_gap": values["cpi_yoy"][-1].value if values["cpi_yoy"] else None
            }
            for currency, values in extracted.items()
        }


def test_a_single_component_pillar_reports_the_component_it_scored() -> None:
    """The inherited single-component path records its own set.

    Four currencies carry a CPI print and four do not, which is the same split
    `MIN_CROSS_SECTION` is happy with. The scored ones must name the component
    and the unscored ones must name nothing, or the two failures in the class
    docstring are both reachable.

    Each print is aged at exactly its own leg's publication lag, so it is the
    newest figure that has actually been released and its factor is 1.0. A
    rounder age would be invisible on the legs whose lag is longer than it.
    """
    pillar = _OneComponentPillar()
    with_data = list(G10)[:4]
    rows = []
    for index, code in enumerate(with_data):
        ref = INDICATORS["cpi_yoy"].series[code]
        rows.append(
            observation(
                "cpi_yoy",
                code,
                2.0 + 0.3 * index,
                publication_lag(ref, ref.frequency),
                ref.frequency,
            )
        )

    scores = dict(pillar.compute(rows, list(G10), ASOF))

    for code in with_data:
        assert scores[code].z is not None, code
        assert pillar.last_contributing[code] == frozenset({"cpi_gap"}), code
        assert scores[code].freshness_factor == pytest.approx(1.0), code
    for code in list(G10)[4:]:
        assert pillar.last_contributing[code] == frozenset(), code


# --- the docstring says which definition is authoritative -------------------


def test_the_docstring_no_longer_claims_the_two_definitions_agree() -> None:
    """Criterion 4. The old text asserted the agreement this issue disproved.

    It said a missing component is handled once, by the floor and the
    renormalisation, and is not charged again here. That holds for a wholly
    absent component and failed for a partially absent one, which is the whole
    defect.
    """
    text = " ".join((inspect.getdoc(BasePillar.pillar_freshness) or "").split())

    assert "components the currency actually has" not in text
    assert "blend" in text
    assert "contributing" in text
