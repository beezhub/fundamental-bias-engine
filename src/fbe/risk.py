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
damaging bug this project could ship, and it is the reason `convert_rate`
refuses to guess: a missing leg raises rather than defaulting to 1.0.

Units convention used throughout:
    * ``units`` are units of the BASE currency. 100,000 units of EURUSD is one
      standard lot and represents EUR 100,000.
    * ``lots`` are ``units / contract_size`` for the broker in question.
    * ``pip_size`` is the price increment one pip represents, in the QUOTE
      currency.
    * Every money figure on `PositionSize`, including ``notional``, is in the
      account currency. `PositionSize` states this as a contract and this module
      is bound by it: nothing leaves here quoted in a foreign currency.
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
    "CONVERSION_PIVOT",
    "CONVICTION_BAND_POSITION",
    "MIN_REWARD_TO_RISK",
    "MissingRateError",
    "pip_size",
    "convert_rate",
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


CONVERSION_PIVOT: str = "USD"
"""Currency used as the middle leg when no direct conversion rate exists.

USD, because it is the one currency that has a liquid quoted leg against every
G10 currency AND against ZAR. A retail feed can be relied on for USDZAR and for
the seven G10 dollar pairs; it cannot be relied on for ZARJPY or ZARCHF, which
are not quoted anywhere the owner will be looking. Pivoting through the dollar
is therefore not a fallback, it is the normal route for this account.
"""


CONVICTION_BAND_POSITION: Mapping[Conviction, float | None] = {
    Conviction.HIGH: 1.0,
    Conviction.MEDIUM: 0.5,
    Conviction.LOW: 0.0,
    Conviction.NONE: None,
}
"""Where each conviction level sits inside the configured risk band.

Deliberately expressed as a position in ``[0, 1]`` rather than as a fraction of
balance. The band's endpoints live in `RiskConfig.risk_per_trade_min` and
``risk_per_trade_max``, and hardcoding 0.01 and 0.02 here would duplicate them
and then drift from them. The drift has a concrete failure: an owner in
drawdown follows the advice in `journal.evaluate` and lowers
``risk_per_trade_max`` to 0.015, a hardcoded ladder still asks for 0.02, and
every high-conviction setup comes back clamped and carrying a warning that the
pre-trade checklist reads as a refusal. Deriving the ladder means changing the
band changes the ladder with it, which is what the owner intended when they
changed the band.

With the defaults of 1% and 2% this reproduces the intended ladder exactly:
HIGH 2%, MEDIUM 1.5%, LOW 1%.

Conviction modulates size, it does not gate entry. A LOW conviction pair with a
clean channel touch is still a trade, taken at the bottom of the band. The
alternative, refusing everything below HIGH, would leave a small account idle
for weeks at a time and push the owner toward the overtrading the plan warns
about. NONE maps to ``None``, meaning no position exists at any size.
"""


MIN_REWARD_TO_RISK: Mapping[Conviction, float] = {
    Conviction.HIGH: 1.5,
    Conviction.MEDIUM: 2.0,
    Conviction.LOW: 2.5,
    Conviction.NONE: float("inf"),
}
"""Minimum reward-to-risk a setup must offer, by conviction. A prior, not a finding.

The ladder rises as conviction falls, which is the opposite direction to size.
The assumption behind it is that hit rate rises with conviction, so a
high-conviction trade can be profitable at a nearer target while a
low-conviction trade has to pay more on the occasions it works.

That assumption is untested. It is exactly the relationship `journal.evaluate`
exists to measure, and by that function's own standard it needs roughly 30
closed trades per conviction bucket before it can be believed. Until then these
numbers are a starting position chosen because it is the conservative one: if
the assumption is wrong, requiring MORE reward on the trades the model is least
sure of costs missed trades rather than lost money.

What would confirm it: ``ConvictionStats.hit_rate`` rising from LOW through
MEDIUM to HIGH across buckets of adequate size. What would refute it: hit rate
flat or inverted across buckets. If it is refuted, this ladder has no basis and
should collapse to a single minimum applied to every trade, and
`CONVICTION_BAND_POSITION` should be reviewed at the same time, since it rests
on the same untested claim.
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
    """Raised when a rate needed to convert into the account currency is absent.

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


def convert_rate(
    from_currency: str,
    to_currency: str,
    rates: Mapping[str, float],
    pivot: str = CONVERSION_PIVOT,
) -> float:
    """Units of ``to_currency`` per one unit of ``from_currency``.

    Every money conversion in this module goes through here, so this is the one
    place the lookup rule is defined. Multiply an amount in ``from_currency`` by
    the returned factor to get the amount in ``to_currency``.

    Resolution order, first match wins:
        0. Identity. ``from_currency == to_currency`` returns 1.0 without
           touching ``rates``. This is the only circumstance in which this
           module ever produces a factor of 1.0.
        1. Direct, quoted as ``f"{from}{to}"``. Return the rate as is. For USD
           to ZAR that is ``rates["USDZAR"]``, so R18.50 per dollar.
        2. Direct, quoted inverted as ``f"{to}{from}"``. Return ``1 / rate``.
           For ZAR to USD that is ``1 / rates["USDZAR"]``.
        3. Two hops through ``pivot``, tried only when neither direct form is
           present. Resolve ``from -> pivot`` by rules 0 to 2 and ``pivot -> to``
           by rules 0 to 2, then multiply the two factors. No third hop is
           attempted: if a two-hop route through the dollar cannot be built, the
           rate set is not fit for sizing this trade.

    The pivot is not an edge case on this account, it is the normal path. JPY to
    ZAR is the worked example. A feed that supplies ``{"USDZAR": 18.50,
    "USDJPY": 155.00}``, which is exactly what the pre-trade checklist asks for,
    has no ``JPYZAR`` and no ``ZARJPY``. Rules 1 and 2 both miss. Rule 3
    resolves JPY to USD as ``1 / 155.00`` and USD to ZAR as ``18.50``, giving
    ``18.50 / 155.00 = 0.119355`` rand per yen. Without rule 3 every JPY cross
    would raise, and those are the pairs most likely to be tradeable at this
    account size, so the guard would refuse precisely the trades the account can
    actually take.

    Failure is loud and it stays loud. If no route exists, raise
    `MissingRateError` naming both currencies, the pivot tried, and the keys
    that were looked for. Do not return 1.0, do not substitute a stale rate, do
    not reuse the last successful conversion, do not silently widen the search
    to a second pivot. A wrong factor produces no visible symptom: the ticket
    fills, the stop sits where the chart put it, and only the amount of money at
    risk is wrong. An exception is recoverable in seconds; a silent factor error
    survives until the account is gone.

    Args:
        from_currency: ISO code of the amount being converted.
        to_currency: ISO code to convert into, normally the account currency.
        rates: Current mid rates keyed by market-convention pair string.
            Staleness matters: a rate from last week is a wrong rate, and the
            caller is responsible for freshness.
        pivot: Middle currency for rule 3. Defaults to `CONVERSION_PIVOT`.

    Returns:
        A strictly positive multiplier.

    Raises:
        MissingRateError: If no direct or single-pivot route exists, or if any
            rate on the chosen route is zero, negative or not finite. A
            non-positive rate is corrupt data, not a small number, and must not
            be inverted or multiplied through.
    """
    raise NotImplementedError


def pip_value(
    pair: str,
    units: float,
    account_currency: str,
    rates: Mapping[str, float],
) -> float:
    """Value of one pip of ``pair``, for ``units``, expressed in the account currency.

    The calculation always starts the same way. One pip on ``units`` of the base
    currency is worth ``units * pip_size(pair)`` in the QUOTE currency. The work
    is carrying that quote-currency amount back to the account currency, which
    is a single call to `convert_rate` from the quote currency to
    ``account_currency``. In full:

        ``units * pip_size(pair) * convert_rate(quote, account_currency, rates)``

    The three cases in the literature all fall out of that one line, and it is
    worth knowing which one this account is in:

    Case 1, quote currency equals account currency. `convert_rate` returns 1.0
    by identity. A USD account trading EURUSD gets ``units * 0.0001`` USD per
    pip. This is the case every textbook example uses and the case this account
    is never in.

    Case 2, the account currency is the BASE of the pair. A USD account trading
    USDJPY needs JPY to USD, which rule 2 resolves as the reciprocal of the
    pair's own current price. Note this uses the live price, not the entry
    price, so pip value drifts as the pair moves.

    Case 3, the account currency appears nowhere in the pair. This is the ZAR
    case and it is every trade this engine will ever produce, because no G10
    cross has a ZAR leg. For a dollar-quoted pair rule 1 resolves it directly
    from USDZAR. For every other quote currency, including all seven JPY
    crosses, rule 3 pivots through the dollar. See `convert_rate` for the worked
    JPY example and for the failure behaviour, which is to raise rather than
    guess.

    Args:
        pair: Pair being traded, e.g. ``"USDJPY"``.
        units: Position size in base-currency units. May be fractional during
            the solve inside `position_size`. Must be non-negative; direction is
            carried by the entry and stop, not by the sign of the size.
        account_currency: ISO code the account is denominated in, ``"ZAR"`` here.
        rates: Current mid rates keyed by pair string, sufficient for the route
            `convert_rate` needs.

    Returns:
        Value of one pip in ``account_currency``, always positive.

    Raises:
        MissingRateError: Propagated from `convert_rate` when no route exists.
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
    """Size a position so the stop costs the planned fraction of the account.

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
           than honouring it. Callers should source this from
           `risk_fraction_for`, which derives from the same config and therefore
           never trips the clamp.
        2. ``risk_amount = config.account_balance * risk_fraction``. This is the
           INTENDED risk, before rounding. On the plan's R2,000 that is R20 at
           1% and R40 at 2%, matching the figures the owner wrote down.
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
        7. ``realised_risk_amount = units * stop_distance_pips * value_per_pip``,
           recomputed from the ROUNDED units. See below.
        8. ``notional``: face value, in the ACCOUNT currency. ``units * entry``
           gives face value in the quote currency, so it must then be carried
           back through the same conversion leg as the risk figures:
           ``units * entry * convert_rate(quote, config.account_currency,
           rates)``. `PositionSize` requires every money field to be in the
           account currency and names this one explicitly. Leaving it in the
           quote currency would understate a dollar-quoted position roughly
           eighteen-fold on this account, in the single number shown for
           leverage awareness, and it would read as plausible while doing it.
           Reported for leverage awareness only, never used in sizing.

    Why ``realised_risk_amount`` is separate from ``risk_amount``:
        Rounding down protects the cap but it leaves intended and actual risk
        different, and it is the actual figure that matters twice. The pre-trade
        check confirms the trade sits inside the R20-R40 band, and that has to
        be checked against what is really at risk. The journal divides the
        outcome by it to get an R-multiple, and an R-multiple computed against
        the intended figure is overstated by the rounding ratio. On the worked
        USDJPY example that ratio is R35.81 against R40.00, so every R-multiple
        in the journal would read about 10.5% high, and those R-multiples are
        the sole input to the conviction calibration the whole model is judged
        on. Both the checklist and the journal read
        ``realised_risk_amount``. Neither reads ``risk_amount``.

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
          a 2-4% trade and the plan stops being a plan. Return zero units, zero
          lots and a zero ``realised_risk_amount`` alongside the warning.
        * ``realised_risk_amount`` more than a stated tolerance below
          ``risk_amount``, so the owner knows the trade is materially smaller
          than the ladder implies.
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
        rates: Current rates including whatever route the ZAR conversion needs.
            See `convert_rate`.
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
        MissingRateError: Propagated from `pip_value` or `convert_rate` when the
            ZAR conversion route is missing. Sizing without it is not possible
            and must not be approximated.
    """
    raise NotImplementedError


def risk_fraction_for(conviction: Conviction, config: RiskConfig) -> float:
    """Fraction of balance to risk at this conviction, derived from the configured band.

    Reads `CONVICTION_BAND_POSITION` for the level's position in ``[0, 1]`` and
    interpolates across the configured band:

        ``risk_per_trade_min + position * (risk_per_trade_max -
        risk_per_trade_min)``

    The band is never hardcoded here. `config` is a required argument precisely
    so that lowering ``risk_per_trade_max``, which `journal.evaluate` explicitly
    advises doing when the conviction ladder fails to earn its keep, moves the
    whole ladder down with it instead of leaving the top rung outside the band
    and clamped.

    Args:
        conviction: The engine's conviction on the pair.
        config: Risk limits supplying the band endpoints.

    Returns:
        A fraction inside ``[risk_per_trade_min, risk_per_trade_max]``, or
        ``0.0`` for `Conviction.NONE`, which the caller must treat as no trade
        rather than as a tiny trade.

    Raises:
        ValueError: If ``config.risk_per_trade_min`` exceeds
            ``risk_per_trade_max``, which would make the interpolation run
            backwards and quietly return a fraction outside the band.
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

        It also measures the PLANNED trade. The realised R-multiple in the
        journal is computed against ``realised_risk_amount`` and will differ
        wherever rounding moved the size.
    """
    raise NotImplementedError


def min_acceptable_rr(conviction: Conviction) -> float:
    """Minimum reward-to-risk required for a trade at this conviction.

    Reads `MIN_REWARD_TO_RISK`. That constant records what the ladder assumes
    and what evidence would confirm or refute it; the assumption is currently
    untested, so treat the returned number as a working threshold rather than a
    calibrated one.

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
        position's realised risk fraction (``realised_risk_amount /
        account_balance``) to BOTH legs. Sum per currency. The resulting map is
        compared leg by leg against ``RiskConfig.max_correlated_exposure``,
        which defaults to 4%, or two full-size trades pointing the same way.

        Realised rather than intended, for the same reason the checklist reads
        realised: what is on the book is what can be lost, and after rounding
        down that is reliably less than what was asked for. Using the intended
        figure would refuse trades on exposure the account does not actually
        carry.

    Attributing the full risk to both legs rather than splitting it in half is
    deliberate and conservative. A pair trade genuinely does put the whole stake
    on each leg's behaviour; the risk is not halved because two currencies are
    involved. The cost of this choice is that a genuinely hedged book, long
    EURUSD and long USDJPY, looks more exposed than it is. That is the right
    error to make on a R2,000 account.

    Args:
        open_positions: Positions currently live. Each supplies its own pair,
            realised risk amount and account balance, so no external state is
            needed.

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
