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
        ``gdp_yoy`` (0.30): headline real GDP year on year, in percent. The
            lagging but comprehensive measure.
        ``pmi_composite`` (0.30): the composite PMI level, in index points. The
            leading survey, and the only forward-looking series in the pillar.
        ``indpro_yoy`` (0.20): industrial production year on year, in percent.
        ``retail_sales_yoy`` (0.20): in percent.

    The mix is one lagging and comprehensive measure, one leading survey, and two
    coincident hard-data series. The composite PMI is used rather than the
    manufacturing series alone because manufacturing is a small and shrinking
    share of most G10 economies, and a composite that includes services is a
    better read on the activity that actually sets the policy path.

    Sign rule: positive means faster activity than the rest of the universe, and
    therefore a strong currency. No component is inverted.

    The PMI coverage gap. The composite PMI is the most useful series in this
    pillar, being monthly, forward-looking and released within days of month end,
    and it is the one series here with no free and consistent G10-wide source.
    The US ISM indices are public; the S&P Global national PMIs are licensed and
    published only in headline form. So ``pmi_composite`` arrives through the
    manual drop at ``DataConfig.manual_dir`` for whichever currencies the trader
    has filled in, and is simply absent for the rest.

    The fallback, per section 3.3 of ``docs/scoring-spec.md``: where the PMI is
    missing for a currency, that currency's sub-weights renormalise across the
    remaining three, which hold 0.70 between them and clear
    `MIN_COMPONENT_WEIGHT`. Coverage is unaffected, because the pillar still has
    data. The engine must not substitute a proxy silently. Filling the manual PMI
    file improves the pillar; leaving it empty does not break it.

    One caveat to hold in mind when reading a run with partial PMI coverage. The
    PMI z-score is computed across whichever currencies have the series, so a
    cross-section of four sits on a different scale from one of eight, and the
    currencies inside the smaller sample are being ranked against a different
    yardstick from the ones outside it. The effect is second-order at a 0.30
    sub-weight inside a 0.15 pillar, and the alternative, dropping the component
    for everyone, throws away the best series in the pillar whenever one country
    is missing. Filling the manual file for all eight removes the question.

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
        "pmi_composite",
        "indpro_yoy",
        "retail_sales_yoy",
    )
    headline_component = "gdp_yoy"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, matching section 3.3 of
            ``docs/scoring-spec.md``.

        """
        return {
            "gdp_yoy": 0.30,
            "pmi_composite": 0.30,
            "indpro_yoy": 0.20,
            "retail_sales_yoy": 0.20,
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
            ascending. ``pmi_composite`` may be absent for some currencies and
            its absence is not an error.

        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Take the newest print of each activity series.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}``. ``pmi_composite`` is the index
            level; the other three are year-on-year percentages. A currency with
            no PMI inside ``ScoringConfig.max_staleness_days`` returns ``None``
            for that component only and is renormalised over the remaining 0.70.

        All four components are levels, taken as published. The three
        year-on-year series are already in comparable percent units and the PMI
        is a diffusion index on a common scale, so none of them needs
        per-currency rebasing before the cross-sectional z-score.

        """
        raise NotImplementedError
