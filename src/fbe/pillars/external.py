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
    )
    headline_component = "current_account_gdp"

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
            ``{currency: {indicator: observations}}``. ``commodity_price``
            observations are keyed by commodity complex in
            ``Observation.meta``, not by currency, so the extractor routes each
            series to the currencies whose ``commodity_link`` names it. A
            currency with no link gets no ``commodity_price`` entry.

        """
        raise NotImplementedError(
            "fbe.pillars.external.ExternalPillar._extract is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )

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

        """
        raise NotImplementedError(
            "fbe.pillars.external.ExternalPillar._transform is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )
