"""Employment pillar: labour market direction, not labour market level.

Weight 0.10. Employment matters to FX mainly as an input to the policy
reaction function, which is why it is weighted below growth and well below the
rates pillar it ultimately feeds.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["EmploymentPillar"]


class EmploymentPillar(BasePillar):
    """Score the direction of unemployment and the momentum of hiring.

    Components and sub-weights:
        ``unemployment_6m`` (0.55): the six-month change in the unemployment
            rate in percentage points, multiplied by ``-1``. Six months rather
            than one because the unemployment rate is a slow, heavily smoothed
            series and a single month of change is mostly survey noise.
        ``employment_trend`` (0.45): the three-month average of
            ``employment_change``, z-scored against that currency's own history
            over ``ScoringConfig.lookback_years`` before it reaches the
            cross-section.

    Why no level component. Unemployment levels across the G10 measure labour
    market institutions rather than the cycle. Japan sits near 2.5% and the euro
    area near 6.5% in good years and bad, so ranking the eight on the level
    ranks their hiring and firing law, which does not trade. The cycle lives
    entirely in the change, so only the change is scored.

    Why ``employment_change`` is normalised against its own history first. The
    series is published in raw counts and the counts are not comparable: a US
    payrolls print is in the hundreds of thousands and a New Zealand quarterly
    employment change is in the thousands. Z-scoring each currency against its
    own record converts every one of them into "strong or weak for this country",
    which is the quantity that can then be ranked across countries. This is the
    same two-step used by the external pillar.

    Sign rule: positive means the labour market is tightening relative to the
    others, which is currency-positive through the expected policy path. The
    inversion on ``unemployment_6m`` is the only sign flip in the pillar: a
    falling unemployment rate is a rising score.

    Known failure modes: the participation trap. Unemployment can fall because
    people stop looking for work, which this pillar reads as strength when it is
    the opposite. ``employment_trend`` is the partial guard, since a
    participation-driven fall shows no hiring behind it, and the two components
    disagreeing is itself informative: it lands as elevated ``dispersion`` on the
    `CurrencyScore` and cuts conviction downstream. The second failure mode is
    frequency. Australia and New Zealand publish employment quarterly while the
    US publishes monthly, so ``periods=3`` means three months for one currency
    and nine for another. The pillar states the horizon in months and the
    extractor is responsible for resampling to a common monthly grid before
    differencing.
    """

    name = PillarName.EMPLOYMENT
    requires: Sequence[str] = ("unemployment_rate", "employment_change")
    headline_component = "unemployment_6m"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0.
        """
        return {"unemployment_6m": 0.55, "employment_trend": 0.45}

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull the unemployment rate and employment change per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}`` resampled to a monthly
            grid, with quarterly publishers carried forward within the quarter
            so that a fixed number of periods means a fixed number of months for
            every currency.
        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Build the inverted unemployment change and the hiring trend.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}``. ``unemployment_6m`` is in
            percentage points with the sign already flipped, so a positive value
            means unemployment fell. ``employment_trend`` is unitless, being a
            time-series z-score.

        Either component missing leaves the other below `MIN_COMPONENT_WEIGHT`
        for ``employment_trend`` alone, so a currency without an unemployment
        series is scored as missing rather than on hiring alone.
        """
        raise NotImplementedError
