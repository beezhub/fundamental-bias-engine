"""The staleness ramp, measured against each series' own release schedule.

The defect these tests pin down. ``Observation.period`` is the first day of the
span a figure describes, so a punctual monthly print is 30 to 45 days old on the
day it is published and a punctual quarterly print is about 120 days old. A
single ramp that reaches zero at ``ScoringConfig.max_staleness_days`` (45) gives
zero weight to both. Measured against the registry on 2026-09-10, thirteen of
the seventeen registered indicators carried zero weight at the age their own
frequency says they publish at, which is every monthly and quarterly series in
the model.

What the tests assert, in one sentence each:

* The ramp is scaled to the indicator's own allowance, so a series that has just
  been published counts as fresh and a series that has missed its release does
  not.
* The scoring side is never more permissive than `registry.coverage_report`,
  which is the two-modules-two-answers failure that started this.
* Components inside a pillar are aged one by one, so a five-month-old GDP print
  and a five-day-old retail sales print are not represented by one number.
* Absence and staleness stay different answers. A component with no data is
  missing from the freshness mapping; a component past its allowance is present
  with a factor of 0.0.

No network, no fixtures on disk. Every observation here is built in the test.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import fbe.scoring as scoring_module
from fbe.config import ScoringConfig
from fbe.datasources.registry import INDICATORS, series_for
from fbe.pillars import default_pillars
from fbe.pillars.base import (
    DEFAULT_PUBLICATION_LAG_DAYS,
    BasePillar,
    staleness_allowance,
)
from fbe.pillars.growth import GrowthPillar
from fbe.pillars.inflation import InflationPillar
from fbe.pillars.monetary import MonetaryPillar
from fbe.scoring import freshness, score_currencies
from fbe.types import Frequency, Observation, PillarName, PillarScore
from fbe.universe import G10

ASOF = date(2026, 9, 10)
"""Run date from the issue, chosen so the registry's own ``last_observed`` dates
are the ones a reader can check by hand."""

CONFIG = ScoringConfig()


def _obs(
    indicator: str,
    currency: str,
    period: date,
    *,
    value: float = 2.0,
    frequency: Frequency = Frequency.MONTHLY,
) -> Observation:
    """Build one observation. ``value`` is irrelevant to every test here."""
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=period,
        source="test",
        series_id=f"TEST/{indicator}/{currency}",
        unit="percent",
        frequency=frequency,
    )


def _days_before(days: int) -> date:
    """Period stamp whose age at `ASOF` is exactly ``days``."""
    return date.fromordinal(ASOF.toordinal() - days)


# ----------------------------------------------------------------------
# The ramp itself
# ----------------------------------------------------------------------


def test_the_default_ramp_still_matches_the_published_formula() -> None:
    """Section 4.1's ramp is unchanged where no allowance is supplied.

    With the shipped defaults the ramp runs 15 to 45 days, so this is the
    regression guard on every currency and pillar whose indicator the registry
    does not know.
    """
    assert freshness(0, CONFIG) == 1.0
    assert freshness(15, CONFIG) == 1.0
    assert freshness(30, CONFIG) == pytest.approx(0.5)
    assert freshness(45, CONFIG) == 0.0
    assert freshness(46, CONFIG) == 0.0


def test_quarterly_cpi_has_no_weight_under_the_default_ramp() -> None:
    """The defect, stated as arithmetic.

    AUD and NZD headline CPI is stamped 2026-04-01 on a run dated 2026-09-10,
    which is 162 days. Against the 45-day default the factor is 0.0 and the
    INFLATION pillar contributes nothing to those two currencies.
    """
    assert (ASOF - date(2026, 4, 1)).days == 162
    assert freshness(162, CONFIG) == 0.0


def test_the_ramp_scales_to_the_indicators_own_allowance() -> None:
    """The same 162-day print against `cpi_yoy`'s 200-day allowance.

    Full weight runs to ``200 * 15 / 45 = 66.67`` days and the ramp reaches zero
    at 200, so the factor is ``(200 - 162) / (200 - 66.67) = 0.285``.
    """
    assert freshness(162, CONFIG, allowance_days=200) == pytest.approx(0.285)


def test_aud_inflation_carries_weight_after_the_fix() -> None:
    """Effective pillar weight for AUD INFLATION, before and after.

    Before: ``0.15 * 0.0 = 0.0``, so INFLATION drops out of AUD's coverage
    entirely. After: ``0.15 * 0.285 = 0.042750``.
    """
    weight = CONFIG.weights[InflationPillar.name]
    assert weight * freshness(162, CONFIG) == 0.0
    assert weight * freshness(162, CONFIG, allowance_days=200) == pytest.approx(0.04275)


def test_a_punctual_print_of_every_registered_indicator_carries_weight() -> None:
    """The registry walk. No series may be born stale.

    `DEFAULT_PUBLICATION_LAG_DAYS` is the age at which a series with no
    ``released_at`` is first admitted by the visibility rule in
    `BasePillar._extract`. An indicator admitted at that age and then given zero
    weight by the ramp can never contribute to a score at all, which is the
    widened form of this defect: it reaches every monthly and quarterly series
    in the model, not only quarterly CPI.
    """
    born_stale = [
        key
        for key, spec in INDICATORS.items()
        if freshness(
            DEFAULT_PUBLICATION_LAG_DAYS[spec.frequency],
            CONFIG,
            allowance_days=spec.max_staleness_days,
        )
        <= 0.0
    ]
    assert born_stale == []


def test_the_ramp_is_never_more_permissive_than_the_registry() -> None:
    """One rule, two modules, and the scoring side is the cautious one.

    `SeriesRef.stale_on` compares ``age > allowance``, so the last usable day is
    ``age == allowance``, where the ramp is already 0.0. The ramp therefore
    reaches zero one day before the registry gives up, and never the other way
    round. Asserted for the three legs named in the issue.
    """
    checks = (("cpi_yoy", "AUD"), ("gdp_yoy", "USD"), ("yield_2y", "GBP"))
    for indicator, currency in checks:
        spec = INDICATORS[indicator]
        ref = series_for(indicator, currency)
        assert ref is not None
        allowance = spec.max_staleness_days
        for age in range(0, allowance + 30):
            factor = freshness(age, CONFIG, allowance_days=allowance)
            stale = ref.stale_on(
                date.fromordinal(ref.last_observed.toordinal() + age)  # type: ignore[union-attr]
                if ref.last_observed is not None
                else ASOF,
                allowance,
            )
            if factor > 0.0:
                assert not stale, f"{indicator}/{currency} at {age} days"
            if stale:
                assert factor == 0.0, f"{indicator}/{currency} at {age} days"


def test_freshness_reads_the_ramp_shape_from_config() -> None:
    """Override the config and confirm the answer moves.

    A test that passes because a literal happens to equal a default proves
    nothing, so this widens the full-weight window and checks the factor rises.
    """
    wider = ScoringConfig(staleness_full_days=30)
    assert freshness(160, CONFIG, allowance_days=200) == pytest.approx(0.3)
    assert freshness(160, wider, allowance_days=200) == pytest.approx(0.6)


def test_freshness_rejects_a_non_positive_allowance() -> None:
    """An allowance of zero is a configuration error, not a stale series."""
    with pytest.raises(ValueError, match="allowance"):
        freshness(1, CONFIG, allowance_days=0)


# ----------------------------------------------------------------------
# Where the allowance comes from
# ----------------------------------------------------------------------


def test_allowance_comes_from_the_registry_and_falls_back_to_config() -> None:
    """The registry owns the release calendar; config owns the default."""
    assert staleness_allowance("cpi_yoy", CONFIG) == 200
    assert staleness_allowance("gdp_yoy", CONFIG) == 270
    assert staleness_allowance("yield_2y", CONFIG) == 10
    assert staleness_allowance("cb_guidance_tone", CONFIG) == CONFIG.max_staleness_days


def test_the_allowance_fallback_is_wired_to_config_not_to_a_literal() -> None:
    """Change the default and the fallback moves; the registry value does not."""
    other = ScoringConfig(max_staleness_days=90)
    assert staleness_allowance("cb_guidance_tone", other) == 90
    assert staleness_allowance("cpi_yoy", other) == 200


def test_pillar_indicator_keys_without_a_registry_allowance_are_pinned() -> None:
    """Which pillar inputs fall back to the default. The set is now empty.

    This pinned nine keys the registry did not carry under the name the pillar
    asked for, ``pmi_composite`` against ``pmi_manufacturing`` and ``vol_index``
    against ``vix`` among them. Those components took the 45-day default, which
    is the behaviour the per-indicator allowance removes everywhere else, so the
    set was recorded rather than left to drift, with "shrinking it is the fix".

    It shrank to nothing. Every one of the nine was a naming disagreement or a
    missing entry, and all nine are closed, so no pillar input falls back to the
    default any more and the allowance reaches every component.

    ``tests/test_registry_pillar_agreement.py`` is now the authority on this
    property. It asserts the same thing more precisely, parametrised per pillar
    and key so a failure names which one drifted rather than reporting one set
    difference. This test is kept for the moment because the empty set is the
    record that the gap closed; it is redundant with that file and can be
    removed once nobody needs the record.
    """
    unresolved = {
        key
        for pillar in default_pillars()
        for key in pillar.requires
        if key not in INDICATORS
    }
    assert unresolved == set()


# ----------------------------------------------------------------------
# Component-level ageing inside a pillar
# ----------------------------------------------------------------------


def test_every_component_declares_the_indicators_it_is_aged_against() -> None:
    """No component may be silently exempt from the freshness discount."""
    for pillar in default_pillars():
        assert set(pillar.component_indicators) == set(pillar.component_weights), (
            f"{type(pillar).__name__} component_indicators and component_weights "
            "disagree"
        )
        for component, indicators in pillar.component_indicators.items():
            assert indicators, (
                f"{type(pillar).__name__}.{component} ages against nothing"
            )


def test_components_of_one_pillar_are_aged_separately() -> None:
    """GROWTH with a five-month-old GDP print and a five-day-old retail print.

    A single pillar-level scalar cannot say that, which is the reason the
    discount moved to the component. GDP is 162 days old against its 270-day
    allowance, so its factor is ``(270 - 162) / (270 - 90) = 0.600``; retail
    sales at 5 days is inside its 90-day full-weight window and takes 1.0.
    """
    pillar = GrowthPillar()
    extracted = {
        "gdp_yoy": [
            _obs(
                "gdp_yoy",
                "USD",
                _days_before(162),
                frequency=Frequency.QUARTERLY,
            )
        ],
        "retail_sales_yoy": [_obs("retail_sales_yoy", "USD", _days_before(5))],
    }
    factors = pillar.component_freshness(extracted, ASOF)
    assert factors == {
        "gdp_yoy": pytest.approx(0.600),
        "retail_sales_yoy": pytest.approx(1.0),
    }


def test_a_stale_component_enters_the_blend_at_a_reduced_sub_weight() -> None:
    """GDP's share of the GROWTH blend falls from 0.60 to 0.474.

    The two components present carry 0.30 and 0.20. Discounted they carry
    ``0.30 * 0.600 = 0.180`` and ``0.20 * 1.000 = 0.200``, so GDP takes
    ``0.180 / 0.380 = 0.4737`` of the blend where the configured sub-weights
    alone would give it ``0.30 / 0.50 = 0.600``.
    """
    pillar = GrowthPillar()
    factors = pillar.component_freshness(
        {
            "gdp_yoy": [
                _obs("gdp_yoy", "USD", _days_before(162), frequency=Frequency.QUARTERLY)
            ],
            "retail_sales_yoy": [_obs("retail_sales_yoy", "USD", _days_before(5))],
        },
        ASOF,
    )
    weights = pillar.component_weights
    discounted = {key: weights[key] * phi for key, phi in factors.items()}
    total = sum(discounted.values())
    assert discounted["gdp_yoy"] == pytest.approx(0.180)
    assert discounted["gdp_yoy"] / total == pytest.approx(0.4737, abs=5e-4)


def test_a_component_with_no_data_is_absent_not_zero() -> None:
    """Absence and staleness are different answers and stay different.

    A component with no observations is missing from the mapping. A component
    whose newest print is past its allowance is present with a factor of 0.0.
    The first lowers the sub-weight the currency holds, which is what
    `MIN_COMPONENT_WEIGHT` judges; the second is a reading that the pillar can
    report.
    """
    pillar = InflationPillar()
    extracted: dict[str, Sequence[Observation]] = {
        "cpi_yoy": [_obs("cpi_yoy", "AUD", _days_before(400))],
    }
    factors = pillar.component_freshness(extracted, ASOF)
    assert factors == {"cpi_gap": 0.0}
    assert "core_gap" not in factors


def test_a_component_is_as_stale_as_its_stalest_input() -> None:
    """MONETARY's real policy rate is a policy rate minus a CPI print.

    The policy rate is a day old and CPI is 162 days old, so the component takes
    CPI's factor. Taking the freshest input would let a daily series carry a
    five-month-old one through the blend at full weight.
    """
    pillar = MonetaryPillar()
    extracted = {
        "policy_rate": [
            _obs("policy_rate", "AUD", _days_before(1), frequency=Frequency.DAILY)
        ],
        "cpi_yoy": [
            _obs("cpi_yoy", "AUD", _days_before(162), frequency=Frequency.QUARTERLY)
        ],
    }
    factors = pillar.component_freshness(extracted, ASOF)
    assert factors["real_policy_rate"] == pytest.approx(0.285)
    assert factors["policy_rate"] == pytest.approx(1.0)


def test_pillar_freshness_is_the_sub_weighted_mean_of_its_components() -> None:
    """AUD INFLATION on 2026-09-10, both components stamped 2026-04-01.

    Both factors are 0.285, so the sub-weighted mean over the components present
    is 0.285 and the pillar's effective weight is ``0.15 * 0.285 = 0.042750``,
    against 0.0 before this change.
    """
    pillar = InflationPillar()
    extracted = {
        "cpi_yoy": [
            _obs("cpi_yoy", "AUD", date(2026, 4, 1), frequency=Frequency.QUARTERLY)
        ],
        "core_cpi_yoy": [
            _obs("core_cpi_yoy", "AUD", date(2026, 4, 1), frequency=Frequency.QUARTERLY)
        ],
    }
    factor = pillar.pillar_freshness(extracted, ASOF)
    assert factor == pytest.approx(0.285)
    assert CONFIG.weights[InflationPillar.name] * factor == pytest.approx(0.04275)


def test_pillar_freshness_of_a_pillar_with_no_inputs_is_zero() -> None:
    """No data is not fresh data. The pillar is absent and weighs nothing."""
    assert InflationPillar().pillar_freshness({}, ASOF) == 0.0


# ----------------------------------------------------------------------
# Where the factors are allowed to go
# ----------------------------------------------------------------------


def test_the_aggregator_does_not_read_component_diagnostics() -> None:
    """`PillarScore.diagnostics` is report-only and must stay that way.

    The per-component factors are published there. Nothing in `scoring.py` may
    read them, or a per-run measurement would start moving composites.
    """
    source = Path(inspect.getfile(scoring_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    reads = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "diagnostics"
    ]
    assert reads == []


def test_the_component_discount_is_applied_where_the_blend_happens() -> None:
    """`blend_components` takes the factors and acts on them.

    The parameter has to exist for a caller to pass the factors at all, and the
    blend has to read it or the ramp changes nothing downstream. Asserted as the
    gap between a currency whose GDP z-score is 1.0 and one whose GDP z-score is
    0.0, with every other component flat across the run: that gap is GDP's share
    of the blend, so it falls when GDP is discounted and does not when it is
    fresh. The run-local mean cancels in a difference.

    The history fixes the divisor at 1.0 on purpose. Without it the divisor is
    the run's own standard deviation, which scales with the blend and cancels
    the discount exactly, so an implementation ignoring the factors would give
    the same two numbers and the test would pass on nothing.
    """
    pillar = GrowthPillar(blend_sd_history=(1.0,) * 20)
    currencies = ("USD", "EUR", "GBP", "JPY")
    component_z: dict[str, dict[str, float | None]] = {
        component: (
            {"USD": 1.0, "EUR": 0.0, "GBP": 0.0, "JPY": 0.0}
            if component == "gdp_yoy"
            else dict.fromkeys(currencies, 0.0)
        )
        for component in pillar.component_weights
    }

    assert (
        "component_freshness"
        in inspect.signature(BasePillar.blend_components).parameters
    )

    fresh = pillar.blend_components(component_z)
    halved = pillar.blend_components(
        component_z,
        component_freshness={"gdp_yoy": dict.fromkeys(currencies, 0.5)},
    )

    # Sub-weight 0.30 of a full 1.00 while fresh, then 0.15 of the 0.85 that
    # survives the discount.
    assert fresh["USD"] - fresh["EUR"] == pytest.approx(0.30)
    assert halved["USD"] - halved["EUR"] == pytest.approx(0.17647058823529413)


# ----------------------------------------------------------------------
# The age of a set, and the age of no set at all
# ----------------------------------------------------------------------
#
# `staleness_days` answers "how old is the freshest of these observations".
# For an empty set there is no such age, and its docstring says it returns
# `ScoringConfig.max_staleness_days + 1` so an absent pillar sorts as stale
# rather than as fresh. It was a `@staticmethod`, so it had no `self.config`
# to read that from and built a fresh `ScoringConfig()` instead, which ignores
# every override.
#
# That is the config-drift trap, and it is quiet. An owner who sets
# `max_staleness_days = 60` gets 46 back, which is inside the allowance, so the
# ramp reports an absent pillar as a late release at partial weight instead of
# as absent. See issue #28.


def test_the_empty_set_sentinel_follows_the_pillars_own_config() -> None:
    """Criterion 2. Override the ceiling and the sentinel must move with it.

    65 rather than the default 45, so a sentinel that happens to equal
    ``ScoringConfig().max_staleness_days + 1`` cannot pass. This is the wire,
    not the constant.
    """
    pillar = MonetaryPillar(config=ScoringConfig(max_staleness_days=65))

    assert pillar.staleness_days([], ASOF) == 66


def test_the_empty_set_sentinel_is_the_one_missing_score_documents() -> None:
    """One sentinel, one owner.

    `missing_score` takes the same value as the default for its own
    ``staleness_days`` argument. If these two ever disagree, a report shows one
    number for an absent pillar and the aggregator acts on the other.
    """
    for ceiling in (30, 45, 65):
        pillar = MonetaryPillar(config=ScoringConfig(max_staleness_days=ceiling))
        assert pillar.staleness_days([], ASOF) == pillar.config.max_staleness_days + 1


def test_staleness_days_reads_config_off_the_instance() -> None:
    """Criterion 1, checked against the method rather than the whole module.

    The two ways to honour the docstring from a static method were a literal
    46 or a fresh `ScoringConfig()`. Neither may come back: the first drifts
    silently from config, and the second ignores overrides while looking like
    it reads them.

    Scoped to this method's own source. `BasePillar.__init__` constructs a
    default `ScoringConfig()` legitimately, since that is how ``self.config``
    comes to exist when a caller passes none, and a module-wide ban would
    forbid the one construction that has to happen.
    """
    source = inspect.getsource(BasePillar.staleness_days)

    assert "self.config.max_staleness_days" in source
    assert "ScoringConfig()" not in source, (
        "building a default ScoringConfig here ignores any override the run "
        "was given; read self.config instead"
    )
    assert not re.search(r"return\s+4[56]\b", source), (
        "a bare staleness value here is a second copy of a number config holds"
    )


def test_the_age_of_a_non_empty_set_is_the_newest_period() -> None:
    """Criterion 3, first half. The freshest observation decides, not the last."""
    pillar = MonetaryPillar()
    observations = [
        _obs("yield_2y", "USD", _days_before(40)),
        _obs("yield_2y", "USD", _days_before(3)),
        _obs("yield_2y", "USD", _days_before(17)),
    ]

    assert pillar.staleness_days(observations, ASOF) == 3


def test_a_forward_dated_period_is_floored_at_zero() -> None:
    """Criterion 3, second half.

    Survey data is routinely stamped ahead of the run date. A negative age
    would read as fresher than fresh and, through the ramp, as a factor above
    1.0, which would hand a pillar more weight than its configured share.
    """
    pillar = MonetaryPillar()
    ahead = date.fromordinal(ASOF.toordinal() + 12)

    assert pillar.staleness_days([_obs("yield_2y", "USD", ahead)], ASOF) == 0


def test_a_period_stamped_on_the_run_date_is_zero_days_old() -> None:
    """The boundary between the two branches above, pinned."""
    pillar = MonetaryPillar()

    assert pillar.staleness_days([_obs("yield_2y", "USD", ASOF)], ASOF) == 0


def test_the_docstring_names_missing_score_as_the_path_for_an_absent_pillar() -> None:
    """Criterion 4.

    The sentinel exists so an absent pillar sorts as stale, but an absent
    pillar should be reaching `missing_score`, which sets ``z`` to ``None`` and
    is what the aggregator actually detects. A reader who finds only the
    sentinel could reasonably build the absent case out of it instead.
    """
    doc = inspect.getdoc(BasePillar.staleness_days) or ""

    assert "missing_score" in doc


# ----------------------------------------------------------------------
# The factor reaching the composite
# ----------------------------------------------------------------------

PUNCTUAL_MONTHLY_AGE = DEFAULT_PUBLICATION_LAG_DAYS[Frequency.MONTHLY]
"""Age of a monthly print on the day the visibility rule first admits it.

45 days, which is also `ScoringConfig.max_staleness_days`. The two being equal
is the defect in one line: the first day a punctual monthly series is visible to
the model is the day the default ramp values it at nothing.
"""


def _inflation_observations(age_days: int) -> list[Observation]:
    """CPI and core CPI for the whole G10 at one age.

    Values differ per currency because `MIN_CROSS_SECTION` refuses to
    standardise fewer than three usable readings, and a flat cross-section has
    no spread to normalise against, so every ``z`` would come back ``None`` and
    the test would pass on an absence rather than on a score.
    """
    period = _days_before(age_days)
    return [
        _obs(indicator, currency, period, value=1.0 + offset * 0.25)
        for offset, currency in enumerate(G10)
        for indicator in ("cpi_yoy", "core_cpi_yoy")
    ]


def _inflation_through_the_scorer(
    observations: Sequence[Observation],
    config: ScoringConfig = CONFIG,
) -> dict[str, PillarScore]:
    """Run INFLATION alone through `score_currencies` and index by currency.

    Through the aggregator rather than through `compute`, because the join
    between the two is where the factor was being dropped and neither side's
    own tests cross it.
    """
    scored = score_currencies(observations, [InflationPillar(config)], config, ASOF)
    return {row.currency: row.pillars[PillarName.INFLATION] for row in scored}


def test_one_currencys_stale_data_does_not_discount_another() -> None:
    """The factor is per currency, and the perturbation proves it.

    Every other fixture here ages the whole universe together, so a scorer that
    took one currency's factor and applied it to all eight would pass them all.
    This one leaves seven currencies punctual at 45 days and pushes USD alone to
    210 days, past CPI's 200-day allowance.

    USD must lose its INFLATION weight and the other seven must keep every bit of
    theirs. The failure this guards against is the one this repository fears
    most: a number that is plausible for the wrong currency.
    """
    punctual = _days_before(PUNCTUAL_MONTHLY_AGE)
    expired = _days_before(210)
    observations = [
        _obs(
            indicator,
            currency,
            expired if currency == "USD" else punctual,
            value=1.0 + offset * 0.25,
        )
        for offset, currency in enumerate(G10)
        for indicator in ("cpi_yoy", "core_cpi_yoy")
    ]

    scores = _inflation_through_the_scorer(observations)
    configured = CONFIG.weights[PillarName.INFLATION]

    assert scores["USD"].z is None
    assert scores["USD"].weight == 0.0
    for currency in G10:
        if currency == "USD":
            continue
        assert scores[currency].z is not None, f"{currency} caught USD's staleness"
        assert scores[currency].weight == pytest.approx(configured)


def test_a_currency_with_no_data_leaves_the_scorer_carrying_no_weight() -> None:
    """An absent pillar reports 0.0 freshness, not a missing factor.

    `missing_score` states it rather than leaving ``None`` and letting the
    age-based fallback reach the same answer through the sentinel age. The two
    agreeing today is a coincidence of ``max_staleness_days + 1`` sitting past
    the ramp, and a run that raised the ceiling would separate them: issue #28
    is that exact bug one level down.

    Weight rather than composite, because ``z`` is ``None`` here and every
    arithmetic consumer already skips the pillar on that. This pins the weight
    an absent pillar reports, which is what a reader sees.

    Note that `scoring._unscored`, the other absence path, taken when a pillar
    raises or returns nothing, deliberately keeps the full configured weight so
    a report can say what the run lost. The two absence paths therefore report
    different weights. That predates this change rather than arriving with it,
    because `missing_score` already reached zero through the ramp, and which one
    a report should show is a separate question.
    """
    observations = [
        _obs(
            indicator, currency, _days_before(PUNCTUAL_MONTHLY_AGE), value=1.0 + offset
        )
        for offset, currency in enumerate(G10)
        if currency != "USD"
        for indicator in ("cpi_yoy", "core_cpi_yoy")
    ]

    scores = _inflation_through_the_scorer(observations)

    assert scores["USD"].z is None
    assert scores["USD"].freshness_factor == 0.0
    assert scores["USD"].weight == 0.0


def test_a_punctual_monthly_pillar_keeps_its_weight_through_the_scorer() -> None:
    """Issue #162, criterion 1. A punctual print carries close to its full weight.

    CPI stamped 45 days before the run is the ordinary case, not a late one.
    Its allowance is 200 days, full weight runs to ``200 * 15 / 45 = 66.67``
    days, and 45 is inside that, so the factor is 1.0 and INFLATION leaves the
    scorer with the whole 0.15 the configuration gave it.

    Before the fix the scorer judged the same print against the global 45-day
    ceiling, where `freshness` is exactly 0.0, so the weight was 0.0 and ``z``
    was cleared.
    """
    scores = _inflation_through_the_scorer(
        _inflation_observations(PUNCTUAL_MONTHLY_AGE)
    )
    configured = CONFIG.weights[PillarName.INFLATION]

    for currency in G10:
        score = scores[currency]
        assert score.z is not None, f"{currency} lost its score to the ramp"
        assert score.weight == pytest.approx(configured)


def test_the_registry_allowance_decides_the_weight_the_scorer_applies() -> None:
    """Issue #162, criterion 2. Override an allowance and the weight moves.

    With CPI's allowance cut from 200 days to 60, full weight runs to
    ``60 * 15 / 45 = 20`` days and the ramp reaches zero at 60, so a 45-day-old
    print sits at ``(60 - 45) / (60 - 20) = 0.375`` and the pillar leaves with
    ``0.15 * 0.375 = 0.05625``.

    The override is the point. A test that only checked the 200-day answer
    would still pass if the implementation read the global default and happened
    to agree.
    """
    observations = _inflation_observations(PUNCTUAL_MONTHLY_AGE)
    configured = CONFIG.weights[PillarName.INFLATION]

    full = _inflation_through_the_scorer(observations)
    narrowed = {
        key: (
            replace(spec, max_staleness_days=60)
            if key in ("cpi_yoy", "core_cpi_yoy")
            else spec
        )
        for key, spec in INDICATORS.items()
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("fbe.pillars.base.INDICATORS", narrowed)
        reduced = _inflation_through_the_scorer(observations)

    for currency in G10:
        assert full[currency].weight == pytest.approx(configured)
        assert reduced[currency].weight == pytest.approx(configured * 0.375)


def test_a_pillar_genuinely_past_its_allowance_still_loses_its_weight() -> None:
    """Issue #162, criterion 3. The ramp still bites, either side of the boundary.

    CPI's allowance is 200 days. At 199 the factor is
    ``(200 - 199) / (200 - 66.67) = 0.0075``, so the pillar survives on a
    sliver of weight. At 200 it is 0.0, the weight is zero and ``z`` is cleared,
    which is the marker the aggregator reads.

    This is the half of the fix that could have been lost. Scaling the ramp to
    the registry must not turn it off.
    """
    inside = _inflation_through_the_scorer(_inflation_observations(199))
    past = _inflation_through_the_scorer(_inflation_observations(200))
    configured = CONFIG.weights[PillarName.INFLATION]

    for currency in G10:
        assert inside[currency].z is not None
        assert inside[currency].weight == pytest.approx(configured * 0.0075)
        assert past[currency].z is None
        assert past[currency].weight == 0.0


HISTORY_STEP_DAYS: dict[Frequency, int] = {
    Frequency.DAILY: 1,
    Frequency.WEEKLY: 7,
    Frequency.MONTHLY: 30,
    Frequency.QUARTERLY: 91,
    Frequency.IRREGULAR: 30,
}
"""Gap between consecutive periods of a series, by how often it publishes."""

HISTORY_POINTS = 24
"""Observations per series, enough to clear `MIN_TIME_SERIES_WINDOW` of 12.

EMPLOYMENT reads a six-month change and RISK reads a drawdown and a volatility
z-score, so neither can be scored from a single print however fresh it is. A
fixture of one observation per series would leave both absent and the coverage
this test asserts on would be measuring the fixture rather than the ramp.
"""


def _punctual_universe() -> list[Observation]:
    """Every registered indicator, published on time, with history behind it.

    The newest observation of each series is aged at the assumed publication lag
    for its own frequency, which is the youngest age at which the visibility rule
    admits an unstamped print. That is the ordinary case: a monthly series is 45
    days old the day it lands and a quarterly one is 120.

    Values fall with age so the series have a direction, and differ per currency
    so the cross-section has a spread to standardise against.
    """
    return [
        _obs(
            key,
            currency,
            _days_before(
                DEFAULT_PUBLICATION_LAG_DAYS[spec.frequency]
                + point * HISTORY_STEP_DAYS[spec.frequency]
            ),
            value=10.0 + offset * 0.25 - point * 0.1,
            frequency=spec.frequency,
        )
        for key, spec in INDICATORS.items()
        for offset, currency in enumerate([*G10, "GLOBAL"])
        for point in range(HISTORY_POINTS)
    ]


def test_real_pillars_reach_a_usable_composite_on_ordinary_inputs() -> None:
    """Issue #162, criterion 4. The assertion whose absence hid all of this.

    Every pillar's own tests call `compute` or its internals, and the
    aggregator's tests build `PillarScore` values by hand, so nothing before this
    drove real pillars through `score_currencies`. Both halves were tested and
    the join between them was not.

    Before the fix this fixture came back at coverage 0.40 for every currency,
    with MONETARY and RISK surviving because both read daily series that clear a
    45-day ramp, and every pillar fed by a monthly or quarterly release scoring
    nothing. That is under ``ScoringConfig.min_coverage`` of 0.60 and so refused
    by the CLI. After it, 0.7925: the 0.80 the five implemented pillars hold
    between them, less the 0.0075 GROWTH gives up, which is five percent of its
    0.15, because its quarterly GDP component is genuinely past its own
    full-weight plateau at 120 days against a 90-day one. EXTERNAL and
    POSITIONING are still scaffolded and hold the remaining 0.20.

    Coverage is asserted as a floor rather than as 0.7925, because the figure
    moves the day either scaffolded pillar lands and that is not a regression.
    The two per-pillar assertions below are what make the floor mean something.
    """
    scored = score_currencies(
        _punctual_universe(), default_pillars(CONFIG), CONFIG, ASOF
    )

    assert len(scored) == len(G10)
    for row in scored:
        assert row.coverage >= CONFIG.min_coverage, (
            f"{row.currency} covered {row.coverage:.4f} of the pillar weight, "
            f"under the {CONFIG.min_coverage} the CLI refuses below"
        )
        assert row.composite != 0.0
        # INFLATION is the pillar this defect was reported against, pinned by
        # name because coverage alone could be carried by the two pillars that
        # were never broken.
        inflation = row.pillars[PillarName.INFLATION]
        assert inflation.z is not None
        assert inflation.weight == pytest.approx(CONFIG.weights[PillarName.INFLATION])
        # GROWTH is the only pillar here whose factor is not 1.0, so it is the
        # one that catches a scorer applying a single factor to every pillar of
        # a currency. Without this, a scorer that read the first pillar's factor
        # and reused it passes the whole suite: the other ramp tests run one
        # pillar at a time and the coverage floor sits 0.20 below the truth.
        # GROWTH's own discount does not move when a scaffolded pillar lands.
        growth = row.pillars[PillarName.GROWTH]
        assert growth.z is not None
        assert growth.weight == pytest.approx(CONFIG.weights[PillarName.GROWTH] * 0.95)


class _FactorDouble:
    """A pillar whose reported factor contradicts its own age.

    Not a `BasePillar`. The point is to stand where a third-party implementation
    of the `fbe.types.Pillar` protocol stands, and to let the factor and the age
    disagree so that a test can tell which one the scorer read.
    """

    def __init__(self, factor: float | None, staleness_days: int = 0) -> None:
        self.name = PillarName.INFLATION
        self.requires: Sequence[str] = ()
        self._factor = factor
        self._staleness_days = staleness_days

    def compute(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> dict[str, PillarScore]:
        return {
            currency: PillarScore(
                pillar=self.name,
                currency=currency,
                raw=None,
                z=0.5,
                score=0.5,
                weight=0.0,
                asof=asof,
                staleness_days=self._staleness_days,
                freshness_factor=self._factor,
            )
            for currency in currencies
        }


def test_the_scorer_takes_the_pillars_factor_rather_than_the_default_ramp() -> None:
    """The wire, tested at two ages where the factor and the ramp disagree.

    The supplied case reports ``staleness_days`` of 0, at which the default ramp
    is 1.0, alongside a freshness factor of 0.25. A scorer reading the age leaves
    the full weight; a scorer reading the factor takes a quarter of it. The two
    cannot both pass.

    The withheld case reports no factor at 30 days, where the ramp is 0.5, and is
    the half that pins the documented fallback. At 0 days it would pin nothing:
    the ramp answers 1.0 there and so does a quiet ``factor = 1.0`` default, so
    an implementation that dropped the fallback entirely would still pass. The
    age is 30 for exactly that reason.
    """
    configured = CONFIG.weights[PillarName.INFLATION]

    supplied = score_currencies([], [_FactorDouble(0.25)], CONFIG, ASOF)
    withheld = score_currencies(
        [], [_FactorDouble(None, staleness_days=30)], CONFIG, ASOF
    )

    for row in supplied:
        score = row.pillars[PillarName.INFLATION]
        # Without this the test can pass through `_unscored`, which carries the
        # configured weight untouched. A double that raises inside `compute` is
        # caught by `score_currencies` and turned into exactly that, so an
        # assertion on the weight alone cannot tell a scorer that read the
        # factor from a pillar that never ran.
        assert score.z is not None, "the double did not score; it raised"
        assert score.weight == pytest.approx(configured * 0.25)
    for row in withheld:
        score = row.pillars[PillarName.INFLATION]
        assert score.z is not None, "the double did not score; it raised"
        assert score.weight == pytest.approx(configured * 0.5)
