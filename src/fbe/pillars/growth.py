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
        ``business_confidence_mfg`` (0.30): OECD business confidence for
            manufacturing, as a percentage balance with zero as neutral. The
            leading survey, and the only forward-looking series in the pillar.
        ``indpro_yoy`` (0.20): industrial production year on year, in percent.
        ``retail_sales_yoy`` (0.20): in percent.

    The mix is one lagging and comprehensive measure, one leading survey, and two
    coincident hard-data series.

    Sign rule: positive means faster activity than the rest of the universe, and
    therefore a strong currency. No component is inverted.

    Why the leading slot holds business confidence rather than a PMI. The slot is
    defined by role, and both candidates fill that role, so the question is which
    one is actually present. ``pmi_composite`` is licensed: the US ISM indices are
    public, but the S&P Global national PMIs are published only in headline form,
    so the key had no free G10 source and arrived through the manual drop at
    ``DataConfig.manual_dir`` only in months an operator keyed eight numbers in by
    hand. In practice that was no month, the file holds no values, and the slot
    was empty for every currency on almost every run. Issue #23 ruled the
    substitution and ADR 0005 records it. ``pmi_composite`` stays registered and
    is listed in `fbe.datasources.registry.UNCONSUMED_INDICATORS`, so re-adopting
    it if it is ever licensed is a one-line change.

    The two are never interchangeable as raw values. A percentage balance is
    neutral at zero and a diffusion index at 50, so a balance scored as an index
    would shift every currency the same way, the cross-sectional z-score would
    absorb the offset, and the ranking would still look orderly with nothing
    raising. That is why they are separate keys, and why the substitution is safe
    only at this layer: each component is z-scored across the universe before the
    blend, so the slot carries a rank rather than a unit.

    The cadence split, which is this component's real cost. USD, EUR, GBP and CHF
    survey monthly; JPY, CAD, AUD and NZD survey quarterly, following the Tankan
    and the Australian, Canadian and New Zealand business outlooks. Both halves
    stay present in the cross-section, because the indicator carries its own
    270-day allowance from the registry rather than the global 45, but they do not
    enter at the same weight: at its freshest a monthly leg enters at the declared
    0.30 and a quarterly leg at an effective 0.192. So GROWTH is more
    hard-data-weighted for those four currencies than for the other four, on every
    run. That is a permanent cross-sectional inconsistency, accepted because the
    alternative was a slot holding nothing for all eight. The discount is not a
    property of this series: ``s0`` derives from a global 15/45 ratio calibrated on
    monthly data, so any punctual quarterly print starts on the declining part of
    the ramp. Issue #126 carries that, and fixing it raises the quarterly half to
    0.30 with no further decision here.

    When a component is missing for a currency, that currency's sub-weights
    renormalise across the remaining three, which hold 0.70 between them and clear
    `MIN_COMPONENT_WEIGHT`. Coverage is unaffected, because the pillar still has
    data. The engine must not substitute a proxy silently.

    A second missing component does break it. The floor is "at or below", so a
    currency holding one 0.30 component and one 0.20 component sits at exactly
    0.50 and the pillar is absent for it. That was live before the substitution:
    ``indpro_yoy`` is manual-only for CHF, AUD and NZD, and with the manual PMI
    file empty those three held GDP plus retail sales alone and lost GROWTH
    entirely. ``business_confidence_mfg`` is verified 8 of 8, so they now clear the
    floor. The combination is still reachable and the reasoning still stands: a
    score built from GDP and retail sales alone would otherwise be presented with
    the same confidence as one built from four series, and nothing in the report
    would say which the reader was holding.

    Known failure modes: GDP is quarterly and lands one to two months after the
    quarter closes, so in the worst case this component describes activity that
    ended five months ago and the pillar is the last one in the engine to notice
    a turn. Industrial production and retail sales are monthly but noisy and
    heavily revised, so a single print can move the cross-sectional ranking and
    then be revised away. And the year-on-year framing lags by construction: a
    recovery that began three months ago still shows a negative year-on-year
    rate, which is why the survey is in the mix at all and why it carries as much
    weight as GDP.

    Two more, from section 3.3 of ``docs/scoring-spec.md``. The staleness
    discount handles the age of a GDP print but not its revision risk, which is a
    separate and unmeasured exposure. And strong growth is currency-positive
    through the policy channel and currency-negative through the import channel,
    since a fast-growing economy sucks in imports and widens its trade deficit.
    This pillar models only the first, and the external pillar is where the
    second would show up, several months later.
    """

    name = PillarName.GROWTH
    requires: Sequence[str] = (
        "gdp_yoy",
        "business_confidence_mfg",
        "indpro_yoy",
        "retail_sales_yoy",
    )
    headline_component = "gdp_yoy"

    component_indicators: Mapping[str, tuple[str, ...]] = {
        "gdp_yoy": ("gdp_yoy",),
        "business_confidence_mfg": ("business_confidence_mfg",),
        "indpro_yoy": ("indpro_yoy",),
        "retail_sales_yoy": ("retail_sales_yoy",),
    }
    """Four components, four separate clocks.

    This is the pillar the component-level discount was built for. GDP is
    quarterly and lands one to two months after the quarter closes while retail
    sales is monthly, so a single pillar-level age reports whichever happened to
    update last and carries the other at full weight.
    """

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, matching section 3.3 of
            ``docs/scoring-spec.md``.

        """
        return {
            "gdp_yoy": 0.30,
            "business_confidence_mfg": 0.30,
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
            ascending. Any of the four may be absent for a currency and an
            absence is not an error; the blend renormalises over what is present
            and the pillar goes absent below `MIN_COMPONENT_WEIGHT`.

        """
        raise NotImplementedError(
            "fbe.pillars.growth.GrowthPillar._extract is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )

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
            ``{currency: {component: value}}``. ``business_confidence_mfg`` is a
            percentage balance, neutral at zero and routinely negative in a
            healthy economy; the other three are year-on-year percentages. A
            currency whose newest print for a component is older than that
            indicator's own allowance returns ``None`` for that component only
            and is renormalised over the rest.

        The allowance comes from the registry, per
        `fbe.pillars.base.staleness_allowance`, not from
        ``ScoringConfig.max_staleness_days``. That matters most here: this
        pillar's survey is quarterly for half the universe and carries 270 days,
        so the global 45 would expire four currencies' legs on the day they
        published.

        All four components are levels, taken as published. The three
        year-on-year series are in comparable percent units and the survey
        balance is on a common scale across countries, so none of them needs
        per-currency rebasing before the cross-sectional z-score. The balance is
        not comparable with a diffusion index as a raw value, and nothing here
        mixes the two; see `fbe.datasources.registry.BUSINESS_CONFIDENCE_MFG`.

        """
        raise NotImplementedError(
            "fbe.pillars.growth.GrowthPillar._transform is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )
