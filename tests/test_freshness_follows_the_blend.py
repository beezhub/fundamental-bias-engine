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
from fbe.pillars.external import ExternalPillar
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

DECLARED_WEIGHT = 1.0
"""MONETARY's declared sub-weight total, which is the denominator.

``real_policy_rate``'s 0.15 stays in it whether that component is absent or
expired, so the two weigh the same and the factor is continuous in age. An
earlier version of this file divided by the 0.85 that blended and expected
0.588235; that rule made the factor rise the day the CPI expired.
"""

EXPECTED_FRESHNESS = FRESH / DECLARED_WEIGHT
"""0.50, the components that blended weighed over everything MONETARY declares."""

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
    0.045 between the two readings, 0.195 before and 0.150 now, is real weight
    in the composite. The issue's own criterion quoted 0.1641, renormalised over
    the components that blended; that rule is the one that made the factor rise
    on the day a component expired, so the figure here is over the declared
    sub-weight instead.
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
    longer counting toward coverage, so it adds nothing to the weight. Its
    sub-weight stays in the denominator, exactly as an absent component's does,
    which is what makes the two cost the same.
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


def test_a_wholly_absent_component_weighs_what_an_expired_one_does() -> None:
    """A component with no observations at all sits in the denominator at zero.

    An earlier version of this test asserted ``(0.15 + 0.45 * 0.5) / 0.60``,
    renormalising over what was present. Renormalising absence while charging
    expiry is the rule that made the factor rise the day a component expired,
    so absence now costs its sub-weight here the same way expiry does, and the
    pillar carries the share of its weight its data supports. Whether the
    pillar speaks at all is still `MIN_COMPONENT_WEIGHT`'s question, in
    `blend_components`.
    """
    rows = [row for row in monetary_run() if row.indicator != "yield_2y_chg_3m"]
    pillar = MonetaryPillar()
    score = dict(pillar.compute(rows, list(G10), ASOF))["USD"]

    assert "yield_2y_chg_3m" not in factors(score)
    assert "yield_2y_chg_3m" not in pillar.last_contributing["USD"]
    assert score.freshness_factor == pytest.approx((0.15 * 1.0 + 0.45 * 0.5) / 1.0)


def test_a_pillar_left_holding_one_fresh_component_takes_its_share() -> None:
    """Weight in proportion to what still counts, with no cliff below it.

    `policy_rate` fresh with all four other components expired is 0.15 of the
    declared sub-weight still counting, so MONETARY carries 0.15 of its weight.
    An earlier rule renormalised onto the fresh component, reported 1.0, and
    then needed a second `MIN_COMPONENT_WEIGHT` floor to drop that to 0.0. The
    fixed denominator gives the proportional answer directly, and a floor would
    only reintroduce a step that section 4.1 of `docs/scoring-spec.md` forbids.
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
    assert score.freshness_factor == pytest.approx(0.15)


class _UnnormalisedPillar(BasePillar):
    """A pillar whose sub-weights declare their own scale rather than summing to 1.0.

    `BasePillar.blend_components` already judges its floor as a fraction of what
    the weight map declares, for the stated reason that passing a subset must not
    put every currency under it. `pillar_freshness` divides by the same map's
    total. A denominator hardcoded at 1.0 agrees with every pillar shipped
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


def test_the_denominator_reads_the_scale_the_weights_declare() -> None:
    """Half of a map summing to 2.0 is half, not all of it.

    `alpha` is measured and `beta` has missed a cycle, so the counted
    sub-weight is 1.0 of a declared 2.0 and the factor is half of `alpha`'s.
    Read against a denominator of 1.0 the same run would report `alpha`'s whole
    factor, as if the expired component were not declared at all.
    """
    pillar = _UnnormalisedPillar()
    extracted = {
        "cpi_yoy": _leg("cpi_yoy", 30),
        "core_cpi_yoy": _leg("core_cpi_yoy", 900),
    }
    measured = pillar.component_freshness(extracted, ASOF)

    assert sum(pillar.component_weights.values()) == 2.0
    assert measured["alpha"] > 0.0
    assert measured["beta"] == pytest.approx(0.0)
    assert pillar.pillar_freshness(extracted, ASOF, ("alpha", "beta")) == (
        pytest.approx(measured["alpha"] / 2.0)
    )


def test_half_the_sub_weight_expired_is_half_the_weight_not_none() -> None:
    """EMPLOYMENT's two components are 0.50 each, so half is a live case.

    An earlier rule dropped this pillar to 0.0 at exactly half its sub-weight
    expired, by a second `MIN_COMPONENT_WEIGHT` floor. That is a cliff: one more
    day of age on `employment_trend` took the weight from about half to none.
    The fixed denominator carries the surviving half at its own factor instead.
    `blend_components` still applies `MIN_COMPONENT_WEIGHT` to presence, which
    is where the question of whether the pillar speaks at all is asked.
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
    assert pillar.pillar_freshness(
        extracted, ASOF, tuple(pillar.component_weights)
    ) == pytest.approx(0.5 * measured["unemployment_6m"])


def test_a_component_blended_without_observations_is_not_charged_as_expired() -> None:
    """EXTERNAL's ``terms_of_trade`` is 0.0 by design for a currency with no
    commodity link, so it blends with no observations and has no age.

    Charging it at ``phi = 0.0`` would cut EXTERNAL's weight by 0.30 for USD,
    EUR, GBP, JPY and CHF on every run, for a component that is not missing.
    It leaves both sides of the mean instead, so USD with fresh balances gets
    1.0 and USD missing ``trade_trend`` gets ``0.40 / 0.70``.
    """
    pillar = ExternalPillar()
    ref = INDICATORS["current_account_gdp"].series["USD"]
    age = publication_lag(ref, ref.frequency)
    extracted = {"current_account_gdp": _leg("current_account_gdp", age)}
    phi = pillar.component_freshness(extracted, ASOF)["current_account_gdp"]

    assert phi == pytest.approx(1.0)
    assert "terms_of_trade" not in pillar.component_freshness(extracted, ASOF)
    assert pillar.pillar_freshness(
        extracted, ASOF, ("current_account_gdp", "terms_of_trade")
    ) == pytest.approx(0.40 * phi / 0.70)
    assert pillar.pillar_freshness(extracted, ASOF, ("terms_of_trade",)) == 0.0


def test_the_weight_is_a_fraction_of_the_declared_sub_weight() -> None:
    """The factor counts sub-weight, not components.

    INFLATION's two components are 0.40 and 0.60. Either one alone and fully
    fresh carries its own sub-weight share of the pillar's weight, 0.60 for core
    and 0.40 for headline. A rule counting components would give both 0.5, and
    one renormalising over what blended would give both 1.0, so a pillar holding
    0.40 of its data would speak at full strength.
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
    assert pillar.pillar_freshness(fresh, ASOF, ("core_gap",)) == pytest.approx(0.60)
    assert pillar.pillar_freshness(fresh, ASOF, ("cpi_gap",)) == pytest.approx(0.40)


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
        # Every declared component sits in the denominator and one that did not
        # blend adds zero to the numerator. One that blended with no measured
        # age leaves both sides.
        unaged = sum(weights[c] for c in reported if c not in measured)
        held = sum(weights[c] * measured[c] for c in reported if c in measured)
        expected = held / (sum(weights.values()) - unaged)
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


# --- the weight follows the age, with no step anywhere ----------------------


def _usd_cpi_lag() -> int:
    """The age in days at which a USD CPI print is first visible to the run.

    Below it `BasePillar._visible` hides the print, so the component is absent
    for a reason that has nothing to do with staleness. The sweeps start here so
    they measure ageing and nothing else.
    """
    ref = INDICATORS["cpi_yoy"].series["USD"]
    return publication_lag(ref, ref.frequency)


def test_monetary_weight_never_rises_as_the_cpi_ages() -> None:
    """Older data never earns more weight, on any day of the ramp.

    The earlier rule lifted a component out of the mean the day its factor
    reached 0.0, while a factor of 0.0001 stayed in both sides of it. So the
    factor fell as the CPI aged and then jumped back up the day it expired:
    0.534 at 100 days, 0.510 at 105, 0.588 at 110. Section 4.1 of
    `docs/scoring-spec.md` has a ramp so that a day of ageing never moves the
    weight in a step, and a step upward is the worst kind, because it rewards
    the run for its data getting older.

    Every day from first visibility to 200 days is checked, which runs past the
    allowance, so the expiry day itself is inside the sweep.
    """
    ages = range(_usd_cpi_lag(), 201)
    weights = [
        scored(monetary_run(cpi_age=age))["USD"].freshness_factor for age in ages
    ]

    rises = [
        (age, before, after)
        for age, before, after in zip(ages[1:], weights[:-1], weights[1:], strict=True)
        if after > before + 1e-12
    ]
    assert not rises, f"the factor rose with age at (age, before, after): {rises}"
    assert weights[0] > weights[-1]


def test_an_absent_cpi_weighs_exactly_what_an_expired_one_does() -> None:
    """Absent and expired are the same fact for the weight: nothing counting.

    Issue #172 asks that absent get no more than expired. Equality is the only
    answer that also keeps the ramp continuous: if absent weighed more, a CPI a
    few days short of expiry would be worth less than no CPI at all, and the run
    that has the print would be penalised against the one that does not. The
    loop pins that no visible age falls below absence.
    """
    absent = scored(monetary_run())["USD"].freshness_factor
    expired = scored(monetary_run(cpi_age=400))["USD"].freshness_factor

    assert absent == pytest.approx(expired)
    for age in range(_usd_cpi_lag(), 201):
        held = scored(monetary_run(cpi_age=age))["USD"].freshness_factor
        assert held >= absent - 1e-12, age
