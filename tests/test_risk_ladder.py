"""The four functions around the sizing chain: the ladder, the geometry, the book.

One row of the prime directive table in the engineering standards came out of
this ladder: it hardcoded 1% and 2% next to a `RiskConfig` that held them, so
lowering the configured ceiling left the top rung outside the band,
`position_size` clamped it, and the pre-trade checklist read the resulting
warning as a refusal. Every high-conviction setup was rejected because two
numbers disagreed. Most of what is below exists to force a derived ladder apart
from a hardcoded one that happens to agree on the defaults.

The tables in ``docs/risk-and-execution.md`` section 3 are fixtures here rather
than prose: both the default ladder and that section's worked example of a
lowered ceiling are asserted, so the document breaks the build if the code stops
agreeing with it.
"""

from __future__ import annotations

import math

import pytest

from fbe.config import DEFAULT_TYPICAL_SPREAD_PIPS, BrokerConfig, RiskConfig
from fbe.risk import (
    CONVICTION_BAND_POSITION,
    MIN_REWARD_TO_RISK,
    PositionRisk,
    correlated_exposure,
    min_acceptable_rr,
    position_size,
    reward_to_risk,
    risk_fraction_for,
)
from fbe.types import Conviction

TRADEABLE = (Conviction.HIGH, Conviction.MEDIUM, Conviction.LOW)
"""Every level that produces a position. NONE is excluded on purpose: it returns
0.0, which sits below the band's floor by design, and the stub docstring says so
explicitly."""

CONFIRMED_BROKER = BrokerConfig(confirmed=True)
"""The default profile, marked confirmed, so these ladder and exposure tests
size without the `fbe.risk.BROKER_UNCONFIRMED` warning that would otherwise sit
on every result and change what they assert."""

NANO_BROKER = BrokerConfig(
    name="test-nano",
    min_lot=0.001,
    lot_step=0.001,
    contract_size=100_000.0,
    max_lot=50.0,
    typical_spread_pips=dict(DEFAULT_TYPICAL_SPREAD_PIPS),
    confirmed=True,
)


@pytest.fixture
def config() -> RiskConfig:
    """The plan's own account: R2,000 in rand, risking 1-2%."""
    return RiskConfig()


# --------------------------------------------------------------------------
# risk_fraction_for
# --------------------------------------------------------------------------


def test_the_published_ladder_reproduces_on_the_default_band(
    config: RiskConfig,
) -> None:
    """Section 3's table, asserted as a fixture.

    HIGH 2.0%, MEDIUM 1.5%, LOW 1.0%, NONE no trade. On the plan's R2,000 that
    is R40, R30, R20 and nothing, which is the column the document publishes
    beside it.
    """
    assert risk_fraction_for(Conviction.HIGH, config) == pytest.approx(0.02)
    assert risk_fraction_for(Conviction.MEDIUM, config) == pytest.approx(0.015)
    assert risk_fraction_for(Conviction.LOW, config) == pytest.approx(0.01)
    assert risk_fraction_for(Conviction.NONE, config) == 0.0

    money = {
        level: risk_fraction_for(level, config) * config.account_balance
        for level in Conviction
    }
    assert money[Conviction.HIGH] == pytest.approx(40.00)
    assert money[Conviction.MEDIUM] == pytest.approx(30.00)
    assert money[Conviction.LOW] == pytest.approx(20.00)
    assert money[Conviction.NONE] == 0.0


def test_the_lowered_ceiling_worked_example_reproduces() -> None:
    """Section 3 works this exact case, and it is the defect's own scenario.

    "If a review sends you to lower `risk_per_trade_max` to 1.5%, the whole
    ladder moves down with it: HIGH becomes 1.5%, MEDIUM 1.25%, LOW stays 1%."

    A ladder hardcoding 1% and 2% returns 2% for HIGH here, `position_size`
    clamps it to 1.5% and attaches a warning, and the checklist reads that
    warning as a refusal. Every high-conviction setup is rejected.
    """
    lowered = RiskConfig(risk_per_trade_max=0.015)
    assert risk_fraction_for(Conviction.HIGH, lowered) == pytest.approx(0.015)
    assert risk_fraction_for(Conviction.MEDIUM, lowered) == pytest.approx(0.0125)
    assert risk_fraction_for(Conviction.LOW, lowered) == pytest.approx(0.01)


def test_halving_the_ceiling_moves_every_rung_that_can_move(
    config: RiskConfig,
) -> None:
    """Halving the maximum moves HIGH and MEDIUM. LOW cannot move, by construction.

    LOW sits at position 0.0 in the band, so it IS `risk_per_trade_min`, and a
    change that leaves the floor alone cannot move it. Asserting that every rung
    changes on this input would assert something a correct implementation cannot
    do; asserting that LOW still equals the floor is the same guarantee stated
    in a way that can fail. A hardcoded 0.01 passes that one by accident, which
    is why the whole-band test below exists as well.
    """
    halved = RiskConfig(risk_per_trade_max=0.01)
    assert risk_fraction_for(Conviction.HIGH, halved) == pytest.approx(0.01)
    assert risk_fraction_for(Conviction.HIGH, halved) != pytest.approx(
        risk_fraction_for(Conviction.HIGH, config)
    )
    assert risk_fraction_for(Conviction.MEDIUM, halved) != pytest.approx(
        risk_fraction_for(Conviction.MEDIUM, config)
    )
    assert risk_fraction_for(Conviction.LOW, halved) == pytest.approx(
        halved.risk_per_trade_min
    )


def test_halving_the_whole_band_moves_every_rung(config: RiskConfig) -> None:
    """Move both endpoints and every rung has to follow, LOW included.

    This is the test a hardcoded ladder cannot survive, because there is no
    rung left sitting on a default it could have been written as.
    """
    halved = RiskConfig(risk_per_trade_min=0.005, risk_per_trade_max=0.01)
    assert risk_fraction_for(Conviction.HIGH, halved) == pytest.approx(0.01)
    assert risk_fraction_for(Conviction.MEDIUM, halved) == pytest.approx(0.0075)
    assert risk_fraction_for(Conviction.LOW, halved) == pytest.approx(0.005)
    for level in TRADEABLE:
        assert risk_fraction_for(level, halved) != pytest.approx(
            risk_fraction_for(level, config)
        )


def test_none_is_no_trade_rather_than_a_tiny_trade(config: RiskConfig) -> None:
    """Exactly 0.0, and the caller is expected to read that as no position.

    A small positive fraction here would size a real ticket on a pair the engine
    has no view on. `CONVICTION_BAND_POSITION` maps NONE to ``None`` rather than
    to a position in the band precisely so this case cannot be interpolated.
    """
    assert risk_fraction_for(Conviction.NONE, config) == 0.0
    assert CONVICTION_BAND_POSITION[Conviction.NONE] is None


def test_an_inverted_band_raises_rather_than_interpolating_backwards() -> None:
    """A floor above the ceiling would return a fraction outside the band.

    Interpolating from 0.03 to 0.01 gives HIGH 0.01 and LOW 0.03, so the ladder
    inverts silently: the engine's strongest calls would be its smallest
    positions and nothing in the result would say so.
    """
    inverted = RiskConfig(risk_per_trade_min=0.03, risk_per_trade_max=0.01)
    with pytest.raises(ValueError):
        risk_fraction_for(Conviction.HIGH, inverted)


def test_an_inverted_band_raises_for_every_level_including_none() -> None:
    """The guard is on the config, not on the level.

    Returning 0.0 for NONE without looking at the band would leave one entry
    point that accepts a config every other entry point refuses.
    """
    inverted = RiskConfig(risk_per_trade_min=0.03, risk_per_trade_max=0.01)
    for level in Conviction:
        with pytest.raises(ValueError):
            risk_fraction_for(level, inverted)


@pytest.mark.parametrize(
    "band",
    [
        RiskConfig(risk_per_trade_max=float("nan")),
        RiskConfig(risk_per_trade_min=float("nan")),
        RiskConfig(risk_per_trade_max=float("inf")),
    ],
)
def test_a_non_finite_band_endpoint_raises(band: RiskConfig) -> None:
    """``min > max`` is False for any NaN, so the inversion guard cannot see one.

    A NaN band returns NaN from every rung. An infinite ceiling is worse in one
    specific way: LOW comes back NaN because ``0.0 * inf`` is NaN, so the
    lowest rung of an unbounded band is the nonsense one.

    `Config.validate()` shares the blind spot and returns no problems for the
    first two, so nothing else in the system refuses them either.
    """
    with pytest.raises(ValueError):
        risk_fraction_for(Conviction.HIGH, band)


def test_a_negative_floor_raises_rather_than_sizing_below_the_plan() -> None:
    """The quietest of the three bad bands, and the one with no other guard.

    With a floor of -1% and the default ceiling, MEDIUM returns 0.5%, half the
    plan's intended minimum. It is inside the configured band, so
    `position_size` neither clamps it nor warns, and the ticket arrives with an
    empty warnings tuple, which section 8 lets the owner tick the sizing box
    on. LOW returns -1%, which is refused downstream but blamed on lot
    rounding rather than on the fraction.
    """
    with pytest.raises(ValueError):
        risk_fraction_for(Conviction.MEDIUM, RiskConfig(risk_per_trade_min=-0.01))


def test_every_conviction_level_has_a_rung_and_a_reward_bar() -> None:
    """Adding a level to `Conviction` must fail here rather than in production.

    Both maps raise `KeyError` for an absent level, which is the right
    behaviour, and this states the exhaustiveness rather than leaving it to be
    discovered by the tests that happen to iterate.
    """
    assert set(CONVICTION_BAND_POSITION) == set(Conviction)
    assert set(MIN_REWARD_TO_RISK) == set(Conviction)


@pytest.mark.parametrize(
    "band",
    [
        RiskConfig(),
        RiskConfig(risk_per_trade_max=0.015),
        RiskConfig(risk_per_trade_min=0.005, risk_per_trade_max=0.01),
        RiskConfig(risk_per_trade_min=0.02, risk_per_trade_max=0.02),
        RiskConfig(risk_per_trade_min=0.02, risk_per_trade_max=0.03),
    ],
)
def test_every_tradeable_fraction_lands_inside_the_band(band: RiskConfig) -> None:
    """The cap cannot be exceeded by the ladder, on any band including a degenerate one.

    NONE is excluded because it returns 0.0, which is below the floor by design
    and is documented as such in the stub's own `Returns:` block.
    """
    for level in TRADEABLE:
        fraction = risk_fraction_for(level, band)
        assert band.risk_per_trade_min <= fraction <= band.risk_per_trade_max


def test_a_band_above_the_validated_ceiling_is_interpolated_not_clamped() -> None:
    """No literal ceiling inside the function, including one the validator enforces.

    `Config.validate()` refuses a ``risk_per_trade_max`` above 2%, so a
    hardcoded 0.02 clamp here never bites on a validated config and every other
    test in this file passes with it in place. This function takes a bare
    `RiskConfig` and validates nothing itself, and a literal 0.02 next to a
    config field holding the ceiling is the precise defect this issue closes.

    An in-band assertion cannot catch it, which is why this asserts exact
    values: clamping 0.03 down to 0.02 still satisfies "inside [0.02, 0.03]".
    """
    wide = RiskConfig(risk_per_trade_min=0.02, risk_per_trade_max=0.03)
    assert risk_fraction_for(Conviction.HIGH, wide) == pytest.approx(0.03)
    assert risk_fraction_for(Conviction.MEDIUM, wide) == pytest.approx(0.025)
    assert risk_fraction_for(Conviction.LOW, wide) == pytest.approx(0.02)


def test_the_ladder_is_ordered_by_conviction(config: RiskConfig) -> None:
    """Size rises with conviction. The reward requirement runs the other way.

    Both directions are asserted here together because reversing either one
    alone still produces a plausible ladder.
    """
    assert (
        risk_fraction_for(Conviction.LOW, config)
        < risk_fraction_for(Conviction.MEDIUM, config)
        < risk_fraction_for(Conviction.HIGH, config)
    )
    assert (
        min_acceptable_rr(Conviction.HIGH)
        < min_acceptable_rr(Conviction.MEDIUM)
        < min_acceptable_rr(Conviction.LOW)
    )


def test_the_band_position_map_is_read_and_not_retyped(
    config: RiskConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Move MEDIUM's position in the band and the fraction has to follow.

    The position is actually moved rather than merely asserted, because 0.5 is
    the midpoint and the midpoint is also what a hardcoded average produces, so
    the default cannot tell a read from a retype. At 0.75 of a 1-2% band the
    answer is 1.75%, which no plausible hardcoding yields.

    `CONVICTION_BAND_POSITION` is exported and is documented as the single
    source of the ladder's shape, and `MIN_REWARD_TO_RISK` names it as
    something to revisit once the journal has evidence. It will be edited, so
    the function following it needs a test that fails when it stops.
    """
    assert CONVICTION_BAND_POSITION[Conviction.MEDIUM] == 0.5
    monkeypatch.setitem(CONVICTION_BAND_POSITION, Conviction.MEDIUM, 0.75)
    assert risk_fraction_for(Conviction.MEDIUM, config) == pytest.approx(0.0175)


# --------------------------------------------------------------------------
# reward_to_risk
# --------------------------------------------------------------------------


def test_the_long_worked_example_reproduces() -> None:
    """Section 2 example A: entry 1.0850, stop 1.0825, target 1.0925.

    The document publishes 0.0075 / 0.0025 = 3.0R and notes it clears the 2.5R
    minimum required at LOW conviction.
    """
    assert reward_to_risk(1.0850, 1.0825, 1.0925) == pytest.approx(3.0)
    assert reward_to_risk(1.0850, 1.0825, 1.0925) >= min_acceptable_rr(Conviction.LOW)


def test_the_short_worked_example_reproduces() -> None:
    """Section 2 example B: entry 155.00, stop 155.30, target 154.10.

    The document publishes 0.90 / 0.30 = 3.0R. Note the stop is above the entry
    and the target below it, which is the mirror image of example A and scores
    the same.
    """
    assert reward_to_risk(155.00, 155.30, 154.10) == pytest.approx(3.0)


def test_a_short_scores_the_same_as_its_mirror_image_long() -> None:
    """Both legs are absolute, so direction carries no information here.

    Signing either leg makes one of the two directions return a negative ratio,
    which compares below every minimum and would refuse every short.
    """
    long_side = reward_to_risk(entry=100.0, stop=98.0, target=106.0)
    short_side = reward_to_risk(entry=100.0, stop=102.0, target=94.0)
    assert long_side == pytest.approx(3.0)
    assert short_side == pytest.approx(3.0)


def test_an_entry_equal_to_the_stop_raises() -> None:
    """The ratio has no defined value on a zero denominator.

    Returning infinity would read as the best setup ever seen and clear every
    minimum in `MIN_REWARD_TO_RISK`.
    """
    with pytest.raises(ValueError):
        reward_to_risk(1.0850, 1.0850, 1.0925)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("leg", ["entry", "stop", "target"])
def test_a_non_finite_price_raises_rather_than_returning_a_ratio(
    leg: str, bad: float
) -> None:
    """A NaN ratio is the one number that clears or fails every bar at once.

    ``nan < 1.5`` is False and ``nan >= 1.5`` is also False, so whether a
    nonsense setup is refused depends entirely on which way the caller spelled
    its check. A caller writing ``if rr < min_acceptable_rr(conviction):
    refuse`` lets it through. Refusing the input makes both spellings agree.
    """
    prices = {"entry": 100.0, "stop": 98.0, "target": 106.0}
    prices[leg] = bad
    with pytest.raises(ValueError):
        reward_to_risk(**prices)


def test_a_non_finite_ratio_would_have_cleared_the_bar_either_way() -> None:
    """Why the guard above is a refusal rather than a warning, stated as arithmetic.

    This asserts the property of NaN that makes it dangerous here, not the
    behaviour of `reward_to_risk`, which now refuses to produce one.
    """
    nan = float("nan")
    assert not nan < min_acceptable_rr(Conviction.HIGH)
    assert not nan >= min_acceptable_rr(Conviction.HIGH)


def test_a_target_on_the_same_side_as_the_stop_raises() -> None:
    """Untradeable whichever direction the setup is meant, so it is refused.

    Read long, the target sits behind the entry. Read short, the stop sits
    between entry and target and is hit before the target can be. Knowing which
    needs no direction argument, only that target and stop fall on the same
    side of entry, so refusing costs nothing in expressiveness.

    Left to the absolute-value arithmetic this scores 3.0 and clears every
    minimum in `MIN_REWARD_TO_RISK`, which is a plausible number on a setup
    that cannot be taken.
    """
    with pytest.raises(ValueError, match="same side"):
        reward_to_risk(entry=100.0, stop=98.0, target=94.0)
    with pytest.raises(ValueError, match="same side"):
        reward_to_risk(entry=100.0, stop=102.0, target=106.0)


def test_refusing_the_same_side_case_leaves_both_real_directions_alone() -> None:
    """The guard must not catch the mirror-image pair it sits next to.

    A long has its stop below and its target above; a short has them the other
    way round. In both the product of the two offsets is negative, which is
    what the guard tests, so neither is touched. A guard written on the sign of
    one leg alone would refuse every short.
    """
    assert reward_to_risk(entry=100.0, stop=98.0, target=106.0) == pytest.approx(3.0)
    assert reward_to_risk(entry=100.0, stop=102.0, target=94.0) == pytest.approx(3.0)


def test_a_target_at_the_entry_is_a_zero_ratio_rather_than_a_refusal() -> None:
    """Zero reward is a real measurement, not a malformed setup.

    The product is 0.0 rather than positive, so the same-side guard does not
    fire. A 0.0R setup clears no minimum, which is the right outcome and is
    reached by measuring rather than by raising.
    """
    assert reward_to_risk(entry=100.0, stop=98.0, target=100.0) == 0.0
    assert reward_to_risk(100.0, 98.0, 100.0) < min_acceptable_rr(Conviction.HIGH)


def test_the_ratio_scales_with_the_distance_to_target() -> None:
    """Twice the distance is twice the R, on a fixed stop."""
    near = reward_to_risk(100.0, 98.0, 104.0)
    far = reward_to_risk(100.0, 98.0, 108.0)
    assert near == pytest.approx(2.0)
    assert far == pytest.approx(4.0)


# --------------------------------------------------------------------------
# min_acceptable_rr
# --------------------------------------------------------------------------


def test_the_published_reward_ladder_reproduces() -> None:
    """Section 3's rightmost column: 1.5R, 2.0R, 2.5R, and no trade at NONE."""
    assert min_acceptable_rr(Conviction.HIGH) == pytest.approx(1.5)
    assert min_acceptable_rr(Conviction.MEDIUM) == pytest.approx(2.0)
    assert min_acceptable_rr(Conviction.LOW) == pytest.approx(2.5)
    assert math.isinf(min_acceptable_rr(Conviction.NONE))


def test_no_reward_to_risk_clears_the_bar_at_none() -> None:
    """Infinity rather than a large number, so nothing can clear it.

    A finite sentinel, however large, is a threshold a sufficiently generous
    target beats, and NONE means the engine has no view to size at all.
    """
    assert reward_to_risk(100.0, 99.0, 1_000_000.0) < min_acceptable_rr(Conviction.NONE)


def test_the_reward_ladder_is_read_and_not_retyped() -> None:
    """The returned value has to come from `MIN_REWARD_TO_RISK` itself."""
    for level in Conviction:
        assert min_acceptable_rr(level) == MIN_REWARD_TO_RISK[level]


# --------------------------------------------------------------------------
# correlated_exposure
# --------------------------------------------------------------------------


def test_two_tickets_pointing_the_same_way_count_as_one_exposure() -> None:
    """Long EURUSD and long GBPUSD is one short-dollar bet held twice.

    Each ticket risks 1% of a R2,000 account. The dollar leg appears in both, so
    the dollar carries 2% while the euro and sterling carry 1% each. Counting
    the two as independent understates the real dollar exposure by half, and
    2% against a 4% cap is the difference between room for another and none.
    """
    book = [
        PositionRisk("EURUSD", risk_amount=20.0, account_balance_at_entry=2000.0),
        PositionRisk("GBPUSD", risk_amount=20.0, account_balance_at_entry=2000.0),
    ]
    exposure = correlated_exposure(book)
    assert exposure["USD"] == pytest.approx(0.02)
    assert exposure["EUR"] == pytest.approx(0.01)
    assert exposure["GBP"] == pytest.approx(0.01)
    assert exposure["USD"] == pytest.approx(2 * exposure["EUR"])


def test_full_risk_lands_on_both_legs_rather_than_half_on_each() -> None:
    """A single ticket puts its whole stake on each leg's behaviour.

    Splitting the risk in half across the two legs would report 0.5% per
    currency here and let twice as much exposure through the cap.
    """
    book = [PositionRisk("EURUSD", 20.0, 2000.0)]
    exposure = correlated_exposure(book)
    assert exposure["EUR"] == pytest.approx(0.01)
    assert exposure["USD"] == pytest.approx(0.01)


def test_a_hedged_book_reads_as_more_exposed_than_it_is() -> None:
    """The documented cost of attributing full risk to both legs.

    Long EURUSD and long USDJPY is close to flat on the dollar, and this reports
    2% on it. That is deliberate and it is the right error to make on a R2,000
    account: the alternative understates a genuinely correlated book, which is
    the failure that costs money rather than opportunity.
    """
    book = [
        PositionRisk("EURUSD", 20.0, 2000.0),
        PositionRisk("USDJPY", 20.0, 2000.0),
    ]
    exposure = correlated_exposure(book)
    assert exposure["USD"] == pytest.approx(0.02)
    assert exposure["EUR"] == pytest.approx(0.01)
    assert exposure["JPY"] == pytest.approx(0.01)


def test_the_realised_risk_is_used_and_not_the_intended_one(
    config: RiskConfig,
) -> None:
    """What is on the book is what can be lost.

    Section 2 example B intends R40.00 and, after rounding down to a whole lot
    step, actually risks R35.81. Against R2,000 that is 1.79% rather than 2.00%.
    Reading the intended figure would report exposure the account does not carry
    and refuse the next trade on it.
    """
    rates = {"USDZAR": 18.50, "USDJPY": 155.00}
    sized = position_size(
        "USDJPY",
        155.00,
        155.30,
        config,
        rates,
        risk_fraction=0.02,
        broker=CONFIRMED_BROKER,
    )
    assert sized.risk_amount == pytest.approx(40.00)
    assert sized.realised_risk_amount == pytest.approx(35.81, abs=0.005)

    exposure = correlated_exposure([PositionRisk.from_position_size(sized)])
    assert exposure["USD"] == pytest.approx(35.81 / 2000.0, abs=1e-5)
    assert exposure["USD"] != pytest.approx(0.02)
    assert exposure["USD"] < 0.02


def test_each_position_is_measured_against_its_own_entry_balance() -> None:
    """The fraction is what the position was sized on, not today's balance.

    A ticket opened at R2,000 risking R20 is a 1% position for its whole life,
    even after the account grows. Dividing both by one balance would misreport
    the older one.
    """
    book = [
        PositionRisk("EURUSD", 20.0, 2000.0),
        PositionRisk("EURGBP", 20.0, 4000.0),
    ]
    exposure = correlated_exposure(book)
    assert exposure["EUR"] == pytest.approx(0.015)
    assert exposure["USD"] == pytest.approx(0.01)
    assert exposure["GBP"] == pytest.approx(0.005)


def test_an_empty_book_returns_an_empty_mapping() -> None:
    """A known-empty book, which is a different fact from an unread one.

    This function cannot express "not known": an empty result here means no
    positions, and the caller that can tell an unread journal from an empty one
    has to take that decision before calling.
    """
    assert dict(correlated_exposure([])) == {}


def test_a_currency_with_no_exposure_is_absent_rather_than_zero() -> None:
    """Absence and a measured zero are different answers.

    A map padded with 0.0 for the other five G10 currencies would read as a
    measurement of them, and the caller cannot tell a currency nothing is held
    in from one whose positions netted out.
    """
    exposure = correlated_exposure([PositionRisk("EURUSD", 20.0, 2000.0)])
    assert set(exposure) == {"EUR", "USD"}
    for absent in ("JPY", "GBP", "CHF", "CAD", "AUD", "NZD"):
        assert absent not in exposure


@pytest.mark.parametrize("balance", [0.0, -2000.0, float("nan"), float("inf")])
def test_a_position_with_no_usable_entry_balance_raises(balance: float) -> None:
    """A zero balance has no risk fraction, and a fabricated one reaches the cap.

    Dividing by zero raises somewhere unhelpful, a negative balance flips the
    sign of the exposure so it compares below every cap, and a NaN compares
    false against every cap it is checked against. All three would put a number
    in front of `max_correlated_exposure` that means nothing.
    """
    book = [PositionRisk("EURUSD", 20.0, balance)]
    with pytest.raises(ValueError):
        correlated_exposure(book)


@pytest.mark.parametrize("risk", [-20.0, float("nan"), float("inf")])
def test_a_position_with_an_unusable_risk_amount_raises(risk: float) -> None:
    """The balance guard's twin, and the one that hides rather than invents.

    A negative ``risk_amount`` subtracts from a leg's total, so one corrupt row
    conceals a real position behind a hedge that does not exist: a book of long
    EURUSD at R20 and a bad row at minus R20 reports zero dollar exposure and
    clears every cap. A NaN poisons the leg it touches and then compares False
    against the cap it is checked against.

    `OpenPosition` documents ``risk_amount`` as positive, but the protocol is
    structural and a `fbe.journal.TradeRecord` satisfies it without that being
    enforced anywhere, so the check belongs here.
    """
    with pytest.raises(ValueError):
        correlated_exposure([PositionRisk("EURUSD", risk, 2000.0)])


def test_one_corrupt_row_cannot_cancel_a_real_position() -> None:
    """The concrete failure the guard above prevents, stated as a book.

    Without the guard this returns 0.0 on both legs, which reads as a flat book
    rather than as a corrupt one, and every cap is cleared by a position that
    is genuinely on.
    """
    book = [
        PositionRisk("EURUSD", 20.0, 2000.0),
        PositionRisk("EURUSD", -20.0, 2000.0),
    ]
    with pytest.raises(ValueError):
        correlated_exposure(book)


def test_a_zero_risk_position_is_measured_rather_than_refused() -> None:
    """Zero is a real answer here, unlike a zero balance.

    A refused trade comes back from `position_size` with a realised risk of
    0.0, and that is a position on the book with nothing at stake rather than
    corrupt data. It contributes 0.0 to both legs and the currencies still
    appear, because they were asked about.
    """
    exposure = correlated_exposure([PositionRisk("EURUSD", 0.0, 2000.0)])
    assert exposure["EUR"] == 0.0
    assert exposure["USD"] == 0.0


def test_a_malformed_pair_raises_rather_than_being_attributed() -> None:
    """A five-character pair cannot be split into two legs.

    Attributing it to a three-character base and whatever is left would record
    exposure against a currency that does not exist, and the cap would never see
    the real one.
    """
    with pytest.raises(ValueError):
        correlated_exposure([PositionRisk("EURUS", 20.0, 2000.0)])


def test_a_six_character_pair_whose_leg_is_not_a_currency_raises() -> None:
    """One mistyped character defeats the exposure cap, and splits cleanly.

    `split_pair` checks length and nothing else, so ``"EURUSO"`` parses exactly
    as well as ``"EURUSD"`` and the position's risk lands on a currency that
    does not exist. The real dollar leg goes unmeasured.

    The cost is concrete. A book of long EURUSD at R40, long GBPUSD at R40 and
    a journal row typed ``"EURUSO"`` at R20, all on R2,000, reports the dollar
    at 4.0%. The real figure is R100, or 5%. A cap that breaches strictly above
    4% therefore clears, and one more dollar trade is authorised. The journal
    is hand-written and `OpenPosition` is structural, so nothing upstream
    validates the string.
    """
    with pytest.raises(ValueError, match="G10"):
        correlated_exposure([PositionRisk("EURUSO", 20.0, 2000.0)])
    with pytest.raises(ValueError, match="G10"):
        correlated_exposure([PositionRisk("XXXUSD", 20.0, 2000.0)])


def test_the_denominator_is_each_position_own_balance_and_says_so() -> None:
    """The understatement this function cannot fix, pinned so it cannot surprise.

    Two R40 tickets opened at R4,000 report 2% on the shared dollar leg. That
    same R80 against a balance now down to R2,000 is 4%, so a caller reading
    the map as a fraction of the current account is understated by exactly the
    ratio of the two balances, and most understated in a drawdown.

    Asserted rather than merely documented, because a later change that
    switched to a single denominator would be a silent behavioural change to
    everything comparing this against a cap.
    """
    book = [
        PositionRisk("EURUSD", 40.0, 4000.0),
        PositionRisk("GBPUSD", 40.0, 4000.0),
    ]
    exposure = correlated_exposure(book)
    assert exposure["USD"] == pytest.approx(0.02)
    real_money_at_risk = 80.0
    assert real_money_at_risk / 2000.0 == pytest.approx(0.04)


def test_the_same_pair_twice_accumulates() -> None:
    """Two tickets on one pair are two positions, not one.

    Deduplicating by pair would hide the most obviously correlated book there
    is, which is the same trade entered twice.
    """
    book = [
        PositionRisk("EURUSD", 20.0, 2000.0),
        PositionRisk("EURUSD", 20.0, 2000.0),
    ]
    exposure = correlated_exposure(book)
    assert exposure["EUR"] == pytest.approx(0.02)
    assert exposure["USD"] == pytest.approx(0.02)


def test_the_exposure_map_is_what_the_cap_is_compared_against(
    config: RiskConfig,
) -> None:
    """The comparison reads `RiskConfig.max_correlated_exposure`, not a literal.

    This function performs no check and refuses nothing, which is #42's scope,
    so what is asserted here is that its output is on the same scale as the
    configured cap: a fraction of the account, not money and not a count.
    """
    book = [
        PositionRisk("EURUSD", 20.0, 2000.0),
        PositionRisk("GBPUSD", 20.0, 2000.0),
        PositionRisk("AUDUSD", 20.0, 2000.0),
    ]
    exposure = correlated_exposure(book)
    assert exposure["USD"] == pytest.approx(0.03)
    assert exposure["USD"] < config.max_correlated_exposure
    # Four 1% tickets summing to exactly the 4% default is a coincidence of the
    # defaults, so it is not asserted against the config value here. Whether
    # the cap is read from `RiskConfig` or retyped is `check_limits`' wire to
    # prove, and ``tests/test_limit_checks.py`` does prove it by overriding
    # `max_correlated_exposure` to 0.07.
    assert correlated_exposure(book + [PositionRisk("NZDUSD", 20.0, 2000.0)])[
        "USD"
    ] == pytest.approx(0.04)


def test_a_position_sized_at_a_nano_broker_reports_its_own_fraction(
    config: RiskConfig,
) -> None:
    """End to end from the sizing chain, so the two halves agree on the units.

    Section 2 example A at a 0.001 lot step realises R18.50 against an intended
    R20.00, which is 0.925% of the account rather than 1%.
    """
    rates = {"USDZAR": 18.50}
    sized = position_size(
        "EURUSD",
        1.0850,
        1.0825,
        config,
        rates,
        risk_fraction=0.01,
        broker=NANO_BROKER,
    )
    assert sized.realised_risk_amount == pytest.approx(18.50)
    exposure = correlated_exposure([PositionRisk.from_position_size(sized)])
    assert exposure["EUR"] == pytest.approx(0.00925)
    assert exposure["USD"] == pytest.approx(0.00925)


def test_a_lowercase_pair_is_normalised_to_the_codes_the_cap_is_keyed_by() -> None:
    """`split_pair` slices positionally and does no case folding of its own.

    Without normalising, ``"eurusd"`` emits keys ``"eur"`` and ``"usd"``, which
    no caller comparing against G10 codes will ever match. The exposure would
    not be wrong, it would be invisible, and the cap would see nothing at all
    for a position that is really on the book.
    """
    exposure = correlated_exposure([PositionRisk("eurusd", 20.0, 2000.0)])
    assert set(exposure) == {"EUR", "USD"}
    assert exposure["EUR"] == pytest.approx(0.01)
