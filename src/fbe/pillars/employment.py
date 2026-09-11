"""Employment pillar: labour market direction, not labour market level.

Weight 0.10. Employment matters to FX mainly as an input to the policy
reaction function, which is why it is weighted below growth and well below the
rates pillar it ultimately feeds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["EmploymentPillar"]


class EmploymentPillar(BasePillar):
    """Score the direction of unemployment and the momentum of hiring.

    Components and sub-weights:
        ``unemployment_6m`` (0.50): the six-month change in the unemployment
            rate in percentage points, multiplied by ``-1``. Six months rather
            than one because the unemployment rate is a slow, heavily smoothed
            series and a single month of change is mostly survey noise.
        ``employment_trend`` (0.50): the three-month average change in
            ``employment_chg``, expressed as an annualised percent of the
            employment level.

    Why no level component. Unemployment levels across the G10 measure labour
    market institutions rather than the cycle. Japan sits near 2.5% and the euro
    area near 6.5% in good years and bad, so ranking the eight on the level
    ranks their hiring and firing law, which does not trade. The cycle lives
    entirely in the change, so only the change is scored.

    Why hiring is expressed as an annualised percent. The underlying series is
    published in raw counts and the counts are not comparable: a US payrolls
    print is in the hundreds of thousands and a New Zealand quarterly employment
    change is in the thousands. Dividing by the employment level and annualising
    turns both into "the labour force grew at this rate", which is directly
    comparable across the cross-section and needs no per-currency rebasing before
    the z-score. Doing it in the units rather than through a second normalisation
    step also keeps the number readable in a report: 1.3% annualised hiring means
    something to a person, and a time-series z-score of 1.3 does not.

    Sign rule: positive means the labour market is tightening relative to the
    others, which is currency-positive through the expected policy path. The
    inversion on ``unemployment_6m`` is the only sign flip in the pillar: a
    falling unemployment rate is a rising score.

    Known failure modes: employment lags the cycle. By the time the unemployment
    rate has turned, the rate market has usually finished repricing, so the
    pillar tends to confirm what the monetary pillar already said rather than
    adding information, which is why it carries only 0.10.

    Then the participation trap. Unemployment can fall because people stop
    looking for work, which this pillar reads as strength when it is the
    opposite. ``employment_trend`` is the partial guard, since a
    participation-driven fall shows no hiring behind it, and the two components
    disagreeing is itself informative: it lands as elevated ``dispersion`` on the
    `CurrencyScore` and cuts conviction downstream.

    Last, frequency. Australia and New Zealand publish employment quarterly while
    the US publishes monthly, so a fixed number of periods means different spans
    of time for different currencies. The pillar states its horizons in months
    and the extractor is responsible for resampling to a common monthly grid
    before differencing.
    """

    name = PillarName.EMPLOYMENT
    requires: Sequence[str] = ("unemployment_rate", "employment_chg")
    headline_component = "unemployment_chg_6m"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, matching section 3.4 of
            ``docs/scoring-spec.md``. The two components are weighted equally
            because neither guards against the other's failure mode on its own.

        """
        return {"unemployment_6m": 0.50, "employment_trend": 0.50}

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull the unemployment rate and the hiring series per currency.

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
        raise NotImplementedError(
            "fbe.pillars.employment.EmploymentPillar._extract is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )

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
            ``{currency: {component: value}}`` over three keys, two of which
            carry sub-weight:

                ``unemployment_6m``: the six-month change in percentage points
                with the sign already flipped, so a positive value means
                unemployment fell. Weighted.

                ``employment_trend``: an annualised percent of the employment
                level. Weighted.

                ``unemployment_chg_6m``: the same six-month change in percentage
                points with its natural published sign, so a positive value means
                unemployment rose. Report-only, no sub-weight, and it exists
                solely to fill `headline_component`.

        The sign flip on unemployment is the single flip in this pillar and is
        applied here, once, never again downstream.

        Why the unflipped copy exists. ``PillarScore.raw`` is contracted to be
        the pillar's headline number in its natural unit, and a report shows it
        so a reader can check the reasoning. Putting the flipped value there
        prints ``+0.3`` beside a currency whose unemployment rate rose 0.3 points,
        which reads as good news about a bad number. The flip is a modelling
        step and belongs to the score; ``raw`` should show what the statistics
        office published. This is the same device the positioning and risk
        pillars use to report a readable quantity alongside a derived one.

        With the components weighted equally at 0.50, either one missing leaves
        exactly `MIN_COMPONENT_WEIGHT` present, and the floor is "at or below",
        so a currency with only one of the two series is scored as missing rather
        than on that series alone. There is therefore no partial state for this
        pillar: a currency has both components or it has none of the pillar, and
        the shortfall reaches the reader as reduced coverage on the
        `CurrencyScore` instead of as a full-weight score built on half the
        evidence. That is the point of the equal weighting. Neither component
        guards against the other's failure mode, so half of this pillar is not
        better than none of it.

        """
        raise NotImplementedError(
            "fbe.pillars.employment.EmploymentPillar._transform is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )
