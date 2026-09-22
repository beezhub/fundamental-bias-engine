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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from math import isfinite
from typing import Protocol, runtime_checkable

from fbe.bias import UNCHECKED_SUFFIX
from fbe.config import BrokerConfig, RiskConfig
from fbe.types import Conviction, PositionSize
from fbe.universe import G10, split_pair

__all__ = [
    "BROKER_UNCONFIRMED",
    "JPY_PIP_SIZE",
    "STANDARD_PIP_SIZE",
    "CONVERSION_PIVOT",
    "MIN_STOP_SPREAD_MULTIPLE",
    "SIZING_SHORTFALL_TOLERANCE",
    "CONVICTION_BAND_POSITION",
    "MIN_REWARD_TO_RISK",
    "LIMITS",
    "LIMIT_CONCURRENT",
    "LIMIT_CORRELATED",
    "LIMIT_DAILY_LOSS",
    "LIMIT_DRAWDOWN",
    "LimitStatus",
    "LimitCheck",
    "LimitReport",
    "OpenPosition",
    "PositionRisk",
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


MIN_STOP_SPREAD_MULTIPLE: float = 2.0
"""Multiple of the typical spread a stop must clear before it is flagged.

Local to this module rather than in `RiskConfig`, because it is a reporting
threshold and not a risk limit: nothing refuses on it and nothing sizes from
it. Two because the spread is paid twice, once entering and once leaving, so a
stop inside that width is inside the cost of the round turn and the broker
takes the trade out before the market has had a view.

Indicative only, like the spreads it is applied to. It warns and does not
refuse, because `BrokerConfig.typical_spread_pips` is sampled at the London and New
York overlap and the owner watching their own terminal knows better than the
profile does.
"""


SIZING_SHORTFALL_TOLERANCE: float = 0.20
"""Fraction of intended risk that may be lost to lot rounding before warning.

Local to this module for the same reason as `MIN_STOP_SPREAD_MULTIPLE`: it
changes what is said, never what is traded.

Some shortfall is arithmetically unavoidable, because rounding down to a lot
step is what keeps the cap intact. The question this threshold answers is when
the gap stops being rounding and starts meaning the account cannot express the
ladder's intent. A fifth is the line: below it the trade is the trade that was
asked for, above it the size is close enough to the broker's floor that the
conviction rung has effectively been ignored.

Both worked examples in ``docs/risk-and-execution.md`` section 2 sit inside it
and are therefore quiet, including example B at 10.5%, which that document
presents as the normal consequence of a lot step. A warning that fires on the
routine case is a warning the reader learns to skip, and the one it would then
also skip is the refusal above it.
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


BROKER_UNCONFIRMED: str = "broker:unconfirmed"
"""Warning marker for a ticket sized from a broker profile nobody has confirmed.

Appended to `PositionSize.warnings` by `position_size` whenever the
`fbe.config.BrokerConfig` it was handed has ``confirmed`` false. A sized
position built on an unconfirmed ``min_lot`` is otherwise indistinguishable
from one built on a verified one, which is the case
`docs/decisions/0002-representing-not-known.md` names against ``DEFAULT_BROKER``
by name.

It follows the ``noun:state`` shape of `fbe.bias`'s blocker vocabulary, but it
is not one of those blockers and carries no `UNCHECKED_SUFFIX`: the profile's
values are used, so nothing was left unchecked, they are simply unconfirmed.
It is enumerated here and referenced, never typed as a literal at the emit
site, because a marker read by matching free text drifts in spelling and the
drift is silent.

`BrokerConfig.confirmed` is the single source of this fact. This marker is a
rendering of it, derived from the profile handed to `position_size` and never
from a second copy of the state.
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


def _checked_rate(key: str, quoted: float) -> float:
    """Return ``quoted``, or raise if it cannot be used as a conversion factor.

    Args:
        key: Pair string the rate was read from, named in the message so the
            corrupt entry can be found in the feed.
        quoted: The rate as the feed supplied it.

    Returns:
        ``quoted`` unchanged, in the same direction it was quoted.

    Raises:
        MissingRateError: If the rate is zero, negative, not finite, a
            ``bool``, or exactly 1.0.

    Note:
        A non-positive rate is corrupt data, not a small number. Zero divides,
        a negative silently flips the sign of every money figure downstream,
        and a NaN propagates into a position size that compares false against
        every limit it is later checked against. Refusing the rate is the only
        one of those four outcomes a reader would notice.

        Exactly 1.0 is refused because callers reach here only after `_leg`
        has already returned on identity, so this is a quote between two
        currencies that are not the same one. It is the eighteenfold bug
        wearing a value instead of an absence: a manual YAML or a mapping
        initialised with ones resolves, sizes eighteen times too large on a
        rand account, and reports the intended risk amount on a ticket
        carrying no warnings. Every other guard in this module fires on
        absence and not one of them sees this.

        The cost of being wrong is asymmetric, and that is the whole argument.
        A genuine mid rate of exactly 1.00000 is possible, since USDCHF and
        EURCHF have both traded through parity. Refusing one raises an error
        the owner reads and works around in seconds. Accepting a placeholder
        is a position eighteen times too large that nothing on the screen
        reports. Note this refuses a quoted leg only: a pivot product of
        exactly 1.0 built from two legitimate legs is arithmetic rather than a
        placeholder, and 1.25 x 0.8 is exactly 1.0 in binary floating point.

        ``bool`` is refused separately because ``isfinite(True)`` is True and
        ``True <= 0.0`` is False, so it would otherwise pass as a factor of
        1.0, and ``bool`` is a subtype of ``int``, so a type checker does not
        object either. A manual YAML is the realistic producer: ``USDZAR: yes``
        and ``USDZAR: on`` both parse as ``True``.

    """
    if isinstance(quoted, bool):
        raise MissingRateError(
            f"rate {key} is the boolean {quoted!r}, not a number. A YAML "
            f"value of yes, no, on or off parses this way; write the rate as "
            f"a decimal instead."
        )
    if not isfinite(quoted) or quoted <= 0.0:
        raise MissingRateError(
            f"rate {key} is {quoted!r}, which cannot be used as a conversion "
            f"factor. A rate must be finite and strictly positive."
        )
    if quoted == 1.0:
        raise MissingRateError(
            f"rate {key} is exactly 1.0 between two different currencies, "
            f"refused as an unpopulated placeholder rather than used. Sizing "
            f"on a placeholder 1.0 is the failure this module exists to "
            f"prevent. If the pair really is at parity, supply the two pivot "
            f"legs instead."
        )
    return float(quoted)


def _leg(source: str, target: str, rates: Mapping[str, float]) -> float | None:
    """Resolve one hop by identity, direct quote or inverted quote, or not at all.

    Args:
        source: ISO code being converted from.
        target: ISO code being converted into.
        rates: Mid rates keyed by market-convention pair string.

    Returns:
        Units of ``target`` per one unit of ``source``, or ``None`` when
        neither quote direction is present. ``None`` means absent, which is a
        different answer from a rate that is present and unusable: that one
        raises rather than returning here, so a corrupt quote is never routed
        around by falling through to the pivot.

    Raises:
        MissingRateError: If a quote exists but is zero, negative or not
            finite.

    Note:
        Identity returns 1.0 before ``rates`` is touched at all. It is the
        only way a factor of 1.0 is read from ``rates``, because
        `_checked_rate` refuses a quoted 1.0 between two different currencies.

    """
    if source == target:
        return 1.0
    key = f"{source}{target}"
    quoted = rates.get(key)
    if quoted is not None:
        return _checked_rate(key, quoted)
    inverse_key = f"{target}{source}"
    inverted = rates.get(inverse_key)
    if inverted is not None:
        return _checked_rate(
            f"1/{inverse_key}", 1.0 / _checked_rate(inverse_key, inverted)
        )
    return None


def _finite_product(
    source: str, middle: str, target: str, first: float, second: float
) -> float:
    """Multiply two resolved legs, refusing a product that is no longer usable.

    Args:
        source: ISO code the route starts at, named in the message only.
        middle: Pivot currency, named in the message only.
        target: ISO code the route ends at, named in the message only.
        first: Units of ``middle`` per one unit of ``source``, already checked.
        second: Units of ``target`` per one unit of ``middle``, already checked.

    Returns:
        Units of ``target`` per one unit of ``source``, strictly positive and
        finite.

    Raises:
        MissingRateError: If the product underflows to zero or overflows to
            infinity. Both need absurd rates, so this is about the contract
            holding rather than about a case seen in a feed. Exactly 1.0 is
            NOT refused here, unlike a quoted leg: a product is arithmetic on
            two rates that were each already accepted.

    """
    product = first * second
    if not isfinite(product) or product <= 0.0:
        raise MissingRateError(
            f"the route {source} to {target} through {middle} resolves to "
            f"{product!r}, which cannot be used as a conversion factor. The "
            f"two legs were {first!r} and {second!r}."
        )
    return product


def _round_down_to_step(value: float, step: float) -> float:
    """Round ``value`` down to the nearest multiple of ``step``.

    Args:
        value: Lots as the sizing solve produced them, always non-negative.
        step: The broker lot step, strictly positive.

    Returns:
        The largest multiple of ``step`` at or below ``value``. Never above it,
        under any input, because rounding up breaches the risk cap by
        construction.

    Raises:
        ValueError: If ``step`` is not strictly positive.

    Note:
        The arithmetic is done in `decimal` rather than on floats, and that is
        not fastidiousness. ``0.29 / 0.01`` evaluates to 28.999999999999996 in
        binary floating point, so flooring the raw quotient turns 0.29 lots
        into 0.28 and silently drops a whole lot step of a position the account
        asked for. Fourteen multiples of 0.01 below 2.0 lots behave this way.
        The conversion goes through ``str`` so each value is read at the
        decimal it displays as, which is the number the broker's contract
        specification is written in.

    """
    if not step > 0.0:
        raise ValueError(f"lot_step must be strictly positive, got {step!r}")
    quantum = Decimal(str(step))
    steps = (Decimal(str(value)) / quantum).to_integral_value(rounding=ROUND_FLOOR)
    return float(steps * quantum)


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
           touching ``rates``. This is the only circumstance in which a factor
           of 1.0 is ever READ from this module's inputs: a quoted rate of
           exactly 1.0 between two different currencies is refused as an
           unpopulated placeholder, for the reason in `_checked_rate`. A pivot
           product can still come to 1.0 by arithmetic, and that is allowed.
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
            rate on the chosen route is zero, negative, not finite, a ``bool``
            or exactly 1.0. A non-positive rate is corrupt data, not a small
            number, and must not be inverted or multiplied through. A quoted
            1.0 between two different currencies is treated as an unpopulated
            placeholder; see `_checked_rate` for why that is worth a false
            positive at parity.

    """
    source = from_currency.upper()
    target = to_currency.upper()
    middle = pivot.upper()

    direct = _leg(source, target, rates)
    if direct is not None:
        return direct

    if middle not in (source, target):
        first = _leg(source, middle, rates)
        second = _leg(middle, target, rates)
        if first is not None and second is not None:
            # Re-checked because each leg being finite and positive does not
            # make their product so: it can still underflow to zero or
            # overflow to infinity, and the documented return contract is a
            # strictly positive multiplier. A zero factor would divide inside
            # `position_size` and an infinite one would be reported as a size
            # below the broker minimum, blaming the lot step for a bad rate.
            return _finite_product(source, middle, target, first, second)

    looked_for = (
        f"{source}{target}",
        f"{target}{source}",
        f"{source}{middle}",
        f"{middle}{source}",
        f"{middle}{target}",
        f"{target}{middle}",
    )
    raise MissingRateError(
        f"no rate from {source} to {target}: neither a direct quote nor a "
        f"single hop through {middle} could be built. Looked for "
        f"{', '.join(looked_for)}. Supply one of these and do not substitute "
        f"a rate of 1.0, a stale rate or a wider search."
    )


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
    if units < 0.0:
        raise ValueError(
            f"units must be non-negative, got {units!r}. Direction is carried "
            f"by the entry and the stop, not by the sign of the size."
        )
    normalised = pair.upper()
    _, quote = split_pair(normalised)
    return (
        units
        * pip_size(normalised)
        * convert_rate(quote, account_currency.upper(), rates)
    )


def position_size(
    pair: str,
    entry: float,
    stop: float,
    config: RiskConfig,
    rates: Mapping[str, float],
    broker: BrokerConfig,
    risk_fraction: float | None = None,
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
        * `BROKER_UNCONFIRMED` when ``broker.confirmed`` is false, on every
          result including a refusal. The profile's values are used, so this is
          not a check that failed to run; it says the owner has not confirmed
          them. `fbe doctor` reports the same fact from the config, and the two
          read the one `BrokerConfig.confirmed`.
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
        * ``spread:unchecked`` when ``broker.typical_spread_pips`` carries no
          entry for the pair, so the check above did not run. A check that
          could not run is not a check that passed, and the suffix is
          `fbe.bias.UNCHECKED_SUFFIX`, the convention the pair filters already
          use for the same distinction. It matters more than it looks: a
          broker profile confirmed with a sparser spread table than the default
          leaves ``typical_spread_pips`` short of some pairs, and a consumer
          reading "any warning blocks" would then refuse them. Consumers should
          treat a line carrying this suffix as non-blocking, as
          `fbe.bias.apply_filters` does.
        * The ``risk_fraction`` clamp described at step 1, which is the only
          warning here that reports an adjustment rather than an observation.
        * ``entry`` equal to ``stop``, which would divide by zero. Return a
          zero-size result carrying the warning rather than raising, so a batch
          run over a shortlist does not abort on one bad row. A price that is
          negative or not finite is a different case and raises, because it is
          a malformed input rather than a fact about the trade.

    Not checked here, and deliberately: ``broker.max_lot`` is never compared
    against the computed size. It can only bind on an account far larger than
    the one the plan describes, and exceeding it produces a ticket the broker
    rejects rather than a breach of the 1-2% rule, so nothing about the risk
    cap is at stake. The list above is otherwise the complete set, and this
    paragraph exists so a future caller sizing on a grown account learns the
    gap from the contract rather than from a rejected order.

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
        broker: The resolved `fbe.config.BrokerConfig` for this run, supplying
            the lot geometry and the typical spreads. It has no default: a
            caller that forgot to pass it would otherwise size against a module
            constant, which is the second-home defect this issue removed, so the
            profile is always the one the run resolved. When its ``confirmed``
            is false, every result carries `BROKER_UNCONFIRMED` in ``warnings``.

    Returns:
        A `PositionSize` with every field populated, including ``warnings``. A
        result with a non-empty ``warnings`` list is still returned rather than
        raised, because the caller needs to see the failed sizing to understand
        why a pair on the shortlist has no ticket behind it.

    Raises:
        MissingRateError: Propagated from `pip_value` or `convert_rate` when the
            ZAR conversion route is missing. Sizing without it is not possible
            and must not be approximated. Resolved before the zero stop
            distance case below, so a row that is both unpriceable and badly
            formed raises rather than coming back as a warning: a warning would
            report a soft failure on a pair nothing here can size at all.
        ValueError: On any of six malformed inputs: ``broker.lot_step`` not
            strictly positive, ``pair`` not six characters, ``entry`` or
            ``stop`` not a finite positive price, ``risk_fraction`` supplied
            and not finite, and ``config.account_balance`` not a finite
            positive amount. None is a fact about the trade, so none is
            reported as a warning. The balance is the newest of the six and is
            checked here because nothing else checks it and because
            `fbe.types.PositionSize.realised_risk_fraction` divides by it.

    """
    normalised = pair.upper()
    _, quote = split_pair(normalised)
    account_currency = config.account_currency.upper()
    warnings: list[str] = []

    # Emitted once, before any size is computed, so it rides every result this
    # returns, including the zero-size refusals: a refused ticket sized on an
    # unconfirmed profile is still sized on an unconfirmed profile. Derived from
    # the profile handed in and never from a second copy of the state, so the
    # config is the only place the fact lives.
    if not broker.confirmed:
        warnings.append(
            f"{BROKER_UNCONFIRMED}: {broker.name} is an unconfirmed profile. Its "
            f"min_lot {broker.min_lot:g}, lot_step {broker.lot_step:g} and "
            f"contract_size {broker.contract_size:g} decide which pairs are "
            f"tradeable and have not been checked against a broker contract "
            f"specification; confirm them per docs/risk-and-execution.md "
            f"section 7."
        )

    # Checked before anything is computed, because every guard further down is
    # a `<` or an `==` and all of those are False for a NaN. Without this, a
    # NaN price sails past the zero-distance return, past the below-minimum
    # refusal and past the shortfall check, and comes back as a PositionSize
    # whose every money field is NaN and whose `warnings` tuple is EMPTY.
    # Section 8 of docs/risk-and-execution.md lets the owner tick the sizing
    # box when `warnings` is empty, so an unchecked input would produce a
    # pass-shaped result. A malformed price is not a fact about the trade, so
    # it raises here rather than being reported as a warning.
    for label, price in (("entry", entry), ("stop", stop)):
        if not isfinite(price) or price <= 0.0:
            raise ValueError(
                f"{label} must be a finite, strictly positive price, got "
                f"{price!r}. A negative price yields a negative notional and "
                f"a NaN yields a size that passes every check by failing "
                f"every comparison."
            )
    # The balance is the same class of input and is checked in the same place.
    # Nothing else validates it: `Config.validate` does not, so
    # `RiskConfig(account_balance=0.0)` is constructible. Unchecked it reaches
    # `PositionSize.realised_risk_fraction` as a denominator, where a zero
    # raises out of whatever is rendering the ticket and a negative reports a
    # position risking less than nothing. A balance is not a fact about the
    # trade, so it raises here rather than arriving as a warning.
    if not isfinite(config.account_balance) or config.account_balance <= 0.0:
        raise ValueError(
            f"account_balance must be a finite, strictly positive amount, got "
            f"{config.account_balance!r}. Every money figure on the result is "
            f"a share of it, so a zero has no fraction to report and a NaN "
            f"comes back as a size whose warnings tuple is empty."
        )
    if risk_fraction is not None and not isfinite(risk_fraction):
        raise ValueError(
            f"risk_fraction must be finite, got {risk_fraction!r}. Clamping a "
            f"NaN returns a NaN while reporting that it was clamped, which is "
            f"a warning that states something untrue."
        )

    requested = config.risk_per_trade_min if risk_fraction is None else risk_fraction
    fraction = min(max(requested, config.risk_per_trade_min), config.risk_per_trade_max)
    if fraction != requested:
        warnings.append(
            f"risk_fraction {requested:g} is outside the configured band "
            f"[{config.risk_per_trade_min:g}, {config.risk_per_trade_max:g}]; "
            f"clamped to {fraction:g} rather than honoured"
        )

    risk_amount = config.account_balance * fraction
    stop_distance_pips = abs(entry - stop) / pip_size(normalised)

    # Both conversions are resolved before anything is reported, so a missing
    # route raises instead of producing a result with a zero size that reads
    # like a refusal the trade earned.
    value_per_pip = pip_value(normalised, 1.0, account_currency, rates)
    conversion = convert_rate(quote, account_currency, rates)

    def _result(
        units: float, lots: float, realised: float, notional: float
    ) -> PositionSize:
        return PositionSize(
            pair=normalised,
            account_currency=account_currency,
            account_balance=config.account_balance,
            risk_fraction=fraction,
            risk_amount=risk_amount,
            realised_risk_amount=realised,
            entry=entry,
            stop=stop,
            stop_distance_pips=stop_distance_pips,
            units=units,
            lots=lots,
            notional=notional,
            warnings=tuple(warnings),
        )

    if stop_distance_pips == 0.0:
        warnings.append(
            f"entry {entry:g} equals stop {stop:g}, so the stop distance is "
            f"zero and no size can be derived from it"
        )
        return _result(0.0, 0.0, 0.0, 0.0)

    typical_spread = broker.typical_spread_pips.get(normalised)
    if typical_spread is None:
        warnings.append(
            f"spread{UNCHECKED_SUFFIX}: {broker.name} lists no typical spread "
            f"for {normalised}, so the tight-stop check did not run. This is "
            f"a statement about what was checked, not about the trade."
        )
    elif stop_distance_pips < MIN_STOP_SPREAD_MULTIPLE * typical_spread:
        warnings.append(
            f"stop distance {stop_distance_pips:.1f} pips is inside "
            f"{MIN_STOP_SPREAD_MULTIPLE:g}x the typical spread of "
            f"{typical_spread:g} pips for {normalised}"
        )

    raw_units = risk_amount / (stop_distance_pips * value_per_pip)
    raw_lots = raw_units / broker.contract_size
    lots = _round_down_to_step(raw_lots, broker.lot_step)

    if lots < broker.min_lot:
        warnings.append(
            f"computed size {raw_lots:.6f} lots rounds down to {lots:g} at "
            f"{broker.name}'s {broker.lot_step:g} step, below its minimum of "
            f"{broker.min_lot:g} lots; refusing to size rather than taking the "
            f"minimum, which would risk more than the configured band allows"
        )
        return _result(0.0, 0.0, 0.0, 0.0)

    units = lots * broker.contract_size
    realised = units * stop_distance_pips * value_per_pip
    notional = units * entry * conversion

    if realised < risk_amount * (1.0 - SIZING_SHORTFALL_TOLERANCE):
        warnings.append(
            f"rounding to {broker.name}'s {broker.lot_step:g} lot step leaves "
            f"{realised:.2f} at risk against an intended {risk_amount:.2f}, a "
            f"materially smaller trade than the risk fraction asked for"
        )

    return _result(units, lots, realised, notional)


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

        **Do not chain the NONE case into `position_size`.** That function
        clamps a below-band fraction UP to ``risk_per_trade_min`` and reports
        it as a warning rather than refusing, which is correct for a caller who
        asked for too little and wrong for one who asked for nothing. So
        ``position_size(..., risk_fraction=risk_fraction_for(conviction,
        config))`` turns "the engine has no view on this pair" into a trade at
        the band floor, described on the ticket as a clamp. Branch on NONE
        before sizing. The plan's R2,000 hides this, because the broker minimum
        refuses the resulting size anyway; it appears as the account grows,
        which is the worst way for it to arrive.

    Raises:
        ValueError: If either band endpoint is not finite or is negative, or if
            ``config.risk_per_trade_min`` exceeds ``risk_per_trade_max``. An
            inverted band makes the interpolation run backwards and quietly
            returns a fraction outside it. A non-finite endpoint slips past
            that check entirely, because ``min > max`` is False for any NaN,
            and returns a NaN from every rung. A negative floor is the quietest
            of the three: it leaves MEDIUM at half the plan's intended minimum
            while still inside the configured band, so nothing downstream
            clamps it and nothing warns.
        KeyError: If ``conviction`` is absent from `CONVICTION_BAND_POSITION`,
            which means a level was added to `Conviction` without a position in
            the band. Loud is correct: there is no defensible default position
            for a level nobody has placed.

    """
    # All three checks run before the NONE early return, so that one entry
    # point cannot accept a band every other entry point refuses.
    #
    # Non-finite first, because `min > max` is False for any NaN, so the
    # inversion check below cannot see one. A NaN band returns a NaN fraction
    # from every rung, and an infinite ceiling returns NaN from LOW alone,
    # since `0.0 * inf` is NaN. `Config.validate()` shares the blind spot, so
    # nothing else refuses it either.
    for label, endpoint in (
        ("risk_per_trade_min", config.risk_per_trade_min),
        ("risk_per_trade_max", config.risk_per_trade_max),
    ):
        if not isfinite(endpoint):
            raise ValueError(
                f"{label} is {endpoint!r}, so the band has no endpoint to "
                f"interpolate across. Every rung would return a NaN, which "
                f"compares False against every limit it is later checked "
                f"against."
            )
        if endpoint < 0.0:
            raise ValueError(
                f"{label} is {endpoint!r}. A negative endpoint puts part of "
                f"the ladder below zero and the rest below the plan's floor "
                f"while still inside the configured band, so `position_size` "
                f"neither clamps it nor warns and the ticket carries no "
                f"warnings at all."
            )
    if config.risk_per_trade_min > config.risk_per_trade_max:
        raise ValueError(
            f"risk_per_trade_min {config.risk_per_trade_min:g} exceeds "
            f"risk_per_trade_max {config.risk_per_trade_max:g}. Interpolating "
            f"across an inverted band returns the ceiling for LOW and the "
            f"floor for HIGH, so the engine's strongest calls become its "
            f"smallest positions and nothing in the result says so."
        )
    position = CONVICTION_BAND_POSITION[conviction]
    if position is None:
        return 0.0
    span = config.risk_per_trade_max - config.risk_per_trade_min
    return config.risk_per_trade_min + position * span


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
        ValueError: If ``entry`` equals ``stop``, which has no defined ratio,
            or if any of the three prices is not finite. A NaN ratio compares
            False against every minimum in `MIN_REWARD_TO_RISK`, so whether a
            nonsense setup is refused depends on whether the caller wrote its
            check as ``rr >= minimum`` or ``rr < minimum``. Refusing the input
            makes both spellings behave the same.

    Note:
        This measures geometry, not probability. A 5R target is only better than
        a 2R target if it is reachable, and a target beyond the far side of the
        channel usually is not. Sanity-check the target against structure before
        trusting the number this returns.

        It also measures the PLANNED trade. The realised R-multiple in the
        journal is computed against ``realised_risk_amount`` and will differ
        wherever rounding moved the size.

    """
    for label, price in (("entry", entry), ("stop", stop), ("target", target)):
        if not isfinite(price):
            raise ValueError(
                f"{label} must be a finite price, got {price!r}. A NaN ratio "
                f"compares False against every minimum in MIN_REWARD_TO_RISK, "
                f"so a caller spelling its check `if rr < minimum` lets the "
                f"setup through."
            )
    if (target - entry) * (stop - entry) > 0.0:
        raise ValueError(
            f"target {target:g} and stop {stop:g} are on the same side of "
            f"entry {entry:g}, which is untradeable whichever way the setup "
            f"is meant: read long, the target is behind the entry; read "
            f"short, the stop sits between entry and target and is hit first. "
            f"No direction argument is needed to see this, and the ratio "
            f"would otherwise come back positive and clear every minimum."
        )
    risk = abs(entry - stop)
    if risk == 0.0:
        raise ValueError(
            f"entry {entry:g} equals stop {stop:g}, so the reward-to-risk "
            f"ratio has no defined value. Returning infinity here would read "
            f"as the best setup ever seen and clear every minimum."
        )
    return abs(target - entry) / risk


def min_acceptable_rr(conviction: Conviction) -> float:
    """Minimum reward-to-risk required for a trade at this conviction.

    Reads `MIN_REWARD_TO_RISK`. That constant records what the ladder assumes
    and what evidence would confirm or refute it; the assumption is currently
    untested, so treat the returned number as a working threshold rather than a
    calibrated one.

    Args:
        conviction: The engine's conviction on the pair.

    Returns:
        The minimum acceptable ratio. Infinite for `Conviction.NONE`, so that
        no finite reward-to-risk can clear it. A large finite sentinel would be
        a bar a generous enough target beats.

    Raises:
        KeyError: If ``conviction`` is absent from `MIN_REWARD_TO_RISK`, for
            the same reason `risk_fraction_for` raises on a level with no band
            position.

    """
    return MIN_REWARD_TO_RISK[conviction]


LIMIT_CONCURRENT: str = "max_concurrent_positions"
LIMIT_CORRELATED: str = "max_correlated_exposure"
LIMIT_DAILY_LOSS: str = "max_daily_loss"
LIMIT_DRAWDOWN: str = "max_drawdown_pause"

LIMITS: Mapping[str, str] = {
    LIMIT_CONCURRENT: (
        "Attention, not capital. The plan runs on 1h and 4h charts managed by "
        "hand, and a fourth open ticket is where management degrades."
    ),
    LIMIT_CORRELATED: (
        "Long EURUSD and long GBPUSD is one short-USD bet wearing two tickets. "
        "Counting them as two independent trades understates the real exposure."
    ),
    LIMIT_DAILY_LOSS: (
        "Revenge trading, given a number. Realised losses only: an open trade "
        "sitting underwater is not yet evidence of anything."
    ),
    LIMIT_DRAWDOWN: (
        "The model, not the trader. A drawdown this deep more likely means the "
        "weights are wrong than that variance was unkind."
    ),
}
"""Every limit `check_limits` evaluates, and the reason each one exists.

The key is the `RiskConfig` field holding the limit's number, so the report can
be read next to the config without a translation table, and so a limit cannot be
added here without a configured number behind it. Iteration order is evaluation
order and print order.

The reason is carried in the code rather than only in the document because a
limit nobody understands is a limit that gets disabled the first time it is
inconvenient. ``tests/test_limit_checks.py`` asserts these keys and the table in
section 4 of ``docs/risk-and-execution.md`` hold the same four limits in the
same order, so the next addition cannot land in one place only.

Every entry is reported on every call. There is no circumstance in which a limit
is left out of the answer: if it could not be evaluated it is reported as
`LimitStatus.NOT_PERFORMED`, because an omitted limit reads to a consumer
exactly like a limit that passed.
"""


class LimitStatus(StrEnum):
    """What happened to one limit on one call.

    Three outcomes, not two, and the third is the reason this enum exists. A
    check that could not run is not a check that passed. ADR 0002 rule 2.

    Values are the words a ticket prints, so the terminal output, the JSON
    export and this enum cannot drift into three vocabularies for one fact.
    """

    CLEAR = "clear"
    """Performed, and the proposed trade is inside the limit."""

    BREACHED = "breached"
    """Performed, and the proposed trade is outside the limit."""

    NOT_PERFORMED = "not performed"
    """An input the limit needs was not supplied, so nothing was compared.

    Not a refusal and not a pass. The trade is not blocked by this outcome,
    because two of the four limits have no automated source at all today and a
    blanket refusal would be argued away within a week, taking the real
    breaches with it. It does mean `LimitReport.all_clear` is false and the
    pre-trade checklist box for that limit has to be ticked by hand.
    """


@dataclass(frozen=True, slots=True)
class LimitCheck:
    """The outcome of one limit, with the figures that produced it.

    Attributes:
        limit: Key from `LIMITS`, which is also the `RiskConfig` field name.
        status: Clear, breached or not performed.
        detail: One line naming the numbers. For a performed check, the
            measured figure and the ceiling it was compared against, with money
            in the account currency (ZAR) and exposure as a percentage of
            balance. For a breach of the correlated limit, the offending
            currency by name, because "too much exposure" does not tell the
            owner which ticket to drop. For a not-performed check, the input
            that was missing. Never empty: a check with nothing to say about
            itself is indistinguishable from one that was never run.

    """

    limit: str
    status: LimitStatus
    detail: str


@dataclass(frozen=True, slots=True)
class LimitReport:
    """Every limit's outcome for one proposed trade.

    The old contract was ``list[str]``, empty meaning take the trade. That
    cannot express the case this account is actually in, where two of the four
    limits have no input at all and the other two had no open book to count
    against, so the empty list said "all clear" when it meant "nothing was
    checked".

    There is deliberately no shortcut from this object to permission. Reading
    `all_clear` is the only way to get a yes, and it is false whenever any limit
    was not performed, so a caller cannot obtain an all-clear from a run that
    did not check everything.

    Attributes:
        checks: One `LimitCheck` per entry in `LIMITS`, in that order. Every
            limit appears exactly once, enforced in ``__post_init__``.

    Raises:
        ValueError: If ``checks`` does not cover every limit in `LIMITS`
            exactly once. A partial report is the original defect wearing a new
            shape: a consumer iterating what it was given would find nothing
            wrong with a limit that was silently dropped.

    """

    checks: tuple[LimitCheck, ...]

    def __post_init__(self) -> None:
        """Refuse a report that does not answer for every limit."""
        seen = [check.limit for check in self.checks]
        if sorted(seen) != sorted(LIMITS):
            missing = sorted(set(LIMITS) - set(seen))
            extra = sorted(set(seen) - set(LIMITS))
            raise ValueError(
                "a limit report must cover every limit exactly once; "
                f"missing {missing}, unexpected {extra}, got {seen}"
            )

    @property
    def all_clear(self) -> bool:
        """True only when every limit was performed and every one of them passed.

        False when anything was breached AND false when anything was not
        performed. The two are different facts, which is why they are also
        available separately, but neither of them is permission to trade.
        """
        return all(check.status is LimitStatus.CLEAR for check in self.checks)

    @property
    def breached(self) -> tuple[LimitCheck, ...]:
        """Limits that were checked and failed. These are reasons to stand aside."""
        return tuple(
            check for check in self.checks if check.status is LimitStatus.BREACHED
        )

    @property
    def not_performed(self) -> tuple[LimitCheck, ...]:
        """Limits that had no input. These are the ones the owner checks by hand."""
        return tuple(
            check for check in self.checks if check.status is LimitStatus.NOT_PERFORMED
        )


@runtime_checkable
class OpenPosition(Protocol):
    """The three facts a limit check needs about a position already on the book.

    Deliberately not `PositionSize`. A position already open is held by the
    journal, and a caller that has a `fbe.journal.TradeRecord` should not have
    to invent an entry price, a stop and a pip distance to ask whether one more
    ticket fits. Interface segregation: where a caller needs only part of a
    type, pass the part. The attribute names are the journal's, so a
    `TradeRecord` satisfies this structurally with no adapter.

    ``risk_amount`` here is the REALISED money at risk in the account currency,
    which is what `TradeRecord.risk_amount` holds. `PositionSize.risk_amount` is
    the INTENDED figure before the lot size was rounded down, and it is the
    wrong number for every limit, so `PositionSize` is intentionally left unable
    to satisfy this protocol. Convert one with
    `PositionRisk.from_position_size`, which reads ``realised_risk_amount``.

    Attributes:
        pair: Six-character pair in market convention. Both legs carry the
            position's full risk in `correlated_exposure`.
        risk_amount: Realised money at risk in the account currency (ZAR),
            positive.
        account_balance_at_entry: Balance the size was derived from, in the
            account currency. Risk fractions are computed against this rather
            than against the current balance, because that is what the position
            was actually sized on.

    """

    @property
    def pair(self) -> str:
        """Six-character pair in market convention, e.g. ``"EURUSD"``."""
        ...

    @property
    def risk_amount(self) -> float:
        """Realised money at risk in the account currency (ZAR), positive."""
        ...

    @property
    def account_balance_at_entry(self) -> float:
        """Balance the position was sized on, in the account currency (ZAR)."""
        ...


@dataclass(frozen=True, slots=True)
class PositionRisk:
    """A concrete `OpenPosition`, for callers that do not hold a journal record.

    Used for the proposed trade inside `check_limits`, which arrives as a
    `PositionSize` and has to be compared against open positions on the same
    terms.

    Attributes:
        pair: Six-character pair in market convention.
        risk_amount: Realised money at risk in the account currency (ZAR).
        account_balance_at_entry: Balance the size was derived from, same
            currency.

    """

    pair: str
    risk_amount: float
    account_balance_at_entry: float

    @classmethod
    def from_position_size(cls, position: PositionSize) -> PositionRisk:
        """Narrow a sized position to the three fields the limits read.

        Reads ``realised_risk_amount``, never ``risk_amount``. The intended
        figure is the one before the lot size was rounded down, it is routinely
        10% or more above what is actually on the book at this account size, and
        using it here would refuse trades on exposure the account does not
        carry.

        Args:
            position: A freshly sized position, money fields in the account
                currency.

        Returns:
            The same position expressed as an `OpenPosition`.

        """
        return cls(
            pair=position.pair,
            risk_amount=position.realised_risk_amount,
            account_balance_at_entry=position.account_balance,
        )


def correlated_exposure(
    open_positions: Sequence[OpenPosition],
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
        position's realised risk fraction (``risk_amount /
        account_balance_at_entry``, both in the account currency) to BOTH legs.
        Sum per currency. The resulting map is compared leg by leg against
        ``RiskConfig.max_correlated_exposure``, which defaults to 4%, or two
        full-size trades pointing the same way.

        Realised rather than intended, for the same reason the checklist reads
        realised: what is on the book is what can be lost, and after rounding
        down that is reliably less than what was asked for. Using the intended
        figure would refuse trades on exposure the account does not actually
        carry.

    **The denominators are not all the same, and the error is not symmetric.**
    Each position is divided by the balance it was sized on, so positions
    opened at different balances contribute fractions of different things, and
    the sum is not a fraction of any single account. The caller compares it
    against `RiskConfig.max_correlated_exposure`, which is defined on the
    current balance.

    That is per this function's contract and it is the right reading while the
    balance is flat or rising: a ticket opened at R2,000 risking R20 really is
    the 1% position it was sized as. It understates after a drawdown, by
    exactly the ratio of entry balance to current balance, which makes it worst
    precisely when the cap is load-bearing. Two R40 tickets opened at R4,000
    report 2% on the shared leg; on a balance now down to R2,000 that same R80
    is 4%, and a third full-size trade would be authorised on a leg already at
    the cap.

    Nothing here can fix that, because choosing a denominator is the caller's
    decision and `check_limits` is where it belongs. What this function owes is
    to say so rather than to let ``Returns`` read as though one balance were
    involved. `docs/risk-and-execution.md` section 6 records the same problem
    for a withdrawal.

    Attributing the full risk to both legs rather than splitting it in half is
    deliberate and conservative. A pair trade genuinely does put the whole stake
    on each leg's behaviour; the risk is not halved because two currencies are
    involved. The cost of this choice is that a genuinely hedged book, long
    EURUSD and long USDJPY, looks more exposed than it is. That is the right
    error to make on a R2,000 account.

    Args:
        open_positions: Positions currently live, as `OpenPosition` views. Each
            supplies its own pair, realised risk amount and the balance it was
            sized on, so no external state is needed. An empty sequence means a
            known-empty book and returns an empty mapping. This function has no
            way to express "not known", which is why the caller that can tell
            the difference, `check_limits`, takes that decision before calling.

    Returns:
        Mapping of ISO currency code to the summed risk fractions attributable
        to it, for every currency appearing in at least one open position.
        Currencies with no exposure are absent rather than present with 0.0,
        because a padded 0.0 reads as a measurement of a currency nothing is
        held in.

        Each term is a fraction of its own position's entry balance, so the
        total is a fraction of the current account only when every position was
        sized on it. See the paragraph above before comparing it against a cap.

    Raises:
        ValueError: If any position's ``account_balance_at_entry`` is not
            strictly positive and finite, or if its ``risk_amount`` is not a
            finite, non-negative figure. A zero balance has no risk fraction,
            and returning one anyway would put a fabricated number in front of
            the exposure cap. A negative ``risk_amount`` is the same failure
            wearing the other sign: it subtracts from a leg's total, so one
            corrupt row hides a real position behind a hedge that does not
            exist. Neither is reported as a warning, because this function
            returns a mapping with nowhere to carry one and the caller compares
            what it returns against a cap.
        ValueError: If a pair is not six characters, propagated from
            `fbe.universe.split_pair`, or if either leg is not in `G10`.
            `split_pair` checks length and nothing else, so ``"EURUSO"`` splits
            as cleanly as ``"EURUSD"`` and would attribute this position's risk
            to a currency that does not exist, leaving the real leg unmeasured
            and the cap understated by the whole position. One mistyped
            character in a hand-written journal row is the realistic producer,
            and `OpenPosition` is a structural protocol with nothing validating
            the pair string behind it.

    """
    exposure: dict[str, float] = {}
    for position in open_positions:
        balance = position.account_balance_at_entry
        if not isfinite(balance) or balance <= 0.0:
            raise ValueError(
                f"{position.pair} carries account_balance_at_entry "
                f"{balance!r}, which yields no risk fraction. A zero divides, "
                f"a negative flips the sign so the exposure compares below "
                f"every cap, and a NaN compares False against every cap it is "
                f"checked against."
            )
        risk = position.risk_amount
        if not isfinite(risk) or risk < 0.0:
            raise ValueError(
                f"{position.pair} carries risk_amount {risk!r}. Money at risk "
                f"is a non-negative figure in the account currency; a negative "
                f"one would subtract from a leg's exposure and hide a real "
                f"position behind a fabricated hedge."
            )
        base, quote = split_pair(position.pair.upper())
        for leg in (base, quote):
            if leg not in G10:
                raise ValueError(
                    f"{position.pair!r} has a leg {leg!r} that is not a G10 "
                    f"currency. `split_pair` checks length only, so a single "
                    f"mistyped character in a hand-written journal row splits "
                    f"cleanly and attributes this position's risk to a "
                    f"currency that does not exist, leaving the real leg "
                    f"unmeasured and the exposure cap understated."
                )
        fraction = risk / balance
        for leg in (base, quote):
            exposure[leg] = exposure.get(leg, 0.0) + fraction
    return exposure


def check_limits(
    open_positions: Sequence[OpenPosition] | None,
    proposed: PositionSize,
    config: RiskConfig,
    realised_pnl_today: float | None = None,
    equity_peak: float | None = None,
) -> LimitReport:
    """Report what each portfolio limit says about ``proposed``, including silence.

    Every limit in `LIMITS` comes back with one of three outcomes: clear,
    breached, or not performed because an input was missing. There is no
    all-clear that a run can obtain without having actually checked everything,
    which is the whole reason this returns a `LimitReport` rather than a list of
    refusals. An empty refusal list from a caller that supplied no open
    positions, no daily profit and loss and no equity peak used to mean "nothing
    was checked" and read as "everything passed", and a pass is what authorises
    a trade.

    A not-performed outcome does not block the ticket. It accompanies it. Two of
    these four limits have no automated source in this repository at all, so a
    refusal on absence would refuse every trade, and a gate that refuses
    everything gets switched off along with the checks that were working. What a
    not-performed outcome does do is deny `LimitReport.all_clear` and put a named
    line on the ticket, so the owner checks that limit by hand against the broker
    terminal before ticking the box in section 8 of
    ``docs/risk-and-execution.md``.

    Every limit is evaluated, so the caller sees all the breaches rather than
    the first. The boundaries differ by limit and are stated here because an
    off-by-one on a limit is not visible in the output:

        Concurrent positions: breached when ``len(open_positions) >=
            max_concurrent_positions``, since the question is whether there is
            room for one more. Three is not a capital constraint, it is an
            attention constraint. The plan runs on 1h and 4h charts managed by
            hand, and a fourth open ticket is where management degrades. Not
            performed when ``open_positions`` is ``None``.
        Correlated exposure: breached when adding ``proposed`` pushes any single
            currency's total STRICTLY above ``max_correlated_exposure``. Landing
            exactly on 4% is holding the configured maximum, not exceeding it.
            Compute `correlated_exposure` over ``open_positions`` plus
            `PositionRisk.from_position_size(proposed)` and compare each leg.
            The detail names the offending currency and the resulting figure,
            because "too much exposure" does not tell the owner which ticket to
            drop. Not performed when ``open_positions`` is ``None``: the
            proposed position's own legs are known, but a total that ignores the
            book is not an exposure figure, and reporting it as one would be the
            same defect in a smaller font.
        Daily loss: breached when ``realised_pnl_today <= -max_daily_loss *
            proposed.account_balance``, at or beyond, because the threshold is a
            stop for the session rather than a ceiling to sit on. R80.00 on the
            plan's R2,000 balance at the 4% default. Realised only: an open
            position sitting at a loss is not yet evidence of anything. Not
            performed when ``realised_pnl_today`` is ``None``.
        Drawdown pause: breached when ``proposed.account_balance <= equity_peak
            * (1 - max_drawdown_pause)``, again at or beyond. The direction to
            take is a review of the model, not a resize: a drawdown that deep on
            a fundamental bias engine is more likely to mean the weights are
            wrong than that variance was unkind. Not performed when
            ``equity_peak`` is ``None``, which is every run today, because
            nothing in this repository records a balance history.

    Which balance: the two money comparisons use ``proposed.account_balance``,
    the balance the proposed size was actually derived from, rather than
    ``config.account_balance``. A run that overrides the balance for one ticket
    would otherwise size against one number and check against another. The
    limits themselves, the counts and the fractions, come from ``config``.

    Args:
        open_positions: Positions already live, as `OpenPosition` views, which a
            `fbe.journal.TradeRecord` satisfies without conversion. ``None``
            means the open book is not known, for example because the journal
            could not be read, and is reported as not performed on the two
            position limits. An empty sequence means the book was read and is
            genuinely empty, which is a reading and clears both. The parameter
            has no default so that a caller has to say which of those two it
            holds.
        proposed: The position being considered. Money fields in the account
            currency, and the limits read ``realised_risk_amount``, not
            ``risk_amount``.
        config: Risk limits from the plan. Supplies every ceiling; no threshold
            is written into this function.
        realised_pnl_today: Closed profit and loss for the current session in
            the account currency (ZAR), negative for a loss. ``None`` means not
            tracked and produces a not-performed outcome. ``0.0`` is a reading,
            it means flat on the day, and it produces a performed check. The two
            used to be the same argument, which is the case ADR 0002 rule 1
            names.
        equity_peak: Highest account balance reached, in the account currency,
            positive. ``None`` means not tracked and produces a not-performed
            outcome rather than assuming the current balance is the peak, which
            would hide an existing drawdown.

    Returns:
        A `LimitReport` holding one `LimitCheck` per entry in `LIMITS`, in that
        order. `LimitReport.all_clear` is the only permission this function
        grants, and it is false if anything was breached or not performed.

    Raises:
        ValueError: If ``proposed.account_balance`` is not strictly positive, or
            if ``equity_peak`` is supplied and is not strictly positive. Both
            would make a percentage of the account meaningless, and a
            meaningless percentage compared against a limit is worse than no
            comparison because it produces an answer.

    """
    raise NotImplementedError(
        "fbe.risk.check_limits is scaffolded; see docs/roadmap.md Phase 4"
    )
