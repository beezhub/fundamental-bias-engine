"""External balance pillar: who has to buy the currency, and who has to sell it.

Weight 0.10. The external accounts are the slowest signal in the engine and the
one least likely to move a pair this week, but they set the direction a currency
drifts in when the rates story is flat, and they are what makes a commodity
currency a commodity currency.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName
from fbe.universe import meta

__all__ = ["ExternalPillar"]


class ExternalPillar(BasePillar):
    """Score the current account, the trade trend, and terms of trade.

    Components and sub-weights:
        ``current_account_gdp`` (0.40): the current account balance as a percent
            of GDP, taken as a level. Positive is a surplus.
        ``trade_trend`` (0.30): the three-month change in the trade balance, also
            as a percent of GDP. The change rather than the level, because the
            level is largely structural and already known, while the direction of
            travel is what reprices.
        ``terms_of_trade`` (0.30): the three-month return on the commodity tied
            to ``CurrencyMeta.commodity_link``, in percent.

    Why everything is scaled by GDP rather than normalised against its own
    history. These balances are published in domestic units and on wildly
    different scales: billions of yen, billions of euro, billions of dollars.
    Ranking them raw across the cross-section would rank the size of the
    economies. Expressing them as a share of GDP puts all eight on one scale
    directly, which is both comparable and readable: a current account of -3.3%
    of GDP means something to a person in a way a time-series z-score of -1.2
    does not. It also keeps the pillar to a single normalisation step, so the
    cross-sectional z is measuring what it appears to measure.

    Terms of trade and the five currencies without a commodity link. Only CAD,
    AUD and NZD carry a ``commodity_link``, respectively crude oil, iron ore and
    dairy. For USD, EUR, GBP, JPY and CHF the component is entered as ``0.0``,
    not as ``None``. That is a modelling statement rather than a convenience: a
    zero says their export basket is diversified enough that no single commodity
    complex drives their terms of trade, so the commodity impulse on them this
    quarter is genuinely neutral. Marking it missing instead would say something
    different and worse, that the component could not be measured, and it would
    leave the z-score to be computed across three currencies, which on three
    points is not a z-score.

    The cost of that choice, stated so it can be read correctly: with five of
    eight values pinned at zero, the cross-sectional standard deviation of this
    component is compressed, and the three commodity currencies land further from
    the mean than the underlying commodity move alone would justify. The effect
    is to amplify the terms-of-trade signal for CAD, AUD and NZD relative to the
    other components, which is defensible because terms of trade genuinely is a
    first-order FX driver for those three and is not for the other five.

    Sign rule: positive means a stronger external position, a widening surplus or
    a narrowing deficit, or an improving commodity price for a linked currency.
    All three components are already oriented that way.

    Known failure modes: the current account is quarterly and lands a quarter
    late, so it is the slowest number in the engine and its cross-sectional rank
    barely changes from run to run, contributing standing rather than news. The
    sign is also horizon-dependent in a way this pillar cannot see: a surplus is
    currency-positive over years, but a deficit financed by heavy portfolio
    inflows is currency-positive over weeks, and the engine only observes the
    balance, never the financing. And a single index is a thin proxy for a whole
    export basket. Iron ore is not all of Australia's terms of trade, and dairy
    is a narrow read on New Zealand's.
    """

    name = PillarName.EXTERNAL
    requires: Sequence[str] = (
        "current_account_gdp",
        "trade_balance",
        "commodity_price",
        "gdp_nominal_usd",
    )
    headline_component = "current_account_gdp"

    component_indicators: Mapping[str, tuple[str, ...]] = {
        "current_account_gdp": ("current_account_gdp",),
        "trade_trend": ("trade_balance", "gdp_nominal_usd"),
        "terms_of_trade": ("commodity_price",),
    }
    """The current account is quarterly across the whole G10, which made it the
    second series after quarterly CPI to take zero weight on every run for every
    currency under a 45-day ramp.

    ``trade_trend`` names both of its inputs, so `component_freshness` ages it on
    the stalest, which is the annual denominator by years. That is arguable and
    the argument is recorded rather than acted on: a nominal GDP is a scaling
    constant rather than news, and the relative size of Germany and New Zealand
    does not move enough in eighteen months to distort a cross-section. Declaring
    some inputs as scaling rather than signal would be a mechanism built for one
    case, so issue #126 carries it and this table states the cost instead.
    """

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, matching section 3.5 of
            ``docs/scoring-spec.md``.

        """
        return {
            "current_account_gdp": 0.40,
            "trade_trend": 0.30,
            "terms_of_trade": 0.30,
        }

    # `_extract` is `BasePillar`'s, deliberately not overridden, and the stub
    # that stood here described something the data layer does not do. It said
    # `commodity_price` observations are keyed by commodity complex in
    # `Observation.meta`. Nothing populates `meta` except `ManualSource`, and
    # `BaseDataSource.observation` does not set it at all, so routing on it
    # would find nothing and take terms of trade away from exactly the three
    # currencies the component exists for. The registry keys that indicator by
    # currency and says so in its own description, which is the routing the
    # base loop already performs. `commodity_link` is read in `_transform`
    # instead, to decide which five currencies take the deliberate `0.0`.
    # Issue #158 carries the ruling.

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Build the two GDP-scaled balances and the terms-of-trade return.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}``. ``current_account_gdp`` is a
            level in percent of GDP and ``trade_trend`` a three-month change in
            percent of GDP. ``terms_of_trade`` is a three-month percent return,
            and is ``0.0`` rather than ``None`` for currencies whose
            ``commodity_link`` is ``None``.

        A currency missing both balances holds only 0.30 of the sub-weight,
        which is under `MIN_COMPONENT_WEIGHT`, so it is scored as missing rather
        than on a commodity move alone. Since terms of trade is never missing,
        that is the only route to an absent external pillar.

        Raises:
            ValueError: A currency's nominal GDP is zero or negative. That is a
                corrupt registry entry or a corrupt source rather than a
                currency with no data, and the two must not answer the same
                way. `scoring.score_currencies` turns this into every currency
                unscored with the reason attached, which is loud, rather than
                one quietly absent component.

        Both three-month quantities measure three calendar months, not three
        observations. `BasePillar.momentum` counts observations and warns in its
        own docstring that a horizon is series-native, which is right for a
        series on a fixed release grid and wrong here: crude oil is daily, so
        three observations of it is three days. See `_window`.

        The scale is load-bearing and nothing here corrects it. ``trade_balance``
        and ``gdp_nominal_usd`` are both in actual US dollars, which is what the
        ruling on #158 chose the World Bank annual series for, so the ratio needs
        no conversion. A denominator filed in millions would make every
        currency's ``trade_trend`` a million times smaller by the same factor,
        the cross-sectional z-score would absorb it exactly, and the ranking
        would be unchanged with every published number wrong.

        """
        built: dict[str, dict[str, float | None]] = {}
        for currency, series in extracted.items():
            # Indexed rather than fetched with a default, so a broken `_extract`
            # contract raises here instead of becoming a quietly unscored
            # currency.
            built[currency] = {
                "current_account_gdp": _newest(series["current_account_gdp"]),
                "trade_trend": _trade_trend(
                    series["trade_balance"], series["gdp_nominal_usd"], currency
                ),
                "terms_of_trade": _terms_of_trade(
                    series["commodity_price"], meta(currency).commodity_link
                ),
            }
        return built


WINDOW_MONTHS: int = 3
"""Horizon of both change components, in calendar months.

Section 3.5 of ``docs/scoring-spec.md`` specifies a three-month change for the
trade balance and a three-month return on the commodity. Three months rather
than three observations, because the three series feeding these components
publish monthly, daily and irregularly.
"""


def _newest(found: Sequence[Observation]) -> float | None:
    """Return the newest visible reading, in the unit the source published it.

    Args:
        found: One currency's visible observations of one series, oldest period
            first, as `_extract` leaves them.

    Returns:
        The newest value with nothing applied, or ``None`` when the currency has
        no visible print. Never ``0.0`` for an absence: a current account of
        exactly zero is a balanced economy, which is a finding, and an outage
        filed under the same value would put that currency in the middle of the
        cross-section on the strength of having no data.

    The last element is the newest because `_extract` sorts by period ascending
    and keeps one observation per period.

    """
    if not found:
        return None
    return found[-1].value


def _months_before(when: date, months: int) -> date:
    """Return the same day-of-month ``months`` earlier, clamped to month end."""
    total = (when.year * 12 + when.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(when.day, monthrange(year, month)[1])
    return date(year, month, day)


def _window(found: Sequence[Observation]) -> tuple[float, float] | None:
    """Return the readings at each end of the three-month window.

    Args:
        found: One currency's visible observations of one series, oldest period
            first.

    Returns:
        ``(baseline, newest)`` in the series' own unit, or ``None`` when the
        series has no reading old enough to open the window. Never a zero
        change: one observation is not a change of nothing, it is not a change.

    The baseline is the newest observation at or before three calendar months
    back, so the window is three months by construction. Where the series has a
    hole the baseline is older than that, and the tolerance is one further
    horizon: a baseline more than six months back is refused rather than
    returned, because a nine-month return reported as a three-month one is the
    wrong number in the right unit and nothing downstream could tell.

    Calendar months rather than a count of observations. The three series that
    reach this function publish monthly, daily and irregularly, so counting
    observations would measure three days of crude oil against three months of
    trade.

    """
    if len(found) < 2:
        return None
    newest = found[-1]
    target = _months_before(newest.period, WINDOW_MONTHS)
    oldest_allowed = _months_before(newest.period, 2 * WINDOW_MONTHS)
    baseline = None
    for observation in found[:-1]:
        if oldest_allowed <= observation.period <= target:
            baseline = observation
    if baseline is None:
        return None
    return baseline.value, newest.value


def _trade_trend(
    balances: Sequence[Observation],
    gdp: Sequence[Observation],
    currency: str,
) -> float | None:
    """Return the three-month change in the trade balance, in percent of GDP.

    Args:
        balances: One currency's visible ``trade_balance`` observations, in
            actual US dollars, oldest period first.
        gdp: The same currency's visible ``gdp_nominal_usd`` observations, in
            actual US dollars.
        currency: ISO code, used only to name the currency in the error.

    Returns:
        ``(newest - baseline) / nominal GDP * 100``, in percent of annual GDP,
        positive when the balance is improving. ``None`` when either series is
        absent or the balance has no reading old enough to open the window, and
        never ``0.0`` for any of those: a zero change is a balance that has not
        moved, which is a reading.

    Raises:
        ValueError: The newest nominal GDP is zero or negative, which is a
            corrupt input rather than a missing one.

    Positive is currency-positive. A narrowing deficit and a widening surplus
    are the same fact in this component, which is why it is the change and not
    the level: the level is largely structural and already priced.

    The numerator is a monthly flow and the denominator an annual level, so the
    figure is smaller than a reader used to annual current-account shares might
    expect. It is not annualised, because multiplying by twelve would apply one
    constant to all eight currencies, change no ranking, and make the number
    read as an annual rate it is not. Each component is z-scored across the
    universe before the blend, so what has to be comparable is this quantity
    across currencies, which it is.

    """
    window = _window(balances)
    level = _newest(gdp)
    if window is None or level is None:
        return None
    if not level > 0.0:
        # Written as a negated `>` rather than `<= 0.0` so that a NaN is caught.
        # Every comparison with NaN is False, so `level <= 0.0` would pass one
        # through and the currency would score NaN rather than raising.
        raise ValueError(
            f"{currency} reports a nominal GDP of {level}, which cannot scale a "
            "trade balance. A non-positive or non-finite level is a corrupt "
            "registry entry or a corrupt source, not a currency with no data."
        )
    baseline, newest = window
    return (newest - baseline) / level * 100.0


def _terms_of_trade(
    prices: Sequence[Observation], commodity_link: str | None
) -> float | None:
    """Return the three-month percent return on the linked commodity.

    Args:
        prices: One currency's visible ``commodity_price`` observations, oldest
            period first. Empty for a currency with no linked series.
        commodity_link: That currency's `CurrencyMeta.commodity_link`, or
            ``None`` where no single commodity complex drives its terms of
            trade.

    Returns:
        ``(newest / baseline - 1) * 100``, positive when the export price is
        rising, which is currency-positive: a higher export price raises
        national income and, through it, the currency.

        ``0.0`` for a currency whose ``commodity_link`` is ``None``. That is the
        one deliberate zero in the engine and it is a modelling statement: USD,
        EUR, GBP, JPY and CHF export a basket diversified enough that no single
        complex drives their terms of trade, so the commodity impulse on them is
        genuinely neutral rather than unmeasured. Marking them absent instead
        would leave the z-score to be computed across three currencies, which on
        three points is not a z-score.

        ``None`` for a currency that has a link and no usable price. That is a
        different fact from the zero above and the two must not be conflated: a
        zero for CAD would be a confident claim that crude did not move, made on
        a feed outage. ``None`` also where the baseline is zero or negative,
        which no traded price is.

    """
    if commodity_link is None:
        return 0.0
    window = _window(prices)
    if window is None:
        return None
    baseline, newest = window
    if not baseline > 0.0:
        # Negated `>` for the same reason as `_trade_trend`'s denominator: a NaN
        # baseline would pass `<= 0.0` and return a NaN return.
        return None
    return (newest / baseline - 1.0) * 100.0
