"""The sizing chain: `convert_rate`, `pip_value` and `position_size`.

Two rows of the prime directive table in the engineering standards came out of
this file, and both are the same shape: a number that was wrong and looked
right. A missing conversion rate treated as 1.0 sizes roughly eighteen times
too large on a rand account, and rounding up to the lot step breaches the risk
cap by construction. Neither raises, neither fails a naive test, and neither is
visible on the ticket. Most of what is below exists to force those two apart
from a correct answer.

Rates used throughout are the ones ``docs/risk-and-execution.md`` section 2
publishes, so the worked examples in that document are fixtures here rather
than prose. Where a test asserts a figure the document states, the docstring
says so and quotes it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fbe.bias import UNCHECKED_SUFFIX
from fbe.config import RiskConfig
from fbe.risk import (
    DEFAULT_BROKER,
    Broker,
    MissingRateError,
    _round_down_to_step,
    convert_rate,
    pip_value,
    position_size,
)
from fbe.types import PositionSize

# The three rates docs/risk-and-execution.md section 2 works its examples from.
USDZAR = 18.50
USDJPY = 155.00
GBPUSD = 1.2500

RATES: dict[str, float] = {"USDZAR": USDZAR, "USDJPY": USDJPY, "GBPUSD": GBPUSD}

# Rand per yen and rand per pound, both derived through the dollar pivot.
JPY_ZAR = USDZAR / USDJPY  # 0.119355
GBP_ZAR = GBPUSD * USDZAR  # 23.125

MICRO_BROKER = Broker(
    name="test-micro",
    min_lot=0.01,
    lot_step=0.01,
    contract_size=100_000.0,
    max_lot=50.0,
    typical_spread_pips=dict(DEFAULT_BROKER.typical_spread_pips),
)
"""0.01 minimum and 0.01 step, the common retail floor.

Declared here rather than aliased to `DEFAULT_BROKER`. That constant's own
docstring tells the owner to replace every value in it, and if they do and
their broker turns out to offer nano lots, five tests below stop asserting what
their names say: the two refusals become sizeable trades and both example B
figures move. The fixtures a test reasons about have to be fixed by the test.
"""

NANO_BROKER = Broker(
    name="test-nano",
    min_lot=0.001,
    lot_step=0.001,
    contract_size=100_000.0,
    max_lot=50.0,
    typical_spread_pips=dict(DEFAULT_BROKER.typical_spread_pips),
)
"""0.001 minimum and 0.001 step. Section 2 sizes both examples at this broker
as well, and on a R2,000 account it is the difference between several G10
crosses being tradeable and being untradeable."""


@pytest.fixture
def config() -> RiskConfig:
    """The plan's own account: R2,000 in rand, risking 1-2%."""
    return RiskConfig()


# --------------------------------------------------------------------------
# convert_rate
# --------------------------------------------------------------------------


def test_identity_returns_one_without_consulting_the_rates() -> None:
    """Rule 0. The only circumstance in which this module produces 1.0.

    The mapping raises on any lookup, so a factor of 1.0 arrived at by reading
    a rate rather than by identity fails here instead of passing quietly.
    """

    class Explosive(dict[str, float]):
        def __missing__(self, key: str) -> float:
            raise AssertionError(f"identity must not look up {key!r}")

        def __contains__(self, key: object) -> bool:
            raise AssertionError(f"identity must not look up {key!r}")

    assert convert_rate("ZAR", "ZAR", Explosive()) == 1.0


def test_a_direct_rate_is_returned_as_quoted() -> None:
    """Rule 1. USDZAR is rand per dollar, so USD to ZAR is the rate itself."""
    assert convert_rate("USD", "ZAR", RATES) == pytest.approx(18.50)


def test_an_inverted_rate_is_reciprocated() -> None:
    """Rule 2. ZAR to USD is 1/18.50, read off the same USDZAR quote.

    Returning 18.50 here instead would overstate a rand amount converted to
    dollars by the square of the rate.
    """
    assert convert_rate("ZAR", "USD", RATES) == pytest.approx(1.0 / 18.50)


def test_the_dollar_pivot_resolves_yen_to_rand() -> None:
    """Rule 3, the worked example in section 1.

    ``{"USDZAR": 18.50, "USDJPY": 155.00}`` carries no JPYZAR and no ZARJPY.
    The document publishes 0.119355 rand per yen and that is what this asserts.
    The first hop is inverted (1/USDJPY) and the second is direct.
    """
    assert convert_rate("JPY", "ZAR", RATES) == pytest.approx(0.119355, abs=1e-6)


def test_the_dollar_pivot_also_resolves_a_direct_first_hop() -> None:
    """Rule 3 where the first hop is direct rather than inverted.

    GBPUSD is quoted base first, so GBP to USD is the rate as is and only the
    second hop is a lookup of USDZAR. Exercising both pivot shapes matters
    because a pivot that inverts unconditionally passes the yen test and fails
    here.
    """
    assert convert_rate("GBP", "ZAR", RATES) == pytest.approx(23.125)


def test_a_direct_quote_beats_the_pivot_route() -> None:
    """Resolution order is first match wins, and the direct form is first.

    The pivot route here would give 23.125 and the direct quote says 22.00.
    A resolver that tries the pivot first, or that averages, returns the wrong
    one of the two.
    """
    rates = dict(RATES) | {"GBPZAR": 22.00}
    assert convert_rate("GBP", "ZAR", rates) == pytest.approx(22.00)


def test_an_inverted_quote_beats_the_pivot_route() -> None:
    """Rule 2 is still ahead of rule 3, with the same reasoning."""
    rates = dict(RATES) | {"ZARGBP": 1 / 22.00}
    assert convert_rate("GBP", "ZAR", rates) == pytest.approx(22.00)


def test_no_route_raises_and_the_message_carries_what_was_tried() -> None:
    """The failure has to be actionable without opening this file.

    Both currencies, the pivot tried and the keys looked for, so the reader can
    see which quote to add to the feed.
    """
    with pytest.raises(MissingRateError) as excinfo:
        convert_rate("CHF", "ZAR", {"USDJPY": USDJPY})
    message = str(excinfo.value)
    for expected in ("CHF", "ZAR", "USD", "CHFZAR", "ZARCHF", "CHFUSD", "USDZAR"):
        assert expected in message, f"{expected!r} missing from {message!r}"


def test_no_second_pivot_is_attempted() -> None:
    """A route needing two hops through two currencies is refused.

    CHF to ZAR via EUR via USD is constructible from this mapping. Widening the
    search until something resolves is how a rate set unfit for sizing starts
    producing numbers.
    """
    with pytest.raises(MissingRateError):
        convert_rate("CHF", "ZAR", {"EURCHF": 0.95, "EURUSD": 1.0850, "USDZAR": USDZAR})


def test_missing_rate_error_is_not_a_key_error() -> None:
    """Its own type so a stray ``except KeyError`` cannot swallow it.

    It stays a ``LookupError`` so an existing broad handler still catches it,
    but catching ``KeyError`` specifically must not.
    """
    assert issubclass(MissingRateError, LookupError)
    assert not issubclass(MissingRateError, KeyError)
    with pytest.raises(MissingRateError):
        try:
            convert_rate("CHF", "ZAR", {})
        except KeyError as exc:  # pragma: no cover - the assert is the point
            raise AssertionError("MissingRateError was caught as a KeyError") from exc


@pytest.mark.parametrize("corrupt", [0.0, -18.50, float("nan"), float("inf")])
def test_a_corrupt_rate_on_the_route_raises_rather_than_propagating(
    corrupt: float,
) -> None:
    """A non-positive or non-finite rate is corrupt data, not a small number.

    Inverting zero raises somewhere unhelpful, inverting a negative silently
    flips the sign of every money figure downstream, and a NaN propagates to a
    position size that compares false against every limit it is checked
    against.
    """
    with pytest.raises(MissingRateError):
        convert_rate("USD", "ZAR", {"USDZAR": corrupt})


def test_a_corrupt_rate_on_the_second_hop_raises() -> None:
    """The check covers every leg of the route, not just the first one."""
    with pytest.raises(MissingRateError):
        convert_rate("JPY", "ZAR", {"USDJPY": USDJPY, "USDZAR": 0.0})


def test_a_quoted_rate_of_exactly_one_is_refused_as_a_placeholder() -> None:
    """The eighteenfold bug wearing a value instead of an absence.

    Every other guard in this module fires on a rate being missing. A rates
    mapping initialised with ones, which is what an unpopulated manual YAML or
    a hand-built dict looks like, is not missing anything: it resolves, and it
    resolves to the one factor that is always wrong between two different
    currencies.

    On the plan's own account the consequence is R370 at risk against a R20
    budget, 18.5% of the balance, reported as R20.00 on a ticket carrying no
    warnings. That is the prime directive table's second row reproduced
    exactly.
    """
    with pytest.raises(MissingRateError, match="placeholder"):
        convert_rate("USD", "ZAR", {"USDZAR": 1.0})
    with pytest.raises(MissingRateError, match="placeholder"):
        convert_rate("ZAR", "USD", {"USDZAR": 1.0})


def test_a_placeholder_rate_is_refused_all_the_way_up_through_sizing(
    config: RiskConfig,
) -> None:
    """The refusal has to reach the caller, not be caught and warned about.

    Sizing cannot continue without knowing what a pip costs, so this raises
    rather than returning a warning-carrying zero result.
    """
    with pytest.raises(MissingRateError):
        position_size(
            "EURUSD",
            1.0850,
            1.0825,
            config,
            {"USDZAR": 1.0},
            risk_fraction=0.01,
            broker=NANO_BROKER,
        )


def test_identity_is_still_one_after_the_placeholder_rule() -> None:
    """The rule must not break the one legitimate 1.0.

    Converting a currency to itself is not a quote and never reads `rates`, so
    it is unaffected.
    """
    assert convert_rate("ZAR", "ZAR", {}) == 1.0
    assert pip_value("EURZAR", 1.0, "ZAR", {}) == pytest.approx(0.0001)


def test_a_pivot_product_of_exactly_one_is_arithmetic_and_allowed() -> None:
    """Only a QUOTED 1.0 is refused, never a computed one.

    ``1.25 x 0.8`` is exactly 1.0 in binary floating point, so a route built
    from two legs that are each plainly real can land on 1.0 by arithmetic.
    Refusing that would reject a genuine rate set. The rates here are chosen
    to make the product exact, not because 0.8 is a plausible USDZAR.
    """
    assert convert_rate("GBP", "ZAR", {"GBPUSD": 1.25, "USDZAR": 0.8}) == 1.0


def test_a_boolean_rate_is_refused_rather_than_read_as_one() -> None:
    """``USDZAR: yes`` in a manual YAML parses as ``True``.

    ``isfinite(True)`` is True and ``True <= 0.0`` is False, so a bool passes
    both numeric guards and lands as a factor of 1.0, which is finding one all
    over again. It type-checks too, because ``bool`` is a subtype of ``int``.
    """
    with pytest.raises(MissingRateError, match="boolean"):
        convert_rate("USD", "ZAR", {"USDZAR": True})


def test_an_integer_rate_comes_back_as_a_float() -> None:
    """The declared return type has to hold at runtime, not just to mypy."""
    assert isinstance(convert_rate("USD", "ZAR", {"USDZAR": 18}), float)


def test_a_pivot_product_that_is_no_longer_usable_raises() -> None:
    """Two legs that are each finite and positive can still multiply to zero.

    JPY to USD resolves to 1e-200 and USD to ZAR to 1e-200, and their product
    underflows. A zero factor divides inside `position_size`, and an infinite
    one is reported as a size below the broker minimum, which blames the lot
    step for what is actually a corrupt rate. The documented return is a
    strictly positive multiplier, so the product is checked as well as the
    legs.
    """
    with pytest.raises(MissingRateError):
        convert_rate("JPY", "ZAR", {"USDZAR": 1e-200, "USDJPY": 1e200})


def test_an_inverted_leg_that_overflows_raises() -> None:
    """Reciprocating a subnormal gives infinity, which is not a rate."""
    with pytest.raises(MissingRateError):
        convert_rate("USD", "ZAR", {"ZARUSD": 1e-320})


# --------------------------------------------------------------------------
# pip_value
# --------------------------------------------------------------------------


def test_pip_value_for_a_dollar_quoted_pair() -> None:
    """Case 3 with a direct conversion. Section 2 example A publishes R0.00185.

    One pip on one unit of EURUSD is 0.0001 USD, carried to rand at USDZAR.
    """
    assert pip_value("EURUSD", 1.0, "ZAR", RATES) == pytest.approx(0.00185)


def test_pip_value_for_a_yen_pair_uses_the_two_decimal_pip() -> None:
    """Section 2 example B publishes R0.00119355 per pip per unit.

    A JPY pip is 0.01, not 0.0001. Using the standard pip size here would
    understate pip value a hundredfold and size the position a hundred times
    too large.

    The tolerance is half of the last published digit: the document rounds
    0.0011935483870967741 to eight decimal places and this asserts the figure
    reproduces at exactly that precision, not to a precision it never claimed.
    """
    assert pip_value("USDJPY", 1.0, "ZAR", RATES) == pytest.approx(0.00119355, abs=5e-9)


def test_pip_value_for_a_cross_pivots_on_the_quote_currency() -> None:
    """EURGBP is quoted in pounds, so the conversion leg is GBP to ZAR.

    A pip value that converted the BASE currency instead would read 0.0001 x
    EURZAR here and be wrong by the EURGBP rate, which is close enough to one
    to look plausible.
    """
    assert pip_value("EURGBP", 1.0, "ZAR", RATES) == pytest.approx(0.0001 * 23.125)


def test_pip_value_scales_linearly_with_units() -> None:
    """The whole sizing solve depends on this being linear in units."""
    one = pip_value("EURUSD", 1.0, "ZAR", RATES)
    assert pip_value("EURUSD", 2500.0, "ZAR", RATES) == pytest.approx(2500.0 * one)


def test_pip_value_needs_no_rate_when_the_quote_is_the_account_currency() -> None:
    """Case 1. A rand-quoted pair converts by identity, so the mapping is unused."""
    assert pip_value("EURZAR", 1.0, "ZAR", {}) == pytest.approx(0.0001)


def test_pip_value_rejects_negative_units() -> None:
    """Direction is carried by entry and stop, never by the sign of the size.

    A negative size accepted here returns a negative pip value, which divides
    into a negative unit count and reads as a short.
    """
    with pytest.raises(ValueError):
        pip_value("EURUSD", -1.0, "ZAR", RATES)


def test_pip_value_rejects_a_malformed_pair() -> None:
    """Guards an upstream contract rather than anything in this module.

    The ``ValueError`` comes from `fbe.universe.split_pair`, so no change to
    the sizing chain can break this. It is here because a five-character pair
    reaching the sizing path at all would mean a caller had built a pair string
    by hand, and the next question after that is whether it was built in market
    order.
    """
    with pytest.raises(ValueError):
        pip_value("EUR", 1.0, "ZAR", RATES)


def test_pip_value_propagates_the_missing_rate() -> None:
    """No route to the account currency means no pip value, not a guess."""
    with pytest.raises(MissingRateError):
        pip_value("USDCHF", 1.0, "ZAR", {"USDJPY": USDJPY})


# --------------------------------------------------------------------------
# position_size, the three worked examples
# --------------------------------------------------------------------------


def test_worked_example_a_eurusd_at_a_nano_broker(config: RiskConfig) -> None:
    """Section 2 example A, every published figure asserted.

    EURUSD long, entry 1.0850, stop 1.0825, 1% of R2,000. The document
    publishes 25.0 pips, 432.4 units before rounding, 400 units after rounding
    down at a 0.001 step, realised risk R18.50 against an intended R20.00, and
    a notional of R8,029.00.
    """
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    assert size.stop_distance_pips == pytest.approx(25.0)
    assert size.risk_amount == pytest.approx(20.00)
    assert size.units == pytest.approx(400.0)
    assert size.lots == pytest.approx(0.004)
    assert size.realised_risk_amount == pytest.approx(18.50)
    assert size.notional == pytest.approx(8029.00)
    assert size.account_currency == "ZAR"


def test_worked_example_a_is_refused_at_a_micro_broker(config: RiskConfig) -> None:
    """The same setup at a 0.01 minimum cannot be expressed, so it is refused.

    Section 2 is explicit that the answer is not "round up to the minimum": at
    1,000 units the same 25 pip stop costs R46.25, which is 2.3% of the account
    and outside the band entirely. Zero units, zero lots, zero realised risk,
    and a warning saying so.
    """
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.01,
        broker=MICRO_BROKER,
    )
    assert size.units == 0.0
    assert size.lots == 0.0
    assert size.realised_risk_amount == 0.0
    assert size.notional == 0.0
    assert any("refusing to size" in warning for warning in size.warnings)


def test_worked_example_b_usdjpy_at_a_micro_broker(config: RiskConfig) -> None:
    """Section 2 example B, every published figure asserted.

    USDJPY short, entry 155.00, stop 155.30, 2% of R2,000. Published: 30.0
    pips, 1,117.1 units before rounding, 1,000 after, realised R35.81 against
    an intended R40.00, notional R18,500.00.

    Note the stop is ABOVE the entry, because this is a short. The distance is
    the same quantity either way.
    """
    size = position_size(
        "USDJPY",
        155.00,
        155.30,
        config,
        RATES,
        risk_fraction=0.02,
        broker=MICRO_BROKER,
    )
    assert size.stop_distance_pips == pytest.approx(30.0)
    assert size.risk_amount == pytest.approx(40.00)
    assert size.units == pytest.approx(1000.0)
    assert size.lots == pytest.approx(0.01)
    assert size.realised_risk_amount == pytest.approx(35.81, abs=0.005)
    assert size.notional == pytest.approx(18500.00)


def test_worked_example_b_at_a_nano_broker(config: RiskConfig) -> None:
    """Section 2's closing line on example B: 1,100 units, R39.39, R20,350.00.

    The finer step keeps more of the intended risk, which is the whole reason
    the document mentions nano lots at all.
    """
    size = position_size(
        "USDJPY",
        155.00,
        155.30,
        config,
        RATES,
        risk_fraction=0.02,
        broker=NANO_BROKER,
    )
    assert size.units == pytest.approx(1100.0)
    assert size.lots == pytest.approx(0.011)
    assert size.realised_risk_amount == pytest.approx(39.39, abs=0.005)
    assert size.notional == pytest.approx(20350.00)


def test_worked_example_c_a_cross_with_no_dollar_leg(config: RiskConfig) -> None:
    """A cross, computed by hand here because section 2 publishes only two.

    EURGBP long, entry 0.8500, stop 0.8460, 1% of R2,000, at the nano broker.

        stop distance  = 0.0040 / 0.0001               = 40.0 pips
        GBP to ZAR     = 1.2500 x 18.50                = 23.125
        pip per unit   = 0.0001 x 23.125               = R0.0023125
        units          = 20.00 / (40.0 x 0.0023125)    = 216.2
        lots           = 216.2 / 100,000               = 0.0021621
        rounded down   = 0.002 lots                    = 200 units
        realised risk  = 200 x 0.0023125 x 40          = R18.50
        notional       = 200 x 0.8500 x 23.125         = R3,931.25

    This is the case neither published example covers: the conversion leg is a
    pivot whose first hop is direct, and the account currency appears in
    neither leg of the pair nor in the pivot's first hop.
    """
    size = position_size(
        "EURGBP",
        0.8500,
        0.8460,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    assert size.stop_distance_pips == pytest.approx(40.0)
    assert size.units == pytest.approx(200.0)
    assert size.lots == pytest.approx(0.002)
    assert size.realised_risk_amount == pytest.approx(18.50)
    assert size.notional == pytest.approx(3931.25)


# --------------------------------------------------------------------------
# position_size, the rounding rule
# --------------------------------------------------------------------------


def test_rounding_is_down_and_not_to_nearest(config: RiskConfig) -> None:
    """A size nearest-rounding would take UP, asserted to have gone down.

    Both published examples are cases the two rules agree on, so neither can
    tell them apart. Example B solves to 0.011171 lots, which against a 0.007
    step is 1.596 steps: rounding down gives 0.007 and nearest gives 0.014.

    The difference is not cosmetic. At 1,400 units the same 30 pip stop costs
    1,400 x R0.00119355 x 30 = R50.13, which is 2.5% of a R2,000 account and
    outside the plan's ceiling, reached by a rounding rule rather than by any
    decision anyone made.
    """
    broker = Broker(
        name="test-coarse",
        min_lot=0.007,
        lot_step=0.007,
        contract_size=100_000.0,
        max_lot=50.0,
        typical_spread_pips=dict(DEFAULT_BROKER.typical_spread_pips),
    )
    size = position_size(
        "USDJPY", 155.00, 155.30, config, RATES, risk_fraction=0.02, broker=broker
    )
    assert size.lots == pytest.approx(0.007)
    assert size.units == pytest.approx(700.0)
    assert size.realised_risk_amount < size.risk_amount


def test_rounding_never_exceeds_the_intended_risk(config: RiskConfig) -> None:
    """The invariant behind the rule, asserted across a sweep of stop widths.

    Any stop distance, any account, the realised figure is at or below the
    intended one. One rounding-up bug anywhere in the chain breaks this for
    some input, and a sweep finds it where a single worked example might not.
    """
    for tenths in range(1, 120):
        stop = round(1.0850 - tenths * 0.0001, 6)
        size = position_size(
            "EURUSD",
            1.0850,
            stop,
            config,
            RATES,
            risk_fraction=0.02,
            broker=NANO_BROKER,
        )
        assert size.realised_risk_amount <= size.risk_amount + 1e-9, (
            f"stop {stop} risked {size.realised_risk_amount} "
            f"against an intended {size.risk_amount}"
        )


def test_a_size_landing_exactly_on_a_lot_step_keeps_that_step(
    config: RiskConfig,
) -> None:
    """A solve that lands on an exact lot step keeps that step.

    The balance is chosen so the solve lands on 0.29 lots: 29,000 units x 25
    pips x R0.00185 = R1,341.25, which is 1% of R134,125.00. This rounds down
    like every other size and comes back the size it started as.

    Note what this does NOT prove. The solve produces 0.2900000000000062, not
    the double nearest to 0.29, and a naive float floor handles that value
    correctly. The floating point hazard `_round_down_to_step` guards is pinned
    where it is reachable, on that function directly, in the test below.
    """
    rich = RiskConfig(account_balance=134_125.00)
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        rich,
        RATES,
        risk_fraction=0.01,
        broker=MICRO_BROKER,
    )
    assert size.lots == pytest.approx(0.29)
    assert size.units == pytest.approx(29_000.0)
    assert size.realised_risk_amount == pytest.approx(size.risk_amount)


def test_the_lot_step_rounding_survives_binary_floating_point() -> None:
    """The mechanism, pinned on the helper where it is actually reachable.

    ``0.29 / 0.01`` evaluates to 28.999999999999996 in binary floating point,
    so flooring the raw quotient returns 0.28 and quietly drops a whole micro
    lot of a position the account asked for. Fourteen multiples of 0.01 below
    two lots behave this way, and a broker's contract specification is written
    in decimal, so the value a person types is exactly the value that breaks.

    Rounding down is still absolute: a size genuinely below a step boundary
    stays below it and is never nudged up to reach one.
    """
    assert _round_down_to_step(0.29, 0.01) == pytest.approx(0.29)
    assert _round_down_to_step(0.58, 0.01) == pytest.approx(0.58)
    assert _round_down_to_step(0.2899999, 0.01) == pytest.approx(0.28)
    assert _round_down_to_step(0.011171171171171172, 0.01) == pytest.approx(0.01)


def test_a_minimum_that_is_not_a_multiple_of_the_step_still_refuses(
    config: RiskConfig,
) -> None:
    """The minimum is checked against the rounded size, not the raw solve.

    Example B solves to 0.011171 lots, which is above this broker's 0.01
    minimum but rounds down to zero at its 0.02 step. Checking the raw figure
    would pass the minimum test and then return a zero-unit position with no
    warning saying why, which reads as a trade the engine simply declined to
    describe.

    Every other broker in this file has ``min_lot == lot_step``, and under that
    condition the two readings agree exactly, so this is the only fixture that
    can tell them apart.
    """
    odd = Broker(
        name="test-coarse-step",
        min_lot=0.01,
        lot_step=0.02,
        contract_size=100_000.0,
        max_lot=50.0,
        typical_spread_pips=dict(DEFAULT_BROKER.typical_spread_pips),
    )
    size = position_size(
        "USDJPY", 155.00, 155.30, config, RATES, risk_fraction=0.02, broker=odd
    )
    assert size.units == 0.0
    assert size.realised_risk_amount == 0.0
    assert any("refusing to size" in warning for warning in size.warnings), (
        size.warnings
    )


def test_a_zero_stop_distance_still_raises_when_the_rate_is_missing(
    config: RiskConfig,
) -> None:
    """Ordering: the conversion resolves before the zero-distance return.

    Both are failures, but they are not the same failure. A zero stop distance
    is a fact about the trade the caller passed in, and the caller gets it back
    as a warning so a batch over a shortlist keeps going. A missing rate means
    the engine does not know what any trade on this pair would cost, and that
    has to raise whatever else is wrong with the row.

    Returning the warning here instead would report a soft failure on a pair
    the engine cannot size at all, and the next valid row for that pair would
    then raise, from a code path the caller has already seen succeed.
    """
    with pytest.raises(MissingRateError):
        position_size(
            "USDCHF",
            0.9000,
            0.9000,
            config,
            {"USDJPY": USDJPY},
            risk_fraction=0.01,
            broker=NANO_BROKER,
        )


def test_units_always_agree_with_the_rounded_lots(config: RiskConfig) -> None:
    """Units are recomputed from the rounded lots, never left at the raw solve.

    Reporting the unrounded units beside the rounded lots is the same defect as
    rounding up: the ticket says one thing and the risk figures another.
    """
    size = position_size(
        "USDJPY",
        155.00,
        155.30,
        config,
        RATES,
        risk_fraction=0.02,
        broker=MICRO_BROKER,
    )
    assert size.units == pytest.approx(size.lots * MICRO_BROKER.contract_size)


# --------------------------------------------------------------------------
# position_size, intended against realised
# --------------------------------------------------------------------------


def test_realised_risk_is_a_separate_field_and_the_smaller_one(
    config: RiskConfig,
) -> None:
    """Example B's two figures, R40.00 intended and R35.81 realised.

    Collapsing them into one field is what would overstate every R-multiple in
    the journal by the rounding ratio, about 10.5% on this trade, and those
    R-multiples are the only evidence the conviction model is judged on.
    """
    size = position_size(
        "USDJPY",
        155.00,
        155.30,
        config,
        RATES,
        risk_fraction=0.02,
        broker=MICRO_BROKER,
    )
    assert size.risk_amount == pytest.approx(40.00)
    assert size.realised_risk_amount == pytest.approx(35.81, abs=0.005)
    assert size.realised_risk_amount < size.risk_amount


def test_realised_risk_is_recomputed_from_the_rounded_units(
    config: RiskConfig,
) -> None:
    """Independent recomputation, so the assertion is not the implementation.

    Multiplying the reported units by the pip value and the stop distance has
    to give back the reported realised risk. A realised figure derived from the
    intended one by a ratio, rather than from the units actually being traded,
    passes the inequality above and fails here.
    """
    size = position_size(
        "EURGBP",
        0.8500,
        0.8460,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    expected = (
        size.units * pip_value("EURGBP", 1.0, "ZAR", RATES) * (size.stop_distance_pips)
    )
    assert size.realised_risk_amount == pytest.approx(expected)


# --------------------------------------------------------------------------
# position_size, notional
# --------------------------------------------------------------------------


def test_notional_is_carried_back_to_the_account_currency(
    config: RiskConfig,
) -> None:
    """Face value in rand, not in the quote currency.

    400 units of EURUSD at 1.0850 is USD 434.00. Reporting that as the notional
    on a rand ticket understates the position by the USDZAR rate, roughly
    eighteenfold, in the single number shown for leverage awareness. Section 2
    publishes R8,029.00.
    """
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    quote_currency_face_value = 400.0 * 1.0850
    assert size.notional == pytest.approx(8029.00)
    assert size.notional != pytest.approx(quote_currency_face_value)


def test_notional_for_a_yen_pair_uses_the_same_leg_as_the_risk_figures(
    config: RiskConfig,
) -> None:
    """Section 2 publishes R18,500.00 for 1,000 units of USDJPY at 155.00.

    Left in yen it would have printed 155,000 on a ticket whose money fields
    are all supposed to be rand, and 155,000 is a larger number than the
    correct answer, so the error does not even read as an understatement.
    """
    size = position_size(
        "USDJPY",
        155.00,
        155.30,
        config,
        RATES,
        risk_fraction=0.02,
        broker=MICRO_BROKER,
    )
    assert size.notional == pytest.approx(18500.00)
    assert size.notional != pytest.approx(155_000.0)


# --------------------------------------------------------------------------
# position_size, the risk fraction band
# --------------------------------------------------------------------------


def test_the_risk_fraction_defaults_to_the_bottom_of_the_band(
    config: RiskConfig,
) -> None:
    """Conservative reading of the plan's 1-2%: 1% when the caller says nothing."""
    size = position_size("EURUSD", 1.0850, 1.0825, config, RATES, broker=NANO_BROKER)
    assert size.risk_fraction == pytest.approx(config.risk_per_trade_min)
    assert size.risk_amount == pytest.approx(20.00)
    assert not size.warnings


def test_a_fraction_above_the_band_is_clamped_and_warned(config: RiskConfig) -> None:
    """Honouring 5% would risk R100 on a R2,000 account against a 2% ceiling.

    Clamped, not honoured, and the warning says so rather than the result
    silently disagreeing with what was asked for.
    """
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.05,
        broker=NANO_BROKER,
    )
    assert size.risk_fraction == pytest.approx(config.risk_per_trade_max)
    assert size.risk_amount == pytest.approx(40.00)
    assert any("0.05" in warning for warning in size.warnings), size.warnings


def test_a_fraction_below_the_band_is_clamped_and_warned(config: RiskConfig) -> None:
    """The floor is clamped too. A 0.1% trade is not what the plan asked for
    either, and silently sizing it wastes the setup."""
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.001,
        broker=NANO_BROKER,
    )
    assert size.risk_fraction == pytest.approx(config.risk_per_trade_min)
    assert size.warnings


def test_a_fraction_inside_the_band_is_left_alone(config: RiskConfig) -> None:
    """1.5%, the MEDIUM rung of the ladder, passes through unchanged and quiet."""
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.015,
        broker=NANO_BROKER,
    )
    assert size.risk_fraction == pytest.approx(0.015)
    assert not size.warnings


# --------------------------------------------------------------------------
# position_size, the warnings
# --------------------------------------------------------------------------


def test_a_size_below_the_broker_minimum_refuses_rather_than_taking_the_minimum(
    config: RiskConfig,
) -> None:
    """The refusal is correct behaviour, not a limitation.

    Taking the minimum lot anyway converts a 1% trade into one of 2.3%, which
    the plan does not permit at any conviction. The warning names the computed
    size and the minimum so the reader can see how far short it fell.
    """
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.01,
        broker=MICRO_BROKER,
    )
    assert size.units == 0.0
    assert any("refusing to size" in warning for warning in size.warnings)


def test_an_entry_equal_to_the_stop_returns_a_zero_size_rather_than_raising(
    config: RiskConfig,
) -> None:
    """A batch over a shortlist must not abort on one bad row.

    The distance is zero, so the solve would divide by zero. A zero-size result
    carrying the warning lets the rest of the shortlist size.
    """
    size = position_size(
        "EURUSD",
        1.0850,
        1.0850,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    assert isinstance(size, PositionSize)
    assert size.units == 0.0
    assert size.lots == 0.0
    assert size.realised_risk_amount == 0.0
    assert size.stop_distance_pips == 0.0
    assert size.warnings


def test_a_stop_inside_twice_the_spread_is_warned(config: RiskConfig) -> None:
    """EURUSD's indicative spread is 0.8 pips, so a 1.0 pip stop is inside the
    noise the broker itself creates.

    The trade still sizes. This is a warning, not a refusal, because the spread
    figure is indicative and the owner may know better than the profile does.
    """
    size = position_size(
        "EURUSD",
        1.0850,
        1.0849,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    assert any("spread" in warning.lower() for warning in size.warnings)


def test_a_comfortable_stop_is_not_warned_about_the_spread(
    config: RiskConfig,
) -> None:
    """25 pips against a 0.8 pip spread is not tight. Warning here would train
    the reader to ignore the warning that matters."""
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    assert not any("spread" in warning.lower() for warning in size.warnings)


def test_an_unknown_spread_is_reported_rather_than_passing_the_check(
    config: RiskConfig,
) -> None:
    """A check that could not run is not a check that passed.

    A broker profile with no entry for the pair leaves the tight-stop check
    unperformed, and saying nothing makes that indistinguishable from a stop
    that cleared it.
    """
    blind = Broker(
        name="test-no-spreads",
        min_lot=0.001,
        lot_step=0.001,
        contract_size=100_000.0,
        max_lot=50.0,
    )
    size = position_size(
        "EURUSD", 1.0850, 1.0825, config, RATES, risk_fraction=0.01, broker=blind
    )
    assert any("spread" in warning.lower() for warning in size.warnings)


def test_a_materially_smaller_trade_than_the_ladder_asked_for_is_warned(
    config: RiskConfig,
) -> None:
    """Rounding that costs most of the intended risk is worth saying out loud.

    A 0.02 lot step against a solve of 0.0112 lots rounds to zero, which is the
    refusal path. A 0.008 step rounds 0.0112 down to 0.008, keeping about 72%
    of the intended risk, so the trade is materially smaller than 2% of the
    account while still being a trade.
    """
    broker = Broker(
        name="test-coarse",
        min_lot=0.008,
        lot_step=0.008,
        contract_size=100_000.0,
        max_lot=50.0,
        typical_spread_pips=dict(DEFAULT_BROKER.typical_spread_pips),
    )
    size = position_size(
        "USDJPY", 155.00, 155.30, config, RATES, risk_fraction=0.02, broker=broker
    )
    assert size.units == pytest.approx(800.0)
    assert any(
        "smaller" in warning.lower() or "shortfall" in warning.lower()
        for warning in size.warnings
    )


def test_the_published_examples_carry_no_shortfall_warning(
    config: RiskConfig,
) -> None:
    """Example B loses 10.5% of its intended risk to rounding and is routine.

    The document presents that gap as the normal consequence of a lot step, not
    as a problem, so the tolerance has to sit above it. A warning on every
    trade is the same as no warning at all.
    """
    size = position_size(
        "USDJPY",
        155.00,
        155.30,
        config,
        RATES,
        risk_fraction=0.02,
        broker=MICRO_BROKER,
    )
    assert not any(
        "smaller" in warning.lower() or "shortfall" in warning.lower()
        for warning in size.warnings
    )


def test_a_missing_conversion_route_propagates(config: RiskConfig) -> None:
    """No rate, no size. This is the eighteenfold bug's only acceptable ending.

    Note it raises rather than returning a warning-carrying zero result: the
    other soft failures are facts about the trade, while this one means the
    engine does not know what the trade would cost.
    """
    with pytest.raises(MissingRateError):
        position_size(
            "USDCHF",
            0.9000,
            0.9030,
            config,
            {"USDJPY": USDJPY},
            risk_fraction=0.01,
            broker=NANO_BROKER,
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0850, 0.0])
def test_a_malformed_price_raises_instead_of_returning_a_pass_shaped_result(
    bad: float, config: RiskConfig
) -> None:
    """Every guard on the sizing path is a ``<`` or an ``==``, and NaN fails both.

    Without an explicit check a NaN entry sails past the zero-distance return,
    past the below-minimum refusal and past the shortfall check, and comes
    back as a `PositionSize` whose every money field is NaN and whose
    ``warnings`` tuple is EMPTY. Section 8 of ``docs/risk-and-execution.md``
    lets the owner tick the sizing box when ``warnings`` is empty, so the
    result would be shaped exactly like one that passed.

    A negative price is the same category: it returns a negative ``notional``
    on a field documented as face value in the account currency, silently.
    """
    with pytest.raises(ValueError):
        position_size(
            "EURUSD",
            bad,
            1.0825,
            config,
            RATES,
            risk_fraction=0.01,
            broker=NANO_BROKER,
        )
    with pytest.raises(ValueError):
        position_size(
            "EURUSD",
            1.0850,
            bad,
            config,
            RATES,
            risk_fraction=0.01,
            broker=NANO_BROKER,
        )


def test_a_non_finite_risk_fraction_raises_rather_than_warning_falsely(
    config: RiskConfig,
) -> None:
    """Clamping a NaN returns a NaN and reports that it clamped it.

    ``min(max(nan, lo), hi)`` is ``nan``, and ``nan != nan`` is True, so the
    clamp warning fires having clamped nothing: "clamped to nan rather than
    honoured". A warning that asserts a correction which did not happen is
    worse than no warning, because the reader acts on it.
    """
    with pytest.raises(ValueError):
        position_size(
            "EURUSD",
            1.0850,
            1.0825,
            config,
            RATES,
            risk_fraction=float("nan"),
            broker=NANO_BROKER,
        )


def test_an_unperformed_check_is_marked_so_a_consumer_can_skip_it(
    config: RiskConfig,
) -> None:
    """The unchecked warning carries the suffix the pair filters already use.

    `DEFAULT_BROKER`'s docstring tells the owner to replace the constant, and
    the natural replacement leaves ``typical_spread_pips`` empty. Every
    correctly sized ticket then carries a warning, ``warnings`` is never
    empty, and a consumer reading "any warning blocks" refuses all 28 pairs.
    That is the failure where a gate that refuses everything gets switched off
    along with the checks that were working.

    The marker is what lets a consumer tell "this check did not run" from
    "this trade has a problem", which is the distinction ADR 0002 rule 4
    requires and `fbe.bias.apply_filters` already implements.
    """
    blind = Broker(
        name="test-no-spreads",
        min_lot=0.001,
        lot_step=0.001,
        contract_size=100_000.0,
        max_lot=50.0,
    )
    size = position_size(
        "EURUSD", 1.0850, 1.0825, config, RATES, risk_fraction=0.01, broker=blind
    )
    unchecked = [w for w in size.warnings if UNCHECKED_SUFFIX in w]
    assert len(unchecked) == 1
    assert unchecked[0].startswith(f"spread{UNCHECKED_SUFFIX}")

    # A real finding must NOT carry the marker, or the distinction is useless.
    tight = position_size(
        "EURUSD",
        1.0850,
        1.0849,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    spread_findings = [w for w in tight.warnings if "spread" in w.lower()]
    assert spread_findings
    assert all(UNCHECKED_SUFFIX not in w for w in spread_findings)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_the_band_is_read_from_config_and_not_retyped(config: RiskConfig) -> None:
    """Override the ceiling and the clamp has to move with it.

    The defaults are 1% and 2%, so a hardcoded 0.02 passes every test above.
    This is the config-drift row of the prime directive table: an owner in
    drawdown lowers the ceiling to 1.5% and a hardcoded ladder keeps sizing at
    2%.
    """
    lowered = RiskConfig(risk_per_trade_max=0.015)
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        lowered,
        RATES,
        risk_fraction=0.02,
        broker=NANO_BROKER,
    )
    assert size.risk_fraction == pytest.approx(0.015)
    assert size.risk_amount == pytest.approx(30.00)


def test_the_balance_is_read_from_config_and_not_retyped() -> None:
    """Double the balance, double the size. R2,000 is a default, not a constant."""
    base = position_size(
        "USDJPY",
        155.00,
        155.30,
        RiskConfig(),
        RATES,
        risk_fraction=0.02,
        broker=NANO_BROKER,
    )
    doubled = position_size(
        "USDJPY",
        155.00,
        155.30,
        RiskConfig(account_balance=4000.0),
        RATES,
        risk_fraction=0.02,
        broker=NANO_BROKER,
    )
    assert doubled.risk_amount == pytest.approx(2 * base.risk_amount)
    assert doubled.units == pytest.approx(2 * base.units)


def test_the_account_currency_is_read_from_config_and_not_retyped() -> None:
    """A dollar-denominated account converts by identity on a dollar-quoted pair.

    ZAR is the default, so a hardcoded "ZAR" passes everything above. Here the
    conversion leg has to disappear: 400 units of EURUSD at 1.0850 is USD
    434.00 face value and one pip on one unit is USD 0.0001.
    """
    usd_account = RiskConfig(account_currency="USD", account_balance=2000.0)
    size = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        usd_account,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    assert size.account_currency == "USD"
    assert size.realised_risk_amount == pytest.approx(size.units * 0.0001 * 25.0)
    assert size.notional == pytest.approx(size.units * 1.0850)


def test_the_broker_contract_size_is_read_and_not_assumed(
    config: RiskConfig,
) -> None:
    """100,000 is the retail norm, which is exactly why it must not be a literal.

    Example B's solve against a 10,000 unit contract, worked by hand:

        raw units     = 1,117.117                       (unchanged by contract size)
        raw lots      = 1,117.117 / 10,000    = 0.11171
        rounded down  = 0.111 lots at a 0.001 step
        units         = 0.111 x 10,000        = 1,110
        realised risk = 1,110 x 30 x R0.00119355 = R39.75
        notional      = 1,110 x 155.00 x 0.119355 = R20,535.00

    Asserting ``units == lots * 10_000`` instead would prove nothing: units are
    defined as lots times the contract size, so that identity holds however the
    lots were arrived at. A solve that divided by a hardcoded 100,000 returns
    110 units here, a position ten times too small, and passes the identity.
    """
    ten_k = Broker(
        name="test-ten-thousand",
        min_lot=0.001,
        lot_step=0.001,
        contract_size=10_000.0,
        max_lot=50.0,
        typical_spread_pips=dict(DEFAULT_BROKER.typical_spread_pips),
    )
    size = position_size(
        "USDJPY", 155.00, 155.30, config, RATES, risk_fraction=0.02, broker=ten_k
    )
    assert size.lots == pytest.approx(0.111)
    assert size.units == pytest.approx(1110.0)
    assert size.realised_risk_amount == pytest.approx(39.75, abs=0.005)
    assert size.notional == pytest.approx(20535.00)


def test_a_long_and_a_short_of_the_same_width_size_identically(
    config: RiskConfig,
) -> None:
    """The stop distance is an absolute value.

    A long has the stop below and a short has it above. Signing the distance
    would make one of the two produce negative units, and negative units
    multiplied by a pip value is a negative risk figure that compares below
    every limit it is checked against.
    """
    long_side = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    short_side = position_size(
        "EURUSD",
        1.0825,
        1.0850,
        config,
        RATES,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    assert long_side.units == pytest.approx(short_side.units)
    assert long_side.stop_distance_pips == pytest.approx(short_side.stop_distance_pips)


def test_the_sizing_module_fetches_nothing(config: RiskConfig) -> None:
    """Rates arrive as an argument. This module must never go and get one.

    A missing rate has to raise, and the tempting fix the next reader will
    reach for is to look it up. That turns a loud refusal into a network call
    on the pre-trade path, and a stale or failed response back into the
    eighteenfold bug. Asserted against the source so it holds for code no test
    exercises.
    """
    import fbe.risk

    source = Path(fbe.risk.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not imported & {"httpx", "requests", "urllib", "socket", "http"}
