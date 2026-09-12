"""Monetary policy pillar: the dominant driver of G10 FX.

Carries the largest weight in the model at 0.30, which is roughly the weight the
market itself puts on it over a multi-day to multi-week horizon. Everything else
in this engine is a slower-moving backdrop that eventually expresses itself
through the policy path; this pillar measures the policy path directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["MonetaryPillar"]


class MonetaryPillar(BasePillar):
    """Score each currency on its policy rate, its front end, and their trend.

    Components and sub-weights:
        ``policy_rate`` (0.15): the current target rate in percent.
        ``yield_2y`` (0.25): the two-year government yield in percent.
        ``yield_2y_chg_1m`` (0.20): one-month change in the two-year yield, in
            basis points.
        ``yield_2y_chg_3m`` (0.25): three-month change in the two-year yield, in
            basis points.
        ``real_policy_rate`` (0.15): ``policy_rate - cpi_yoy``, in percentage
            points. A high nominal rate against high inflation is not a strong
            currency, it is a central bank behind the curve.

    How to read the weighting. The instinct is to split these into levels and
    changes and argue about which should dominate, but that miscategorises the
    two-year yield. The 2y is not a backward-looking level: it is the market's
    expectation of the policy path, already priced, so 0.25 on it is not a vote
    for levels over direction of travel. The genuinely backward-looking term is
    the policy rate, which is what the central bank has already done, and it
    carries the joint-lowest weight at 0.15. Between them the 2y level and its
    two changes hold 0.70 of the pillar, and all three are statements about the
    expected path rather than the current setting.

    The three-month change carries more than the one-month because a single
    month is dominated by whichever meeting or payroll print happened to land
    inside it, while a quarter of front-end movement is a repricing.

    Sign rule: positive means a higher or a rising rate path relative to the
    other seven currencies, and therefore a strong currency. All five components
    are already oriented that way, so no component is inverted.

    ``real_policy_rate`` makes this pillar partly an inflation pillar with the
    opposite sign to INFLATION, and the sub-weights above do not show it. Per
    percentage point of headline CPI the term's composite loading on the section
    7 fixture is -0.0501, against INFLATION's +0.1008 on the same series. Section
    3.2 of ``docs/scoring-spec.md`` publishes both sides, `scoring.series_loading`
    computes them, and ADR 0003 records why the term was kept.

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
    requires: Sequence[str] = (
        "policy_rate",
        "yield_2y",
        "yield_2y_chg_1m",
        "yield_2y_chg_3m",
        "cpi_yoy",
    )
    headline_component = "yield_2y_chg_3m"

    component_indicators: Mapping[str, tuple[str, ...]] = {
        "policy_rate": ("policy_rate",),
        "yield_2y": ("yield_2y",),
        "yield_2y_chg_1m": ("yield_2y_chg_1m",),
        "yield_2y_chg_3m": ("yield_2y_chg_3m",),
        "real_policy_rate": ("policy_rate", "cpi_yoy"),
    }
    """The real policy rate names both of its inputs.

    It is a policy rate less a headline CPI print, a daily series and a monthly
    or quarterly one in the same component, and the CPI half is the one that
    goes stale. It therefore takes the CPI factor, since a component is as stale
    as its stalest input. The two change series arrive from the registry under
    their own keys, as `_extract` describes, so each ages against itself.
    """

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, matching section 3.1 of
            ``docs/scoring-spec.md``.

        """
        return {
            "policy_rate": 0.15,
            "yield_2y": 0.25,
            "yield_2y_chg_1m": 0.20,
            "yield_2y_chg_3m": 0.25,
            "real_policy_rate": 0.15,
        }

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull the rate, yield, yield-change and CPI series per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}`` for the five indicators in
            `requires`, each sorted by period ascending.

        The two change series arrive as their own keys, ``yield_2y_chg_1m`` and
        ``yield_2y_chg_3m``, rather than being differenced here, so the
        definition of "one month back" lives in one place: the registry's
        ``chg_1m``/``chg_3m`` transform, which resamples to month-end or
        quarter-end before differencing. That matters because the two-year
        yield is daily and the currencies keep different holiday calendars;
        differencing raw daily observations locally would make a one-month
        change mean "twenty-one business days back from whichever day this
        series last updated", which is not comparable across the
        cross-section. Neither change key is backed by an independently
        published series; both reuse ``yield_2y``'s own identifiers under a
        different transform, so a currency's coverage on the two change
        components always matches its coverage on the level.

        """
        raise NotImplementedError(
            "fbe.pillars.monetary.MonetaryPillar._extract is scaffolded; "
            "see docs/roadmap.md Phase 2"
        )

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
            `component_weights`. Units differ between components, which is why
            each is z-scored separately before the blend.

        Component construction:
            ``policy_rate`` and ``yield_2y`` are the newest values as published,
            in percent. ``yield_2y_chg_1m`` and ``yield_2y_chg_3m`` are the
            newest published changes in basis points. ``real_policy_rate`` is
            the newest policy rate less the newest headline CPI year on year, in
            percentage points. Headline rather than core is used here
            because the deposit rate a saver compares against is the one that
            includes food and fuel; the core measure earns its place in the
            inflation pillar, where the question is what the central bank will
            do next rather than what the rate is worth today.

        A currency missing ``cpi_yoy`` returns ``None`` for
        ``real_policy_rate`` only; the other four still compute, and
        `blend_components` renormalises over 0.85 of the sub-weight, which
        clears `MIN_COMPONENT_WEIGHT`.

        """
        raise NotImplementedError(
            "fbe.pillars.monetary.MonetaryPillar._transform is scaffolded; "
            "see docs/roadmap.md Phase 2"
        )
