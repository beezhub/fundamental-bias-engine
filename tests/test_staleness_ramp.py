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
from fbe.datasources.registry import (
    CYCLE_DAYS,
    DEFAULT_PUBLICATION_LAG_DAYS,
    INDICATORS,
    full_weight_age,
    publication_lag,
    series_for,
    staleness_allowance,
)
from fbe.pillars import default_pillars
from fbe.pillars.base import BasePillar
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


def test_the_ramp_shape_is_unchanged_on_the_bounds_it_is_given() -> None:
    """Section 4.1's shape, now that both bounds arrive as arguments.

    The ramp is still flat to ``s0``, linear to ``S`` and zero past it. What
    #126 changed is where those two ages come from: the leg, rather than a
    configured ratio of a hand-keyed allowance. Written on the old 15 and 45
    so the shape can be compared against the published formula directly.
    """
    assert freshness(0, 15, 45) == 1.0
    assert freshness(15, 15, 45) == 1.0
    assert freshness(30, 15, 45) == pytest.approx(0.5)
    assert freshness(45, 15, 45) == 0.0
    assert freshness(46, 15, 45) == 0.0


def test_the_quarterly_cpi_print_that_had_no_weight_now_has_all_of_it() -> None:
    """The defect and its fix, stated as arithmetic on the same print.

    AUD and NZD headline CPI is stamped 2026-04-01 on a run dated 2026-09-10,
    which is 162 days. Against the 45-day global ramp the factor was 0.0.
    Issue #8 raised it to 0.285 by giving the indicator a 200-day allowance,
    which still started its discount at 200/3. The leg publishes 120 days
    after its quarter starts and the next print is due 92 days later, so
    nothing is late until 212 days and this print is worth all of its weight.
    """
    assert (ASOF - date(2026, 4, 1)).days == 162
    assert freshness(162, AUD_CPI_S0, AUD_CPI_S) == 1.0


def test_the_same_print_is_judged_by_its_own_legs_calendar() -> None:
    """One indicator, two legs, two answers, and the age is the same.

    ``cpi_yoy`` is quarterly for AUD and monthly for USD. At 162 days the
    Australian print is punctual, inside its 212-day window. The American one
    is five months past a monthly release and long past its 107-day ceiling.
    A ramp keyed on the indicator rather than the leg has to give one answer
    for both, and either answer is wrong for one of them.
    """
    assert freshness(162, AUD_CPI_S0, AUD_CPI_S) == 1.0
    assert freshness(162, 76, 107) == 0.0


def test_aud_inflation_carries_its_whole_configured_weight() -> None:
    """Effective pillar weight for AUD INFLATION, through three stages.

    The 45-day global ramp gave ``0.15 * 0.0 = 0.0`` and INFLATION dropped out
    of AUD's coverage entirely. Issue #8's allowance gave ``0.15 * 0.285``.
    Deriving the ramp from the leg gives ``0.15 * 1.0``, which is the whole
    point: the declared weight is the operative weight for a punctual print,
    whatever its cadence.
    """
    weight = CONFIG.weights[InflationPillar.name]

    assert weight * freshness(162, AUD_CPI_S0, AUD_CPI_S) == pytest.approx(0.15)


def test_the_registry_walk_moved_to_the_per_leg_file() -> None:
    """This file's walk aged every indicator at
    ``DEFAULT_PUBLICATION_LAG_DAYS[spec.frequency]``, the parent spec's
    cadence. 36 legs carry a frequency their spec does not, so the walk was
    reading a monthly ramp for quarterly legs, which is part of why #126
    survived this long. ``tests/test_ramp_from_the_leg.py`` walks the same
    ground per leg, and this placeholder records why the walk is not here.
    """
    import tests.test_ramp_from_the_leg as per_leg

    assert hasattr(per_leg, "test_no_registered_leg_is_born_stale")
    assert hasattr(
        per_leg, "test_no_registered_leg_reaches_zero_before_its_next_print_is_due"
    )


def test_the_ramp_is_never_more_permissive_than_the_registry() -> None:
    """One rule, two modules, and the scoring side is the cautious one.

    `SeriesRef.stale_on` compares ``age > allowance``, so the last usable day is
    ``age == allowance``, where the ramp is already 0.0. The ramp therefore
    reaches zero one day before the registry gives up, and never the other way
    round. Asserted for the three legs named in the issue.
    """
    checks = (("cpi_yoy", "AUD"), ("gdp_yoy", "USD"), ("yield_2y", "GBP"))
    for indicator, currency in checks:
        ref = series_for(indicator, currency)
        assert ref is not None
        allowance = staleness_allowance(ref, ref.frequency)
        full = full_weight_age(ref, ref.frequency)
        for age in range(0, allowance + 30):
            factor = freshness(age, full, allowance)
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


def test_the_ramp_reads_no_config_at_all() -> None:
    """It used to take its shape from ``staleness_full_days`` over
    ``max_staleness_days`` and its ceiling from the indicator's allowance. Both
    are gone: a caller that knows the leg passes both ages, and a configured
    number that nothing reads would be the config-drift defect in reverse.
    """
    import inspect

    assert "config" not in inspect.signature(freshness).parameters
    assert not hasattr(ScoringConfig(), "staleness_full_days")


def test_freshness_rejects_a_non_positive_allowance() -> None:
    """An allowance of zero is a configuration error, not a stale series."""
    with pytest.raises(ValueError, match="allowance"):
        freshness(1, CONFIG, allowance_days=0)


# ----------------------------------------------------------------------
# Where the allowance comes from
# ----------------------------------------------------------------------


def test_the_allowance_comes_from_the_leg_and_needs_no_fallback() -> None:
    """The registry owns the release calendar, and now owns both ages.

    There is no config fallback left to reach for. A key the registry does not
    carry still has an observation with a frequency on it, so the frequency
    tables answer for it, which is the same answer `BasePillar._visible` gives
    the same observation. One unknown key cannot take a different ramp from
    the rule that decided whether the run could see it at all.
    """
    aud_cpi = series_for("cpi_yoy", "AUD")
    usd_cpi = series_for("cpi_yoy", "USD")
    assert aud_cpi is not None and usd_cpi is not None

    # Same indicator, two cadences, two ramps. 120 + 92 and 45 + 31.
    assert full_weight_age(aud_cpi, aud_cpi.frequency) == 212
    assert staleness_allowance(aud_cpi, aud_cpi.frequency) == 304
    assert full_weight_age(usd_cpi, usd_cpi.frequency) == 76
    assert staleness_allowance(usd_cpi, usd_cpi.frequency) == 107

    # An unregistered key falls to the frequency tables, not to a global.
    assert full_weight_age(None, Frequency.MONTHLY) == 76
    assert staleness_allowance(None, Frequency.MONTHLY) == 107


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
    """GROWTH with a GDP print half a quarter late and a fresh retail print.

    A single pillar-level scalar cannot say that, which is the reason the
    discount moved to the component. US GDP is quarterly: published 120 days
    after the quarter starts, due again 92 days later, so nothing is late
    until 212 days and the weight is gone at 304. At 258 days it is exactly
    half a cycle late, ``(304 - 258) / (304 - 212) = 0.5``. Retail sales at 5
    days is inside its own full-weight window and takes 1.0.
    """
    pillar = GrowthPillar()
    extracted = {
        "gdp_yoy": [
            _obs(
                "gdp_yoy",
                "USD",
                _days_before(258),
                frequency=Frequency.QUARTERLY,
            )
        ],
        "retail_sales_yoy": [_obs("retail_sales_yoy", "USD", _days_before(5))],
    }
    factors = pillar.component_freshness(extracted, ASOF)
    assert factors == {
        "gdp_yoy": pytest.approx(0.500),
        "retail_sales_yoy": pytest.approx(1.0),
    }


def test_a_stale_component_enters_the_blend_at_a_reduced_sub_weight() -> None:
    """GDP's share of the GROWTH blend falls from 0.60 to 0.4286.

    The two components present carry 0.30 and 0.20. With GDP half a cycle
    late they carry ``0.30 * 0.500 = 0.150`` and ``0.20 * 1.000 = 0.200``, so
    GDP takes ``0.150 / 0.350 = 0.4286`` of the blend where the configured
    sub-weights alone would give it ``0.30 / 0.50 = 0.600``.
    """
    pillar = GrowthPillar()
    factors = pillar.component_freshness(
        {
            "gdp_yoy": [
                _obs("gdp_yoy", "USD", _days_before(258), frequency=Frequency.QUARTERLY)
            ],
            "retail_sales_yoy": [_obs("retail_sales_yoy", "USD", _days_before(5))],
        },
        ASOF,
    )
    weights = pillar.component_weights
    discounted = {key: weights[key] * phi for key, phi in factors.items()}
    total = sum(discounted.values())
    assert discounted["gdp_yoy"] == pytest.approx(0.150)
    assert discounted["gdp_yoy"] / total == pytest.approx(0.4286, abs=5e-4)


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

    The policy rate is a day old and the quarterly CPI print is 258 days old,
    half a cycle late at 0.5, so the component takes CPI's factor. Taking the
    freshest input would let a daily series carry a late one through the blend
    at full weight.
    """
    pillar = MonetaryPillar()
    extracted = {
        "policy_rate": [
            _obs("policy_rate", "AUD", _days_before(1), frequency=Frequency.DAILY)
        ],
        "cpi_yoy": [
            _obs("cpi_yoy", "AUD", _days_before(258), frequency=Frequency.QUARTERLY)
        ],
    }
    factors = pillar.component_freshness(extracted, ASOF)
    assert factors["real_policy_rate"] == pytest.approx(0.5)
    assert factors["policy_rate"] == pytest.approx(1.0)


def test_pillar_freshness_is_the_sub_weighted_mean_of_its_components() -> None:
    """AUD INFLATION on 2026-09-10, with one component late and one punctual.

    Headline CPI is stamped 2026-04-01, which is 162 days and inside the
    212-day full-weight window, so it takes 1.0. Core is stamped 258 days back,
    half a cycle late at 0.5. The sub-weights are 0.40 and 0.60, so the mean is
    ``0.40 * 1.0 + 0.60 * 0.5 = 0.70`` and the pillar's effective weight is
    ``0.15 * 0.70 = 0.105``.

    Two different factors on purpose. With both components on one release they
    always carry the same factor, and a mean of one number cannot tell a
    sub-weighted mean from a plain one.
    """
    pillar = InflationPillar()
    extracted = {
        "cpi_yoy": [
            _obs("cpi_yoy", "AUD", date(2026, 4, 1), frequency=Frequency.QUARTERLY)
        ],
        "core_cpi_yoy": [
            _obs(
                "core_cpi_yoy",
                "AUD",
                _days_before(258),
                frequency=Frequency.QUARTERLY,
            )
        ],
    }
    factor = pillar.pillar_freshness(extracted, ASOF)
    assert pillar.component_weights == {"cpi_gap": 0.40, "core_gap": 0.60}
    assert factor == pytest.approx(0.70)
    assert CONFIG.weights[InflationPillar.name] * factor == pytest.approx(0.105)


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


# ----------------------------------------------------------------------
# The stamping convention (issue #27)
# ----------------------------------------------------------------------
#
# `Observation.period` is the first day of the span a figure describes, so age
# is days since the period began. Two modules used to read it two ways: the
# lag table assumed first-day stamping and the `staleness_days` docstring
# anchored its example on the quarter's end, a month apart for a monthly series
# and a quarter apart for a quarterly one. The difference lands on the
# effective weight, so the convention is pinned here in arithmetic and in the
# contract's own wording.


def test_a_monthly_print_is_aged_from_the_first_day_of_its_month() -> None:
    """Criterion 3. US CPI for August 2026 on the day the BLS publishes it.

    ``period = 2026-08-01`` and ``asof = 2026-09-11`` is 41 days. Under
    last-day stamping the same print would be 10 days old, and the two readings
    put INFLATION's headline component at different effective weights on the
    same run.
    """
    pillar = MonetaryPillar()
    august_cpi = _obs("cpi_yoy", "USD", date(2026, 8, 1))

    assert pillar.staleness_days([august_cpi], date(2026, 9, 11)) == 41


def test_the_contract_states_first_day_stamping_with_both_examples() -> None:
    """Criterion 1. The convention lives on ``Observation.period``.

    The two examples are the ones every module in this area quotes: the month
    and the quarter. A reader who finds only "the period the data describes"
    has to guess which day, and the two modules that age it once guessed
    differently.
    """
    doc = inspect.getdoc(Observation) or ""

    assert "first day" in doc
    assert "2026-08-01" in doc
    assert "2026-04-01" in doc


def test_staleness_days_anchors_its_example_on_the_start_of_the_span() -> None:
    """The one-word correction from the ruling.

    The docstring's GDP example read "the quarter that ended four months ago",
    which is the last-day reading, forty lines below a lag table that assumes
    the first day. An example is where a reader goes to learn what age means,
    so it must not contradict the arithmetic under it.
    """
    doc = inspect.getdoc(BasePillar.staleness_days) or ""

    assert "first day" in doc
    assert "the quarter that began" in doc
    assert "the quarter that ended" not in doc


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

MONTHLY_CPI = tuple(
    currency
    for currency, ref in INDICATORS["cpi_yoy"].series.items()
    if ref.frequency is Frequency.MONTHLY
)
"""The six currencies whose CPI leg is monthly, read off the registry rather
than typed. AUD and NZD are quarterly and have their own ramp, which is the
whole of issue #126, so a test about a monthly boundary cannot include them."""

AUD_CPI_S0 = 212
"""``lag + cycle`` for AUD headline CPI: quarterly, published 120 days after
the quarter starts, next print due 92 days later. Read off ADR 0014's table
and checked against the registry below."""

AUD_CPI_S = 304
"""``lag + 2 * cycle`` for the same leg."""

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

    Each observation carries its own leg's frequency, which is what a source
    stamps it with. Two of the eight CPI legs are quarterly, so a single age
    means two different distances into the ramp, and a caller asserting on a
    boundary uses `MONTHLY_CPI` rather than the whole universe.
    """
    period = _days_before(age_days)
    return [
        _obs(
            indicator,
            currency,
            period,
            value=1.0 + offset * 0.25,
            frequency=_leg_frequency(indicator, currency),
        )
        for offset, currency in enumerate(G10)
        for indicator in ("cpi_yoy", "core_cpi_yoy")
    ]


def _leg_frequency(indicator: str, currency: str) -> Frequency:
    """The frequency a source would stamp on this leg's observations."""
    ref = series_for(indicator, currency)
    return ref.frequency if ref is not None else INDICATORS[indicator].frequency


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

    CPI stamped 45 days before the run is the ordinary case for a monthly leg,
    not a late one: the leg publishes at 45 days and the next print is due 31
    days later, so nothing is late until 76 and the factor is 1.0. INFLATION
    leaves the scorer with the whole 0.15 the configuration gave it.

    Before the fix the scorer judged the same print against the global 45-day
    ceiling, where `freshness` is exactly 0.0, so the weight was 0.0 and ``z``
    was cleared.

    The two quarterly legs are not in this: at 45 days their print is not yet
    published, so `BasePillar._visible` has not admitted it and the currency is
    absent rather than discounted. That is the visibility rule doing its job,
    and `test_real_pillars_reach_a_usable_composite_on_ordinary_inputs` is
    where the quarterly legs are scored at their own punctual age.
    """
    scores = _inflation_through_the_scorer(
        _inflation_observations(PUNCTUAL_MONTHLY_AGE)
    )
    configured = CONFIG.weights[PillarName.INFLATION]

    for currency in MONTHLY_CPI:
        score = scores[currency]
        assert score.z is not None, f"{currency} lost its score to the ramp"
        assert score.weight == pytest.approx(configured)


def test_the_legs_own_cycle_decides_the_weight_the_scorer_applies() -> None:
    """Issue #162, criterion 2, restated on what the ramp now reads.

    The allowance is no longer a field to override, so the equivalent lever is
    the leg's cycle. Shortening the monthly cycle from 31 days to 10 moves both
    bounds: full weight to ``45 + 10 = 55`` and zero at ``45 + 20 = 65``. A
    45-day-old print is punctual either way, so the test ages it to 60 days,
    where the shipped tables give 1.0 (inside 76) and the shortened one gives
    ``(65 - 60) / (65 - 55) = 0.5``.

    The override is the point. A test that only checked the shipped answer
    would still pass if the implementation read a global and happened to agree.
    """
    observations = _inflation_observations(60)
    configured = CONFIG.weights[PillarName.INFLATION]

    full = _inflation_through_the_scorer(observations)
    shortened = {**CYCLE_DAYS, Frequency.MONTHLY: 10}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("fbe.datasources.registry.CYCLE_DAYS", shortened)
        reduced = _inflation_through_the_scorer(observations)

    for currency in MONTHLY_CPI:
        assert full[currency].weight == pytest.approx(configured)
        assert reduced[currency].weight == pytest.approx(configured * 0.5)


def test_a_pillar_genuinely_past_its_allowance_still_loses_its_weight() -> None:
    """Issue #162, criterion 3. The ramp still bites, either side of the edge.

    A monthly CPI leg is worth nothing from 107 days: ``45 + 2 * 31``. At 106
    the factor is ``(107 - 106) / (107 - 76) = 1 / 31``, so the pillar survives
    on a sliver of weight. At 107 it is 0.0, the weight is zero and ``z`` is
    cleared, which is the marker the aggregator reads.

    This is the half of the fix that could have been lost. Deriving the ramp
    from the leg must not turn it off, and #126 is precisely a complaint that
    it was turned on too early rather than that it should not exist.
    """
    inside = _inflation_through_the_scorer(_inflation_observations(106))
    past = _inflation_through_the_scorer(_inflation_observations(107))
    configured = CONFIG.weights[PillarName.INFLATION]

    for currency in MONTHLY_CPI:
        assert inside[currency].z is not None
        assert inside[currency].weight == pytest.approx(configured * (1 / 31))
        assert past[currency].z is None
        assert past[currency].weight == 0.0


HISTORY_STEP_DAYS: dict[Frequency, int] = {
    Frequency.DAILY: 1,
    Frequency.WEEKLY: 7,
    Frequency.MONTHLY: 30,
    Frequency.QUARTERLY: 91,
    Frequency.ANNUAL: 365,
    Frequency.IRREGULAR: 30,
}
"""Gap between consecutive periods of a series, by how often it publishes.

Every member of `Frequency` needs an entry. This mapping and
`DEFAULT_PUBLICATION_LAG_DAYS` are the only two indexed by a spec's frequency,
and both are indexed with a bare subscript, so a new member without an entry
fails here while building the fixture rather than inside the test it breaks.
"""

QUARTERLY_CPI = frozenset(
    currency
    for currency, ref in INDICATORS["cpi_yoy"].series.items()
    if ref.frequency is Frequency.QUARTERLY
)
"""The currencies whose CPI leg is quarterly, AUD and NZD today, read off the
registry rather than typed so the assertion follows a registry change."""

HISTORY_POINTS = 24
"""Observations per series, enough to clear `MIN_TIME_SERIES_WINDOW` of 12.

EMPLOYMENT reads a six-month change and RISK reads a drawdown and a volatility
z-score, so neither can be scored from a single print however fresh it is. A
fixture of one observation per series would leave both absent and the coverage
this test asserts on would be measuring the fixture rather than the ramp.
"""


def _punctual_universe() -> list[Observation]:
    """Every registered indicator, published on time, with history behind it.

    The newest observation of each series is aged at its leg's publication lag,
    which is the youngest age at which the visibility rule admits an unstamped
    print. That is the ordinary case: a monthly series is 45 days old the day
    it lands and a quarterly one is 120, and a leg FRED mirrors from the OECD is
    older still, by the measured lag the registry holds for it since #222. The
    frequency is the leg's own where the registry has one, because a series
    that is quarterly for one currency is not aged as monthly for it.

    Values fall with age so the series have a direction, and differ per currency
    so the cross-section has a spread to standardise against.
    """

    def frequency_of(key: str, currency: str) -> Frequency:
        ref = series_for(key, currency)
        return ref.frequency if ref is not None else INDICATORS[key].frequency

    return [
        _obs(
            key,
            currency,
            _days_before(
                publication_lag(series_for(key, currency), frequency)
                + point * HISTORY_STEP_DAYS[frequency]
            ),
            value=10.0 + offset * 0.25 - point * 0.1,
            frequency=frequency,
        )
        for key in INDICATORS
        for offset, currency in enumerate([*G10, "GLOBAL"])
        for frequency in (frequency_of(key, currency),)
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
        # Flipped by #126, which is what this issue was about. #222 made this
        # fixture age each leg at its own lag, which exposed the defect here:
        # a punctual quarterly CPI print is 120 days old and the old ramp
        # discounted it to 0.6 of the declared weight while the six monthly
        # legs kept all of theirs. Every currency now carries the configured
        # weight on a punctual print, whatever its cadence, and AUD and NZD
        # are asserted alongside the rest rather than as an exception.
        assert inflation.weight == pytest.approx(CONFIG.weights[PillarName.INFLATION])
        # GROWTH mixes cadences within one pillar: a quarterly GDP leg, a
        # monthly retail leg, and some of each slowed further by FRED's
        # mirror. Before #126 those differences reached the weight, and the
        # discount differed per currency for no reason but the calendar. All
        # of them are punctual here, so all of them carry the configured
        # weight. The test that a pillar's own factor reaches its own weight,
        # rather than one factor being reused across a currency, is
        # `test_one_late_leg_discounts_its_own_pillar_and_no_other` below.
        growth = row.pillars[PillarName.GROWTH]
        assert growth.z is not None
        assert growth.weight == pytest.approx(CONFIG.weights[PillarName.GROWTH])


def test_one_late_leg_discounts_its_own_pillar_and_no_other() -> None:
    """The guard the all-punctual fixture can no longer carry on its own.

    A scorer that read one pillar's factor and reused it across the currency
    passes every all-punctual assertion, because every factor is 1.0. So this
    pushes exactly one leg late and checks the discount lands where it should
    and nowhere else.

    GDP for the United States is quarterly: punctual to 212 days, worthless at
    304. At 258 it is half a cycle late, so its component factor is 0.5. All
    four of GROWTH's components are present here and their sub-weights sum to
    1.0, with GDP carrying 0.30, so the sub-weighted mean is
    ``0.30 * 0.5 + 0.70 * 1.0 = 0.85`` and GROWTH leaves with
    ``0.15 * 0.85 = 0.1275``. INFLATION, which shares no input with it, is
    untouched.
    """
    observations = [
        (
            replace(observation, period=_days_before(258))
            if observation.indicator == "gdp_yoy" and observation.currency == "USD"
            else observation
        )
        for observation in _punctual_universe()
    ]

    scored = score_currencies(observations, default_pillars(CONFIG), CONFIG, ASOF)
    rows = {row.currency: row for row in scored}

    usd_growth = rows["USD"].pillars[PillarName.GROWTH]
    assert usd_growth.weight == pytest.approx(0.1275, abs=5e-4)
    assert rows["USD"].pillars[PillarName.INFLATION].weight == pytest.approx(
        CONFIG.weights[PillarName.INFLATION]
    )
    for currency in G10:
        if currency == "USD":
            continue
        assert rows[currency].pillars[PillarName.GROWTH].weight == pytest.approx(
            CONFIG.weights[PillarName.GROWTH]
        ), f"{currency} was discounted by the United States' late print"


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

    The withheld case used to pin a documented fallback to a global ramp. Since
    #126 there is no ramp this module can apply without knowing the leg, so a
    pillar that reports no factor is refused rather than guessed at, and the
    withheld half asserts the refusal reaches the caller instead.
    """
    configured = CONFIG.weights[PillarName.INFLATION]

    supplied = score_currencies([], [_FactorDouble(0.25)], CONFIG, ASOF)

    for row in supplied:
        score = row.pillars[PillarName.INFLATION]
        # Without this the test can pass through `_unscored`, which carries the
        # configured weight untouched. A double that raises inside `compute` is
        # caught by `score_currencies` and turned into exactly that, so an
        # assertion on the weight alone cannot tell a scorer that read the
        # factor from a pillar that never ran.
        assert score.z is not None, "the double did not score; it raised"
        assert score.weight == pytest.approx(configured * 0.25)
    with pytest.raises(ValueError, match="no freshness factor"):
        score_currencies([], [_FactorDouble(None, staleness_days=30)], CONFIG, ASOF)
