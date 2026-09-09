"""Inflation pillar: distance from target, in headline and core terms.

Weight 0.15. The pillar never scores the level of inflation. A 3.0% print is 1.0
point above target in the United States, the euro area, the United Kingdom,
Japan, Canada and New Zealand; 0.5 points above target in Australia, whose target
is the midpoint of a 2-3% band; and 2.0 points above target in Switzerland, where
the Swiss National Bank aims below 2%. Those are three different policy problems
and therefore three different currency implications. What travels across a
cross-section is the gap between inflation and the number that currency's central
bank has committed to, which is what `CurrencyMeta.inflation_target` holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["InflationPillar"]


class InflationPillar(BasePillar):
    """Score how far headline and core inflation sit from target.

    Components and sub-weights:
        ``cpi_gap`` (0.40): ``cpi_yoy - inflation_target`` in percentage points.
        ``core_gap`` (0.60): ``core_cpi_yoy - inflation_target``, in percentage
            points.

    Core carries the larger share because it is the series central banks act on.
    Headline is retained because it drives household inflation expectations and,
    through them, the political pressure on the bank, which is a real channel
    even where the committee says it is looking through the number.

    Sign rule: above target is currency-positive, below target is
    currency-negative. Neither component is inverted. Inflation reaches an
    exchange rate almost entirely through the policy channel: an overshoot
    obliges the central bank to hold policy tight or tighten further, and the
    market prices that in the front end.

    The credibility caveat, and where it is handled. Above-target inflation is
    currency-positive only while the central bank is expected to respond to it.
    Where a bank is credibly and deliberately tolerating an overshoot, the same
    print is currency-negative: inflation erodes the real return on the currency
    while the nominal rate does not rise to compensate, and the yen through the
    yield curve control years is the reference case. This pillar does not model
    that switch. The ``real_policy_rate`` term inside the monetary pillar catches
    part of it, since a tolerated overshoot shows up as a falling real rate, and
    it is a partial fix rather than a complete one. Making it explicit is an open
    question in section 10 of ``docs/scoring-spec.md``, and it is deliberately
    left there rather than solved locally: a credibility gate built from the
    front end would be reading a market price that also moves for global reasons,
    and getting that wrong would flip the sign of a 0.15-weight pillar on a
    signal that is not purely domestic.

    Known failure modes: the sign assumption inverts in a stagflation, where
    inflation is high, growth is collapsing, and the market prices cuts anyway.
    The pillar reads the inflation as currency-positive at exactly the moment the
    currency is being sold. The growth pillar partially offsets this by moving
    the other way, which is one reason the two carry equal weight. Separately,
    year-on-year rates carry base effects: the gap moves when a large print from
    twelve months ago drops out of the window and nothing has happened this
    month. And the targets in `CurrencyMeta` are single numbers where several of
    these banks publish bands, so the AUD gap is measured against a 2.5% midpoint
    the RBA does not literally target.
    """

    name = PillarName.INFLATION
    requires: Sequence[str] = ("cpi_yoy", "core_cpi_yoy")
    headline_component = "cpi_gap"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, matching section 3.2 of
            ``docs/scoring-spec.md``.

        """
        return {"cpi_gap": 0.40, "core_gap": 0.60}

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull headline and core CPI per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}`` for the two indicators in
            `requires`, sorted by period ascending.

        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Difference the newest prints against each currency's own target.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}`` in percentage points, positive
            above target. Both values are plain differences against the target,
            with nothing else applied to them, which is what lets ``cpi_gap``
            serve as `headline_component` and reach a report as
            ``PillarScore.raw``. A reader seeing ``+0.9`` there is seeing
            inflation nine tenths of a point above the bank's own target, and
            ``notes`` should carry the level and the target alongside it so the
            number can be checked.

        Each currency's target comes from ``universe.meta(currency)``, so a
        currency outside the scored universe raises rather than silently
        defaulting to 2%. A currency missing ``core_cpi_yoy`` holds only 0.40 of
        the sub-weight, which is under `MIN_COMPONENT_WEIGHT`, so it is scored as
        missing: headline alone is too noisy to carry this pillar.

        """
        raise NotImplementedError
