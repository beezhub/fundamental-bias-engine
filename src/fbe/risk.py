"""Position sizing and risk limits for a ZAR-denominated retail account.

This module is where the engine's opinion becomes a ticket. Everything above
it argues about which currency is stronger. Everything here answers the only
question the broker cares about: how many units, and what stops us.

The hard problem in this file is pip value. The account is denominated in ZAR.
The tradeable universe is 28 G10 crosses and not one of them has ZAR on either
leg, so every position's risk arrives in USD, JPY, CHF, CAD, AUD, NZD, GBP or
EUR and has to be carried back to ZAR at the prevailing rate before it can be
compared to the plan's 1-2% band.

Get that conversion wrong and nothing visibly breaks. The sizing function still
returns a number, the ticket still fills, the stop still sits where the chart
says it should. The account simply risks the wrong amount on every trade,
silently, for as long as the error survives. A conversion factor that is out by
the USDZAR rate turns a 1% risk into an 18% risk. That is the single most
damaging bug this project could ship, and it is the reason `pip_value` refuses
to guess: a missing rate raises rather than defaulting to 1.0.

Units convention used throughout:
    * ``units`` are units of the BASE currency. 100,000 units of EURUSD is one
      standard lot and represents EUR 100,000.
    * ``lots`` are ``units / contract_size`` for the broker in question.
    * ``pip_size`` is the price increment one pip represents, in the QUOTE
      currency.
    * ``risk_amount`` and every other money figure exposed to the caller are in
      the account currency, ZAR.
    * All rates are quoted market convention, base first: ``"USDZAR"`` means
      ZAR per USD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from fbe.config import RiskConfig
from fbe.types import Conviction, PositionSize
from fbe.universe import split_pair

__all__ = [
    "Broker",
    "DEFAULT_BROKER",
    "JPY_PIP_SIZE",
    "STANDARD_PIP_SIZE",
    "CONVICTION_RISK_FRACTION",
    "MIN_REWARD_TO_RISK",
    "MissingRateError",
    "pip_size",
    "pip_value",
    "position_size",
    "risk_fraction_for",
    "reward_to_risk",
    "min_acceptable_rr",
    "correlated_exposure",
    "check_limits",
]


STANDARD_PIP_SIZE: float = 0.0001
"""One pip for a pair quoted to four decimal places, e.g. EURUSD."""

JPY_PIP_SIZE: float = 0.01
"""One pip for a JPY-quoted pair, which the market quotes to two decimals."""


CONVICTION_RISK_FRACTION: Mapping[Conviction, float] = {
    Conviction.HIGH: 0.02,
    Conviction.MEDIUM: 0.015,
    Conviction.LOW: 0.01,
    Conviction.NONE: 0.0,
}
"""Conviction to risk fraction, inside the plan's 1-2% band.

Conviction modulates size, it does not gate entry. A LOW conviction pair with a
clean channel touch is still a trade, taken at the bottom of the band. The
alternative, refusing everything below HIGH, would leave a small account idle
for weeks at a time and push the owner toward the overtrading the plan warns
about. NONE maps to 0.0, which means no trade at all: the engine has no view,
so there is no bias layer to add to the chart.
"""


MIN_REWARD_TO_RISK: Mapping[Conviction, float] = {
    Conviction.HIGH: 1.5,
    Conviction.MEDIUM: 2.0,
    Conviction.LOW: 2.5,
    Conviction.NONE: float("inf"),
}
"""Minimum reward-to-risk a setup must offer before it is worth taking.

The ladder runs the opposite way to size, on purpose. When the fundamental case
is strong the hit rate carries the expectancy, so a 1.5R target is enough. When
the case is thin the trade has to pay more when it works, because it will work
less often. NONE is infinite, which is the arithmetic way of saying no target
justifies a trade the engine cannot support.
"""


@dataclass(frozen=True, slots=True)
class Broker:
    """Execution constraints imposed by the owner's broker.

    These are not preferences, they are the boundaries of what can physically
    be sent. On a R2,000 account the minimum lot is the binding constraint far
    more often than the risk rule is, which is why it lives in a real object
    rather than being assumed.

    Attributes:
        name: Broker identifier, recorded on journal entries so a change of
            broker is visible in the trade history.
        min_lot: Smallest lot the broker will accept. 0.01 is the common retail
            "micro lot" floor. Some brokers offer 0.001 nano lots, which on an
            account this size is the difference between several G10 pairs being
            tradeable and being untradeable.
        lot_step: Increment above the minimum. Sizes must land on a multiple of
            this, always by rounding down.
        contract_size: Base-currency units in one standard lot. 100,000 across
            G10 spot FX at essentially every retail broker.
        max_lot: Largest single ticket. Irrelevant at this account size, held
            for completeness and for when the account grows.
        typical_spread_pips: Indicative spread per pair, in pips, during the
            London and New York overlap. Used to flag pairs whose cost eats the
            edge, per the plan's "low spreads and trading costs" rule. These are
            indicative only: spreads widen at the Asia open, around releases,
            and on Sunday reopen.
        commission_per_lot: Round-turn commission per standard lot in the
            account currency, 0.0 on a spread-only account.
    """

    name: str
    min_lot: float
    lot_step: float
    contract_size: float
    max_lot: float = 100.0
    typical_spread_pips: Mapping[str, float] = field(default_factory=dict)
    commission_per_lot: float = 0.0


DEFAULT_BROKER: Broker = Broker(
    name="generic-retail-micro",
    min_lot=0.01,
    lot_step=0.01,
    contract_size=100_000.0,
    max_lot=50.0,
    typical_spread_pips={
        "EURUSD": 0.8,
        "GBPUSD": 1.2,
        "USDJPY": 0.9,
        "USDCHF": 1.3,
        "USDCAD": 1.4,
        "AUDUSD": 1.0,
        "NZDUSD": 1.6,
        "EURGBP": 1.3,
        "EURJPY": 1.5,
        "GBPJPY": 2.4,
        "EURCHF": 1.6,
        "AUDJPY": 1.7,
        "CADJPY": 2.0,
        "CHFJPY": 2.2,
        "NZDJPY": 2.3,
        "EURAUD": 2.0,
        "EURCAD": 2.2,
        "EURNZD": 3.0,
        "GBPAUD": 2.8,
        "GBPCAD": 3.0,
        "GBPCHF": 2.6,
        "GBPNZD": 4.0,
        "AUDCAD": 2.0,
        "AUDCHF": 2.1,
        "AUDNZD": 2.4,
        "NZDCAD": 2.8,
        "NZDCHF": 3.0,
        "CADCHF": 2.4,
    },
    commission_per_lot=0.0,
)
"""Placeholder broker profile. THE OWNER MUST CONFIRM EVERY VALUE.

These are typical retail figures for a spread-only account, not a quote from
any specific broker. ``min_lot`` and ``lot_step`` in particular decide which
pairs are tradeable at R2,000, so an assumed 0.01 where the broker actually
offers 0.001 wrongly rules out most of the universe, and an assumed 0.01 where
the broker requires 0.1 wrongly rules it in. Read them off the broker's
contract specification, place one minimum-size trade to confirm, then replace
this constant. Spreads should be sampled from the owner's own terminal during
the hours they actually trade, not taken from a marketing page.
"""


class MissingRateError(LookupError):
    """Raised when a rate needed to convert risk into the account currency is absent.

    This exists as its own exception type so callers cannot swallow it by
    accident alongside an ordinary ``KeyError``. Converting at an assumed rate
    of 1.0 would size the position roughly eighteen times too large on a ZAR
    account and would do so without any visible symptom, so the only acceptable
    behaviour is to refuse to size the trade.
    """


def pip_size(pair: str) -> float:
    """Return the price increment one pip represents for a pair.

    Args:
        pair: Six-character pair string in market convention, e.g. ``"EURUSD"``.

    Returns:
        ``0.01`` when the quote currency is JPY, otherwise ``0.0001``.

    Raises:
        ValueError: If ``pair`` is not six characters.

    Note:
        This is about the QUOTE currency only. USDJPY and GBPJPY both use 0.01
        because both are quoted in yen. USDCHF uses 0.0001 despite the base
        being a dollar. Pairs quoted to five or three decimals by the broker
        show fractional pips (points); a pip is still the fourth or second
        decimal and this function reports the pip, not the point.
    """
    _, quote = split_pair(pair.upper())
    return JPY_PIP_SIZE if quote == "JPY" else STANDARD_PIP_SIZE


def pip_value(
    pair: str,
    units: float,
    account_currency: str,
    rates: Mapping[str, float],
) -> float:
    """Value of one pip of ``pair``, for ``units``, expressed in the account currency.

    The calculation always starts the same way. One pip on ``units`` of the base
    currency is worth ``units * pip_size(pair)`` in the QUOTE currency. The work
    is carrying that quote-currency amount back to the account currency, and
    there are exactly three cases.

    Case 1, quote currency equals account currency. No conversion. A USD account
    trading EURUSD gets ``units * 0.0001`` USD per pip directly. This is the
    case every textbook example uses and the case this account is never in.

    Case 2, the account currency is the BASE of the pair. A USD account trading
    USDJPY earns pips in yen, and one USD buys ``rate`` yen, so the pip value is
    ``units * pip_size / rate`` where ``rate`` is the pair's own current price.
    The pair's price is the conversion rate, which is why this case needs no
    extra market data but does need the live price rather than the entry price.

    Case 3, the account currency appears nowhere in the pair. This is the ZAR
    case and it is every trade this engine will ever produce, because no G10
    cross has a ZAR leg. A third rate is required: the QUOTE currency against
    ZAR. Compute ``units * pip_size`` in the quote currency, then multiply by
    ZAR per unit of quote currency. For USDJPY on a ZAR account that means
    finding JPY to ZAR, which is normally derived as ``USDZAR / USDJPY``.

    Rate lookup and its failure mode:
        ``rates`` is keyed by market-convention pair string, so ``"USDZAR"``
        holds ZAR per USD. To convert currency ``X`` into ``account_currency``
        ``A``, look for ``f"{X}{A}"`` and multiply, or ``f"{A}{X}"`` and divide.
        If neither key is present, raise `MissingRateError`. Do not fall back to
        1.0, do not fall back to a cached rate of unknown age, do not fall back
        to the last successful conversion. A silently wrong pip value breaks the
        1-2% rule on every subsequent trade and leaves no trace in the output.
        Refusing to size the trade is loud, immediate, and recoverable.

    Args:
        pair: Pair being traded, e.g. ``"USDJPY"``.
        units: Position size in base-currency units. May be fractional during
            the solve inside `position_size`. Must be non-negative; direction is
            carried by the entry and stop, not by the sign of the size.
        account_currency: ISO code the account is denominated in, ``"ZAR"`` here.
        rates: Current mid rates keyed by pair string. Must include whatever
            conversion leg the case above requires. Staleness matters: a rate
            from last week is a wrong rate, and the caller is responsible for
            freshness.

    Returns:
        Value of one pip in ``account_currency``, always positive.

    Raises:
        MissingRateError: If the conversion leg is absent from ``rates``, or is
            present but zero or negative.
        ValueError: If ``units`` is negative or ``pair`` is malformed.
    """
    raise NotImplementedError


def position_size(
    pair: str,
    entry: float,
    stop: float,
    config: RiskConfig,
    rates: Mapping[str, float],
    risk_fraction: float | None = None,
    broker: Broker = DEFAULT_BROKER,
) -> PositionSize:
    """Size a position so the stop costs exactly the planned fraction of the account.

    This is the function the whole plan turns on. The plan's rule is not "trade
    a fixed lot with a sensible stop", it is "let the chart place the stop, then
    derive the size from that distance". The stop goes just beyond the far side
    of the channel or the trendline, wherever that happens to be, and the size
    absorbs the difference.

    The chain, in order:
        1. ``risk_fraction`` defaults to ``config.risk_per_trade_min`` when not
           supplied, which is the conservative reading of the plan's 1-2% band.
           Clamp it to ``[risk_per_trade_min, risk_per_trade_max]`` and add a
           warning if the caller asked for something outside that band, rather
           than honouring it.
        2. ``risk_amount = config.account_balance * risk_fraction``. On the
           plan's R2,000 that is R20 at 1% and R40 at 2%, matching the figures
           the owner wrote down.
        3. ``stop_distance_pips = abs(entry - stop) / pip_size(pair)``. Absolute
           value: a long has the stop below and a short has it above, and the
           distance is the same quantity either way.
        4. ``value_per_pip = pip_value(pair, 1.0, config.account_currency,
           rates)``, the ZAR value of one pip on a single base-currency unit.
        5. ``units = risk_amount / (stop_distance_pips * value_per_pip)``. This
           is the whole calculation. Everything else is guarding it.
        6. ``lots = units / broker.contract_size``, then round DOWN to the
           nearest multiple of ``broker.lot_step``, then recompute ``units``
           from the rounded lots so the two fields never disagree.
        7. ``notional = units * entry``, the face value of the position in the
           quote currency. Reported for leverage awareness, not used in sizing.

    Rounding is always downward. Never nearest, never upward. Rounding up
    breaches the risk cap by construction, and on a small account the breach is
    proportionally large: at R2,000 the gap between one micro lot and two is a
    doubling of risk, not a rounding error. Rounding down costs a few cents of
    expected profit and keeps the rule intact, which is the trade the plan
    would make every time.

    Warnings populated on the result:
        * Computed lots below ``broker.min_lot``. The setup is valid and the
          engine has a view, but the account cannot express it at this stop
          distance without breaking the risk rule. This is not a defect to be
          engineered around. At R2,000 with R20 at risk and a channel stop 25
          pips wide, several G10 crosses size below any retail minimum, and the
          honest response is to trade fewer pairs, wait for setups with tighter
          stops, or grow the account. The one response that is not available is
          to take the minimum lot anyway: that silently converts a 1% trade into
          a 2-4% trade and the plan stops being a plan.
        * Rounding down moved the realised risk more than a stated tolerance
          below the intended risk, so the owner knows the trade is smaller than
          the ladder implies.
        * Stop distance below roughly twice the pair's typical spread, from
          ``broker.typical_spread_pips``. A stop that tight is inside the noise
          the broker itself creates.
        * ``entry`` equal to ``stop``, which would divide by zero. Return a
          zero-size result carrying the warning rather than raising, so a batch
          run over a shortlist does not abort on one bad row.

    Args:
        pair: Pair to size, e.g. ``"EURUSD"``.
        entry: Intended entry price, from the owner's chart.
        stop: Stop price, placed just beyond the opposite side of the channel or
            the key trendline as the plan requires.
        config: Risk limits, supplying account currency, balance and the band.
        rates: Current rates including the ZAR conversion leg. See `pip_value`.
        risk_fraction: Explicit fraction of balance to risk, normally from
            `risk_fraction_for`. Defaults to ``config.risk_per_trade_min``.
        broker: Execution constraints. Defaults to `DEFAULT_BROKER`, whose
            values the owner is expected to replace.

    Returns:
        A `PositionSize` with every field populated, including ``warnings``. A
        result with a non-empty ``warnings`` list is still returned rather than
        raised, because the caller needs to see the failed sizing to understand
        why a pair on the shortlist has no ticket behind it.

    Raises:
        MissingRateError: Propagated from `pip_value` when the ZAR conversion
            leg is missing. Sizing without it is not possible and must not be
            approximated.
    """
    raise NotImplementedError


def risk_fraction_for(conviction: Conviction) -> float:
    """Map a conviction level to the fraction of balance to risk.

    Reads `CONVICTION_RISK_FRACTION`. Kept as a function rather than exposing
    the mapping directly so the ladder can later account for consecutive
    losses, drawdown state, or a reduced-size return after a pause, without
    every call site changing.

    Args:
        conviction: The engine's conviction on the pair.

    Returns:
        A fraction in ``[0.01, 0.02]``, or ``0.0`` for `Conviction.NONE`, which
        the caller must treat as no trade rather than as a tiny trade.
    """
    raise NotImplementedError


def reward_to_risk(entry: float, stop: float, target: float) -> float:
    """Reward-to-risk ratio of a setup, in R multiples.

    ``abs(target - entry) / abs(entry - stop)``. Both legs are absolute, so the
    function is direction-agnostic and a short with target below entry scores
    the same as the mirror-image long.

    Args:
        entry: Intended entry price.
        stop: Stop price.
        target: Take-profit price, set from structure per the plan, meaning the
            next support or resistance level rather than a round number of pips.

    Returns:
        Reward per unit of risk. 2.0 means the target is twice as far as the
        stop.

    Raises:
        ValueError: If ``entry`` equals ``stop``, which has no defined ratio.

    Note:
        This measures geometry, not probability. A 5R target is only better than
        a 2R target if it is reachable, and a target beyond the far side of the
        channel usually is not. Sanity-check the target against structure before
        trusting the number this returns.
    """
    raise NotImplementedError


def min_acceptable_rr(conviction: Conviction) -> float:
    """Minimum reward-to-risk required for a trade at this conviction.

    Reads `MIN_REWARD_TO_RISK`. See that constant for why the requirement rises
    as conviction falls.

    Args:
        conviction: The engine's conviction on the pair.

    Returns:
        The minimum acceptable ratio. Infinite for `Conviction.NONE`.
    """
    raise NotImplementedError


def correlated_exposure(
    open_positions: Sequence[PositionSize],
) -> Mapping[str, float]:
    """Total risk fraction attributable to each currency across open positions.

    G10 FX has eight currencies and 28 pairs, which means positions overlap
    constantly. Long EURUSD and long GBPUSD are not two independent bets. They
    are one short-USD bet held through two tickets, and they will lose together
    on any USD rally regardless of what the euro and sterling are doing
    separately. Counting them as two positions each risking 1% understates the
    real exposure by half.

    Method:
        For each position, split its pair into base and quote. Attribute the
        position's risk fraction (``risk_amount / account_balance``) to BOTH
        legs. Sum per currency. The resulting map is compared leg by leg
        against ``RiskConfig.max_correlated_exposure``, which defaults to 4%,
        or two full-size trades pointing the same way.

    Attributing the full risk to both legs rather than splitting it in half is
    deliberate and conservative. A pair trade genuinely does put the whole stake
    on each leg's behaviour; the risk is not halved because two currencies are
    involved. The cost of this choice is that a genuinely hedged book, long
    EURUSD and long USDJPY, looks more exposed than it is. That is the right
    error to make on a R2,000 account.

    Args:
        open_positions: Positions currently live. Each supplies its own pair,
            risk amount and account balance, so no external state is needed.

    Returns:
        Mapping of ISO currency code to total risk fraction of the account, for
        every currency appearing in at least one open position. Currencies with
        no exposure are absent rather than present with 0.0.
    """
    raise NotImplementedError


def check_limits(
    open_positions: Sequence[PositionSize],
    proposed: PositionSize,
    config: RiskConfig,
    realised_pnl_today: float = 0.0,
    equity_peak: float | None = None,
) -> list[str]:
    """Return the reasons ``proposed`` must not be taken. Empty means take it.

    Returning reasons rather than a boolean is the point. The owner needs to
    know which limit bit, because "already three positions open" is a wait and
    "down 4% today" is a stop for the day, and those call for different
    behaviour. A bare False invites arguing with the number.

    Checks, all evaluated so the caller sees every breach rather than the first:
        Concurrent positions: ``len(open_positions) >= max_concurrent_positions``
            refuses the trade. Three is not a capital constraint, it is an
            attention constraint. The plan runs on 1h and 4h charts managed by
            hand, and a fourth open ticket is where management degrades.
        Correlated exposure: adding ``proposed`` must not push any single
            currency's total above ``max_correlated_exposure``. Compute
            `correlated_exposure` over ``open_positions + [proposed]`` and
            compare each leg. Name the offending currency and the resulting
            figure in the reason string.
        Daily loss: ``realised_pnl_today <= -max_daily_loss * account_balance``
            stops trading for the session. This is the plan's revenge-trading
            guard given a number. The threshold is realised loss only; an open
            position sitting at a loss is not yet evidence of anything.
        Drawdown pause: if ``equity_peak`` is supplied and current balance has
            fallen ``max_drawdown_pause`` (10%) or more below it, refuse and
            direct the owner to review the model rather than to resize. A
            drawdown that deep on a fundamental bias engine is more likely to
            mean the weights are wrong than that variance was unkind.

    Args:
        open_positions: Positions already live.
        proposed: The position being considered.
        config: Risk limits from the plan.
        realised_pnl_today: Closed profit and loss for the current session in
            the account currency, negative for a loss. Defaults to 0.0, which
            disables the daily-loss check for callers that do not track it.
        equity_peak: Highest account balance reached, in the account currency.
            ``None`` disables the drawdown check rather than assuming the
            current balance is the peak, which would hide an existing drawdown.

    Returns:
        Human-readable reasons for refusal, one per breached limit, in the order
        listed above. Empty list means every limit passed.
    """
    raise NotImplementedError
