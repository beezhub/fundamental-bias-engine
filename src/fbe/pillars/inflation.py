"""Inflation pillar: distance from target, and whether policy is answering it.

Weight 0.15. The pillar never scores the level of inflation. A 3% print is hot
in Zurich, where the SNB targets 1%, and unremarkable in Sydney, where the RBA
aims at the middle of a 2-3% band. What travels across a cross-section is the
gap between inflation and the number that currency's central bank has promised
to deliver, which is what `CurrencyMeta.inflation_target` holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["InflationPillar", "GATE_SATURATION_PP"]


GATE_SATURATION_PP: float = 0.10
"""Front-end move, in percentage points over three months, at which the policy
response gate reaches full strength. Below this the gate is scaled linearly, so
the sign change at a flat front end is continuous rather than a jump."""


class InflationPillar(BasePillar):
    """Score deviation from target, gated by whether the central bank responds.

    Components and sub-weights:
        ``cpi_gap`` (0.30): ``cpi_yoy - inflation_target`` in percentage points,
            gated as described below.
        ``core_gap`` (0.30): ``core_cpi_yoy - inflation_target``, gated the same
            way. Core carries equal weight to headline because it is what the
            committee actually reacts to.
        ``core_trend`` (0.40): three-month change in ``core_cpi_yoy`` in
            percentage points, ungated. It holds the largest sub-weight because
            the level of inflation has been forecast and traded for months while
            the change in the trend is the part that forces a revision.

    The two-sided channel. Above-target inflation reaches the currency through
    two opposite routes and the pillar has to choose between them run by run.
    The usual route is the policy response: inflation overshoots, the market
    prices tightening, real and nominal rates rise, the currency rallies. The
    other route opens when the central bank is credibly refusing to respond.
    Then the overshoot is pure debasement, the real rate falls as inflation
    climbs past a static nominal rate, and the currency sells off. The yen
    through the yield curve control years is the reference case: inflation rose,
    policy did not move, and the currency fell rather than rose.

    The gate that picks between them. Let ``r`` be the three-month change in the
    two-year yield in percentage points, the same quantity the monetary pillar
    uses, and let ``g = clip(r / GATE_SATURATION_PP, -1, +1)``. For a positive
    gap the component value is ``gap * g``, so a front end that is repricing
    higher makes the overshoot currency-positive, a front end that is repricing
    lower makes the same overshoot currency-negative, and a flat front end
    scores it near zero. The pivot sits at ``r = 0`` on purpose: an unchanged
    expected path while inflation runs hot means the real path is falling, which
    is a refusal even though nobody announced one. The function is continuous at
    the pivot, so a currency sitting on the boundary cannot flip between a large
    positive and a large negative score on a basis point of yield.

    For a negative gap the value is the gap itself, ungated. A central bank
    failing to lift inflation back to target is the ordinary condition of the G10
    at the lower bound and carries no extra information, whereas a bank refusing
    to fight an overshoot is a genuine regime. Undershooting is read as
    currency-negative in every case.

    Sign rule: positive means inflation is above target with policy responding,
    or core is accelerating. Negative means inflation is below target, or above
    target with policy standing still.

    Known failure modes: the gate is read off a market price, so a global rates
    shock that moves every front end at once is misread as eight simultaneous
    domestic policy signals. Year-on-year rates carry base effects, so
    ``core_trend`` records disinflation when a large print from twelve months ago
    drops out of the window and nothing happened this month. And the targets in
    `CurrencyMeta` are single numbers where several of these banks publish bands,
    so the AUD gap is measured against a 2.5% midpoint the RBA does not literally
    target.
    """

    name = PillarName.INFLATION
    requires: Sequence[str] = ("cpi_yoy", "core_cpi_yoy", "yield_2y")
    headline_component = "cpi_gap"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0.

        """
        return {"cpi_gap": 0.30, "core_gap": 0.30, "core_trend": 0.40}

    @staticmethod
    def response_gate(front_end_change: float | None) -> float:
        """Return the policy response gate for a given front-end move.

        Args:
            front_end_change: Three-month change in the two-year yield in
                percentage points, or ``None`` when the yield series is missing.

        Returns:
            ``clip(front_end_change / GATE_SATURATION_PP, -1.0, +1.0)``.
            Returns ``0.0`` for ``None``, which neutralises the two level
            components rather than guessing at the central bank's intent, and
            leaves ``core_trend`` carrying the pillar on its own.

        """
        if front_end_change is None:
            return 0.0
        scaled = float(front_end_change) / GATE_SATURATION_PP
        return max(-1.0, min(1.0, scaled))

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull headline CPI, core CPI and the two-year yield per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}`` for the three indicators in
            `requires`, sorted by period ascending. The yield series is resampled
            to month-end for the same reason as in the monetary pillar.

        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Build the gapped and gated inflation components.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}`` in percentage points.

        Each currency's target comes from ``universe.meta(currency)``, so a
        currency outside the scored universe raises rather than silently
        defaulting to 2%. A currency missing ``core_cpi_yoy`` loses 0.70 of the
        sub-weight, falls under `MIN_COMPONENT_WEIGHT`, and is scored as missing:
        headline alone is too noisy to carry this pillar.

        """
        raise NotImplementedError
