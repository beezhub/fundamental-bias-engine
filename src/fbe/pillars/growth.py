"""Growth pillar: relative activity across the cycle.

Weight 0.15. Growth reaches a currency through two channels: it pulls the policy
path higher, and it pulls foreign capital into the equity and direct investment
account. Both are slower than the rates channel, which is why this pillar is
weighted at half the monetary pillar despite being the thing most people mean
when they say a country is doing well.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["GrowthPillar"]


class GrowthPillar(BasePillar):
    """Score relative activity from GDP, surveys, output and consumption.

    Components and sub-weights:
        ``gdp_yoy`` (0.25): headline real GDP year on year, in percent.
        ``pmi_level`` (0.20): manufacturing PMI less 50, in index points, so the
            component is signed at the expansion boundary rather than at the
            cross-sectional mean.
        ``pmi_trend`` (0.10): three-month change in the manufacturing PMI.
        ``industrial_production_yoy`` (0.20): in percent.
        ``retail_sales_yoy`` (0.25): in percent. Consumption carries as much as
            GDP because it is monthly and GDP is not, so it is where a turn
            shows up first.

    Sign rule: positive means faster activity than the rest of the universe, and
    therefore a strong currency. No component is inverted.

    The PMI coverage gap. The manufacturing PMI is the most useful series in this
    pillar, being monthly, forward-looking and released within days of month end,
    and it is the one series here with no free and consistent G10-wide source.
    The US ISM index is public; the S&P Global national PMIs are licensed and
    published only in headline form. So ``pmi_manufacturing`` arrives through the
    manual drop at ``DataConfig.manual_dir`` for whichever currencies the trader
    has filled in, and is simply absent for the rest.

    The fallback is deliberate and slightly counter-intuitive: if the PMI is
    missing for any currency in the cross-section, both PMI components are
    dropped for every currency, not just for the ones that lack it. A z-score
    computed across four currencies sits on a different scale from one computed
    across eight, and blending the two would quietly rescale the pillar and hand
    an advantage to whichever currencies happened to be in the smaller sample.
    The remaining three components renormalise over 0.70 of the sub-weight, which
    clears `MIN_COMPONENT_WEIGHT`, so the pillar keeps running on GDP, industrial
    production and retail sales. Filling the manual PMI file improves the pillar;
    leaving it empty does not break it.

    Known failure modes: GDP is quarterly and lands one to two months after the
    quarter closes, so in the worst case this component describes activity that
    ended five months ago and the pillar is the last one in the engine to notice
    a turn. Industrial production and retail sales are monthly but noisy and
    heavily revised, so a single print can move the cross-sectional ranking and
    then be revised away. And the year-on-year framing lags by construction: a
    recovery that began three months ago still shows a negative year-on-year
    rate, which is why ``pmi_trend`` is in the mix at all.
    """

    name = PillarName.GROWTH
    requires: Sequence[str] = (
        "gdp_yoy",
        "industrial_production_yoy",
        "pmi_manufacturing",
        "retail_sales_yoy",
    )
    headline_component = "gdp_yoy"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, with 0.30 in the two PMI
            components that may be dropped wholesale.

        """
        return {
            "gdp_yoy": 0.25,
            "pmi_level": 0.20,
            "pmi_trend": 0.10,
            "industrial_production_yoy": 0.20,
            "retail_sales_yoy": 0.25,
        }

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull the four activity series per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}``, sorted by period
            ascending. ``pmi_manufacturing`` is expected to be absent for most
            currencies and its absence is not an error.

        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Build the growth components, dropping PMI if coverage is partial.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}``. ``pmi_level`` and ``pmi_trend``
            are set to ``None`` for every currency when any currency in the
            cross-section lacks a PMI observation inside
            ``ScoringConfig.max_staleness_days``, so the drop is all or nothing.

        The three year-on-year components are taken as published; they are
        already in comparable percent units so they need no per-currency
        rebasing before the cross-sectional z-score.

        """
        raise NotImplementedError
