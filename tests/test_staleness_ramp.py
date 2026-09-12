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
from fbe.scoring import freshness
from fbe.types import Frequency, Observation

ASOF = date(2026, 9, 10)
"""Run date from the issue, chosen so the registry's own ``last_observed`` dates
are the ones a reader can check by hand."""

CONFIG = ScoringConfig()

SCAFFOLD = re.compile(r"is scaffolded;")
"""Marker that a callable is still a stub, as ``tests/test_stubs.py`` requires."""


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


def test_the_component_discount_is_documented_where_the_blend_happens() -> None:
    """`blend_components` takes the factors, so the discount cannot be skipped.

    The body is still scaffolded, so the contract is the deliverable here: the
    parameter has to exist for a caller to pass the factors at all.
    """
    parameters = inspect.signature(BasePillar.blend_components).parameters
    assert "component_freshness" in parameters
    assert SCAFFOLD.search(inspect.getsource(BasePillar.blend_components))
