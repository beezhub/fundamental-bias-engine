"""The staleness ramp is derived from the leg, per ADR 0014 and issue #126.

A punctual print carries full weight whatever its cadence. The discount is for
lateness, and lateness is measured from when the leg's own source publishes:

    s0 = lag + cycle          the oldest a punctual newest print gets
    S  = lag + 2 * cycle      one full cycle late, and worth nothing

Before this, ``s0`` was ``S / 3`` from a global ratio and ``S`` was a
hand-keyed allowance per indicator. A punctual quarterly print is 120 days old
on the day it is first visible, and ``S / 3`` is 60 to 90 for every quarterly
indicator, so quarterly legs began life on the falling part of the ramp and
never reached full weight. AUD and NZD carried 0.6 of their declared inflation
weight on publication day and 0.225 by the end of the quarter, against 1.0 for
the six currencies whose CPI is monthly. That is a currency ranked by its
statistics office's calendar, which is what this file exists to prevent.

The arithmetic here is recomputed by hand from the ADR's table rather than read
back from the implementation.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fbe.config import ScoringConfig, default_config
from fbe.datasources.registry import (
    CYCLE_DAYS,
    DEFAULT_PUBLICATION_LAG_DAYS,
    INDICATORS,
    publication_lag,
    series_for,
)
from fbe.pillars.base import BasePillar
from fbe.scoring import freshness
from fbe.types import Frequency, Observation

CONFIG = default_config().scoring

# Read off ADR 0014's table by hand: (lag, cycle, s0, S).
ADR_TABLE: dict[Frequency, tuple[int, int, int, int]] = {
    Frequency.DAILY: (1, 4, 5, 9),
    Frequency.WEEKLY: (7, 7, 14, 21),
    Frequency.MONTHLY: (45, 31, 76, 107),
    Frequency.QUARTERLY: (120, 92, 212, 304),
    Frequency.ANNUAL: (552, 366, 918, 1284),
    Frequency.IRREGULAR: (45, 31, 76, 107),
}


# --- the arithmetic ---------------------------------------------------------


@pytest.mark.parametrize("frequency", list(Frequency))
def test_the_tables_still_produce_the_ruled_numbers(frequency: Frequency) -> None:
    """If a table entry moves, the ADR's worked numbers move with it, and this
    says so before any behaviour test reports a puzzling failure."""
    lag, cycle, s0, ceiling = ADR_TABLE[frequency]
    assert DEFAULT_PUBLICATION_LAG_DAYS[frequency] == lag
    assert CYCLE_DAYS[frequency] == cycle
    assert lag + cycle == s0
    assert lag + 2 * cycle == ceiling


@pytest.mark.parametrize("frequency", list(Frequency))
def test_a_punctual_print_carries_full_weight_for_its_whole_cycle(
    frequency: Frequency,
) -> None:
    """From first visibility to the day before its successor is admitted.

    This is the defect stated as a property. A series is not late until the
    next one is due, so nothing about a publication calendar may cost weight.
    """
    lag, cycle, s0, _ = ADR_TABLE[frequency]
    for age in range(lag, s0 + 1):
        assert freshness(age, s0, ADR_TABLE[frequency][3]) == 1.0, (
            f"{frequency.value} at {age} days, inside lag {lag} plus cycle {cycle}"
        )


@pytest.mark.parametrize("frequency", list(Frequency))
def test_one_full_cycle_late_is_worth_nothing(frequency: Frequency) -> None:
    _, _, s0, ceiling = ADR_TABLE[frequency]
    assert freshness(ceiling, s0, ceiling) == pytest.approx(0.0)
    assert freshness(ceiling + 1, s0, ceiling) == 0.0


@pytest.mark.parametrize("frequency", list(Frequency))
def test_the_ramp_crosses_half_weight_at_the_middle_of_the_cycle(
    frequency: Frequency,
) -> None:
    """Linear between the two bounds, so half a cycle late is half weight.

    Asserted by bracketing rather than by an exact midpoint, because an odd
    cycle has no whole-day middle: a weekly ramp steps 1/7 per day and never
    lands on 0.5. The two days either side of the middle must straddle it.
    """
    _, cycle, s0, ceiling = ADR_TABLE[frequency]
    before = freshness(s0 + cycle // 2, s0, ceiling)
    after = freshness(s0 + cycle // 2 + 1, s0, ceiling)

    assert after <= 0.5 <= before, f"{frequency.value}: {before} then {after}"
    assert before - after == pytest.approx(1 / cycle), "one day is one step"


def test_the_quarterly_case_the_issue_was_filed_about() -> None:
    """AUD CPI, the numbers from #126's body and its 19 September verification.

    Punctual was 0.6 and the freshest print reachable on an ordinary run day
    was 0.225. Both are 1.0 now: 120 and 170 days both sit inside s0 = 212.
    """
    _, _, s0, ceiling = ADR_TABLE[Frequency.QUARTERLY]
    assert freshness(120, s0, ceiling) == 1.0
    assert freshness(170, s0, ceiling) == 1.0
    # And it still falls away once genuinely late.
    assert freshness(213, s0, ceiling) < 1.0
    assert freshness(304, s0, ceiling) == pytest.approx(0.0)


def test_a_late_monthly_print_is_still_discounted() -> None:
    """The fix must not simply switch the ramp off."""
    _, _, s0, ceiling = ADR_TABLE[Frequency.MONTHLY]
    assert freshness(s0, s0, ceiling) == 1.0
    assert 0.0 < freshness(s0 + 15, s0, ceiling) < 1.0
    assert freshness(ceiling + 1, s0, ceiling) == 0.0


def test_freshness_reads_no_config_and_no_registry() -> None:
    """It is arithmetic now. Both bounds arrive as arguments, so a caller
    cannot get one from the leg and the other from a global."""
    import inspect

    parameters = list(inspect.signature(freshness).parameters)
    assert parameters == ["staleness_days", "full_days", "allowance_days"]


def test_a_full_weight_age_at_or_above_the_allowance_is_refused() -> None:
    """The two bounds have to be ordered or the ramp divides by zero or
    inverts, and an inverted ramp gives an expired series full weight."""
    with pytest.raises(ValueError, match="full_days"):
        freshness(10, 107, 107)
    with pytest.raises(ValueError, match="full_days"):
        freshness(10, 200, 107)


def test_a_non_positive_allowance_is_refused() -> None:
    with pytest.raises(ValueError, match="allowance_days"):
        freshness(10, 0, 0)


# --- resolution per leg -----------------------------------------------------


def _obs(
    indicator: str,
    currency: str,
    period: date,
    frequency: Frequency,
    value: float = 1.0,
) -> Observation:
    ref = series_for(indicator, currency)
    return Observation(
        indicator=indicator,
        currency=currency,
        period=period,
        value=value,
        unit="percent",
        source=ref.source if ref is not None else "fred",
        series_id=ref.series_id if ref is not None else "TEST",
        frequency=frequency,
    )


class _OnePillar(BasePillar):
    """A pillar over one component and one indicator, so the factor reported
    is that leg's and nothing is averaged into it."""

    name = INDICATORS["cpi_yoy"].pillar
    requires = ("cpi_yoy",)
    component_indicators = {"cpi_gap": ("cpi_yoy",)}
    component_weights = {"cpi_gap": 1.0}

    def _transform(self, extracted, asof):  # type: ignore[no-untyped-def]
        raise NotImplementedError("not used; component_freshness is called directly")


def _factor(currency: str, age_days: int, frequency: Frequency) -> float:
    asof = date(2026, 9, 21)
    pillar = _OnePillar(CONFIG)
    extracted = {
        "cpi_yoy": [
            _obs("cpi_yoy", currency, asof - timedelta(days=age_days), frequency)
        ]
    }
    return pillar.component_freshness(extracted, asof)["cpi_gap"]


def test_a_punctual_quarterly_leg_and_a_punctual_monthly_leg_agree() -> None:
    """The cross-sectional assertion, on the quantity that carries the defect.

    #126's criterion 4 asked for this on the blended sub-weight, where the
    factor has already cancelled out: `blend_components` renormalises over the
    sub-weight present, and INFLATION's two components share one release, so
    the discount divides out and the assertion passes either way. The factor
    itself is where the difference lives.
    """
    aud = _factor("AUD", 120, Frequency.QUARTERLY)
    usd = _factor("USD", 45, Frequency.MONTHLY)

    assert aud == 1.0
    assert usd == 1.0
    assert aud == usd


def test_the_leg_frequency_wins_over_the_indicator_frequency() -> None:
    """``cpi_yoy`` is monthly on its spec and quarterly for AUD and NZD. A rule
    keyed on the spec hands AUD's quarterly print a monthly ramp and calls a
    punctual print 44 days late. 36 legs in the registry differ this way."""
    assert INDICATORS["cpi_yoy"].frequency is Frequency.MONTHLY
    aud_ref = series_for("cpi_yoy", "AUD")
    assert aud_ref is not None
    assert aud_ref.frequency is Frequency.QUARTERLY

    assert _factor("AUD", 120, Frequency.QUARTERLY) == 1.0
    # The same age read as monthly is past even the monthly ceiling of 107.
    assert _factor("AUD", 120, Frequency.MONTHLY) == 0.0


def test_a_measured_lag_shifts_that_legs_ramp_and_no_other() -> None:
    """A FRED leg keyed by #222 publishes months after its cadence suggests.
    Its punctual print must be full weight at that later age, while a leg on
    the default lag is already past its own ceiling there."""
    slow = series_for("trade_balance", "CHF")
    assert slow is not None
    assert slow.publication_lag_days == 258

    lag = publication_lag(slow, Frequency.MONTHLY)
    cycle = CYCLE_DAYS[Frequency.MONTHLY]
    assert freshness(lag, lag + cycle, lag + 2 * cycle) == 1.0
    assert freshness(lag, 76, 107) == 0.0


def test_a_component_is_as_stale_as_its_stalest_input() -> None:
    """Unchanged by this issue, and pinned because the resolution moved."""
    asof = date(2026, 9, 21)

    class _TwoInputs(_OnePillar):
        requires = ("cpi_yoy", "core_cpi_yoy")
        component_indicators = {"cpi_gap": ("cpi_yoy", "core_cpi_yoy")}

    pillar = _TwoInputs(CONFIG)
    extracted = {
        "cpi_yoy": [
            _obs("cpi_yoy", "USD", asof - timedelta(days=45), Frequency.MONTHLY)
        ],
        "core_cpi_yoy": [
            _obs("core_cpi_yoy", "USD", asof - timedelta(days=95), Frequency.MONTHLY)
        ],
    }
    factors = pillar.component_freshness(extracted, asof)

    # 95 days is past s0 = 76 and inside S = 107: (107 - 95) / (107 - 76).
    assert factors["cpi_gap"] == pytest.approx(12 / 31)


def test_a_component_with_no_observations_is_absent_not_zero() -> None:
    """An absent component lowers the sub-weight held, which is what the
    component floor judges. A component at 0.0 is a series that exists and has
    run late. Different facts, and the mapping keeps them apart."""
    asof = date(2026, 9, 21)
    pillar = _OnePillar(CONFIG)

    assert pillar.component_freshness({"cpi_yoy": []}, asof) == {}


# --- the registry and the scorer agree --------------------------------------


def test_no_registered_leg_is_born_stale() -> None:
    """The registry walk, restated on the derived bounds. A leg admitted by the
    visibility rule at its lag must carry weight at that age, or it can never
    contribute to a score at all."""
    born_stale = [
        f"{key} {currency}"
        for key, spec in INDICATORS.items()
        for currency, ref in spec.series.items()
        if freshness(
            publication_lag(ref, ref.frequency),
            publication_lag(ref, ref.frequency) + CYCLE_DAYS[ref.frequency],
            publication_lag(ref, ref.frequency) + 2 * CYCLE_DAYS[ref.frequency],
        )
        < 1.0
    ]
    assert born_stale == []


def test_no_registered_leg_reaches_zero_before_its_next_print_is_due() -> None:
    """Nine indicators did, on punctual data, because a hand-keyed allowance
    sat below the oldest age a punctual print reaches. Derivation removes the
    class of defect rather than the nine instances."""
    early_zero = [
        f"{key} {currency}"
        for key, spec in INDICATORS.items()
        for currency, ref in spec.series.items()
        if freshness(
            publication_lag(ref, ref.frequency) + CYCLE_DAYS[ref.frequency],
            publication_lag(ref, ref.frequency) + CYCLE_DAYS[ref.frequency],
            publication_lag(ref, ref.frequency) + 2 * CYCLE_DAYS[ref.frequency],
        )
        <= 0.0
    ]
    assert early_zero == []


def test_the_registry_and_the_ramp_give_one_answer_about_one_series() -> None:
    """``stale_on`` compares ``age > allowance`` and the ramp reaches zero at
    the allowance, so the last day the registry counts a ref is the first day
    the ramp gives it nothing. Neither side may be the more permissive."""
    for key in ("cpi_yoy", "trade_balance", "yield_2y"):
        for currency, ref in INDICATORS[key].series.items():
            lag = publication_lag(ref, ref.frequency)
            cycle = CYCLE_DAYS[ref.frequency]
            s0, ceiling = lag + cycle, lag + 2 * cycle
            if ref.last_observed is None:
                continue
            for age in (s0, ceiling - 1, ceiling, ceiling + 1):
                factor = freshness(age, s0, ceiling)
                stale = ref.stale_on(ref.last_observed + timedelta(days=age), ceiling)
                if factor > 0.0:
                    assert not stale, f"{key} {currency} at {age} days"
                if stale:
                    assert factor == 0.0, f"{key} {currency} at {age} days"


def test_the_removed_config_field_is_gone_rather_than_unread() -> None:
    """A configured number nothing reads is the config-drift defect in
    reverse: an operator sets it, the run ignores it, and nothing says so."""
    assert not hasattr(ScoringConfig(), "staleness_full_days")


def test_the_surviving_config_ceiling_is_only_the_absent_pillar_sentinel() -> None:
    """``max_staleness_days`` stays, read by the sentinel that marks a pillar
    with no data at all. It no longer bounds any ramp, and #223 carries its
    retirement with the sentinel itself.

    The field's own docstring is read from the source, because an attribute
    docstring is not on the class at runtime and this is a claim about what
    the next reader is told.
    """
    import inspect

    import fbe.config as config_module

    assert ScoringConfig().max_staleness_days > 0
    source = inspect.getsource(config_module)
    field_doc = source.split("max_staleness_days: int = 45", 1)[1][:900]
    assert "Sentinel age" in field_doc
    assert "bounds no ramp" in field_doc


def test_validate_no_longer_judges_the_removed_ordering() -> None:
    """The ramp's two ages are derived per leg and are never configured, so
    there is no ordering left here to get wrong. The defaults validate clean
    and nothing mentions the removed field."""
    import inspect

    import fbe.config as config_module

    assert default_config().validate() == []
    assert "staleness_full_days" not in inspect.getsource(config_module)
