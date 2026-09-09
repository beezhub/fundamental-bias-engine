"""External balance pillar: who has to buy the currency, and who has to sell it.

Weight 0.10. The external accounts are the slowest signal in the engine and the
one least likely to move a pair this week, but they set the direction a currency
drifts in when the rates story is flat, and they are what makes a commodity
currency a commodity currency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["ExternalPillar"]


class ExternalPillar(BasePillar):
    """Score the current account, the trade trend, and terms of trade.

    Components and sub-weights:
        ``current_account`` (0.40): the latest current account balance,
            z-scored against that currency's own history over
            ``ScoringConfig.lookback_years``.
        ``trade_trend`` (0.30): the trailing twelve-month sum of
            ``trade_balance``, also z-scored against its own history. The
            twelve-month sum is used rather than the monthly print because trade
            data is violently seasonal and the sum removes the seasonality and
            the monthly noise in one step.
        ``terms_of_trade`` (0.30): the three-month percent change in the
            commodity index tied to ``CurrencyMeta.commodity_link``.

    Why the first two are normalised against their own history first. These
    series are published in domestic units and on wildly different scales:
    billions of yen, billions of euro, and in some vintages a percentage of GDP.
    Ranking them raw across the cross-section would rank the size of the
    economies. Z-scoring each against its own five-year record converts them
    into "wide or narrow for this country", and those are comparable. The
    resulting time-series z-scores are then z-scored cross-sectionally by the
    shared normaliser, which is the same two-step the employment pillar uses on
    ``employment_change``.

    Terms of trade and the five currencies without a commodity link. Only CAD,
    AUD and NZD carry a ``commodity_link``, respectively crude oil, iron ore and
    dairy. For USD, EUR, GBP, JPY and CHF the component is entered as ``0.0``,
    not as ``None``. That is a modelling statement rather than a convenience: a
    zero says their export basket is diversified enough that no single commodity
    complex drives their terms of trade, so the commodity impulse on them this
    quarter is neutral. Marking it missing instead would say something different
    and worse, that the component could not be measured, and would trigger the
    renormalisation machinery for five of the eight currencies every single run.

    The cost of that choice, stated so it can be reconciled: with five of eight
    values pinned at zero, the cross-sectional standard deviation of this
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
        "trade_balance",
        "current_account",
        "commodity_index",
    )
    headline_component = "current_account"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0.

        """
        return {
            "current_account": 0.40,
            "trade_trend": 0.30,
            "terms_of_trade": 0.30,
        }

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull the balances per currency and the commodity index per link.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}``. ``commodity_index``
            observations are keyed by commodity complex in
            ``Observation.meta``, not by currency, so the extractor routes each
            index to the currencies whose ``commodity_link`` names it. A
            currency with no link gets no ``commodity_index`` entry.

        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Build the two normalised balances and the terms-of-trade change.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}``. ``current_account`` and
            ``trade_trend`` are unitless time-series z-scores.
            ``terms_of_trade`` is a percent change, and is ``0.0`` rather than
            ``None`` for currencies whose ``commodity_link`` is ``None``.

        A currency missing both balances holds only 0.30 of the sub-weight,
        which is under `MIN_COMPONENT_WEIGHT`, so it is scored as missing rather
        than on a commodity move alone.

        """
        raise NotImplementedError
