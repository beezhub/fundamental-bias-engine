"""Monetary policy pillar: the dominant driver of G10 FX.

Carries the largest weight in the model at 0.30, which is roughly the weight the
market itself puts on it over a multi-day to multi-week horizon. Everything else
in this engine is a slower-moving backdrop that eventually expresses itself
through the policy path; this pillar measures the policy path directly.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["MonetaryPillar"]


class MonetaryPillar(BasePillar):
    """Score each currency on its policy rate, its front end, and their trend.

    Components and sub-weights:
        ``policy_rate`` (0.15): the current target rate in percent.
        ``yield_2y`` (0.15): the two-year government yield in percent, which
            prices the whole expected path rather than today's setting.
        ``yield_2y_1m`` (0.25): one-month change in the two-year yield, in
            percentage points.
        ``yield_2y_3m`` (0.30): three-month change in the two-year yield.
        ``real_policy_rate`` (0.15): ``policy_rate - cpi_yoy``, in percentage
            points. A high nominal rate against high inflation is not a strong
            currency, it is a central bank behind the curve.

    Why the change terms outweigh the levels, 0.55 against 0.45: the level of
    the carry is public, stable, and has been in the price for months, so it
    discriminates poorly across a cross-section that moves slowly. What moves a
    pair is the revision to the expected path, and the front-end yield is the
    cleanest observable proxy for that revision. The three-month change carries
    more than the one-month because a single month is dominated by whichever
    meeting or payroll print happened to land inside it, while a quarter of
    front-end movement is a repricing.

    Sign rule: positive means a higher or a rising rate path relative to the
    other seven currencies, and therefore a strong currency. All five components
    are already oriented that way, so no component is inverted.

    Known failure mode: the pillar is late to a turn. It reads a policy path
    that is already priced, so at the exact moment a central bank pivots, the
    2y yield gaps and the pillar swings hard after the FX move has happened.
    It is also blind to policy that is not expressed in rates. Quantitative
    easing, yield curve control and FX intervention all move a currency without
    moving the target rate, and the JPY years under yield curve control are the
    case where this pillar was confidently wrong for a long time. Where a
    currency's front end is administratively pinned, the change components go
    quiet and the pillar's opinion collapses to the carry level.
    """

    name = PillarName.MONETARY
    requires: Sequence[str] = ("policy_rate", "yield_2y", "cpi_yoy")
    headline_component = "yield_2y_3m"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, with the two change terms
            holding 0.55 between them.
        """
        return {
            "policy_rate": 0.15,
            "yield_2y": 0.15,
            "yield_2y_1m": 0.25,
            "yield_2y_3m": 0.30,
            "real_policy_rate": 0.15,
        }

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull policy rate, two-year yield and headline CPI per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}`` for the three indicators
            in `requires`, each sorted by period ascending.

        The two-year yield is daily, so ``yield_2y`` is resampled to month-end
        before it reaches `momentum`. Differencing raw daily observations would
        make a one-month change mean "twenty-one business days back from
        whichever day this series last updated", which is not comparable across
        currencies with different holiday calendars.
        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Build the five monetary components in percent and percentage points.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}`` over the components named in
            `component_weights`.

        Component construction:
            ``policy_rate`` and ``yield_2y`` are the newest values as published.
            ``yield_2y_1m`` and ``yield_2y_3m`` are `momentum` over the
            month-end series at one and three periods.
            ``real_policy_rate`` is the newest policy rate less the newest
            headline CPI year on year. Headline rather than core is used here
            because the deposit rate a saver compares against is the one that
            includes food and fuel; the core measure earns its place in the
            inflation pillar, where the question is what the central bank will
            do next rather than what the rate is worth today.

        A currency missing ``cpi_yoy`` returns ``None`` for
        ``real_policy_rate`` only; the other four still compute, and
        `blend_components` renormalises over 0.85 of the sub-weight, which
        clears `MIN_COMPONENT_WEIGHT`.
        """
        raise NotImplementedError
