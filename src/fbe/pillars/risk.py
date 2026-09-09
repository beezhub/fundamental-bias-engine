"""Risk regime pillar: the same currencies, re-signed by the weather.

Weight 0.10. The other six pillars hold a fixed view of what makes a currency
strong. This one does not: the yen is the best currency in the world on the worst
day of the year and an unremarkable one in a quiet rally, and it is the same yen.
The pillar reads the regime once, globally, and hands each currency its own share
of it through ``CurrencyMeta.risk_beta``.

In a genuine risk-off episode, correlations across G10 FX collapse into a single
factor. Funding and haven currencies are bought regardless of their macro
picture, and high-beta commodity currencies are sold regardless of theirs. On
those days the other six pillars are approximately noise. On all other days this
pillar should be small, and the construction below makes it small automatically
rather than by rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = [
    "RiskPillar",
    "DRAWDOWN_WINDOW_SESSIONS",
    "DRAWDOWN_SCALE_PCT",
    "VOL_SCALE_Z",
    "RISK_SCALE",
]


DRAWDOWN_WINDOW_SESSIONS: int = 252
"""Trailing sessions defining the 52-week high the drawdown is measured from."""

DRAWDOWN_SCALE_PCT: float = 10.0
"""Equity drawdown, in percent, that saturates the drawdown component.

A 10% fall from the 52-week high is a correction that has everybody's attention.
Scaling by it means an ordinary 3% pullback contributes -0.3 rather than
registering as a crisis.
"""

VOL_SCALE_Z: float = 2.0
"""Volatility z-score that saturates the volatility component."""

RISK_SCALE: float = 2.0
"""Multiplier turning ``R * risk_beta`` into a score on the band.

At 2.0 a full risk-off shock against the highest-beta currency, AUD at +0.9,
scores ``2.0 * -1.0 * 0.9 = -1.8``, which is comparable to the magnitude the
z-scored pillars reach in practice. Without it this pillar would be systematically
quieter than the other six and would carry less than the 0.10 it is configured
for.
"""


class RiskPillar(BasePillar):
    """Read one global risk regime and score each currency by its beta to it.

    Constructing the regime. Two global inputs, neither of them currency
    specific, are combined into a single number ``R`` shared by all eight:

        ``dd_component = clip(equity_drawdown_pct / 10.0, 1.0)``, upper bounded
        at ``0.0``, where ``equity_drawdown_pct`` is the current drawdown of a
        broad equity index from its 52-week high, as a negative percent.

        ``vol_component = clip(-vol_z / 2.0, 1.0)``, where ``vol_z`` is the
        z-score of a volatility index over ``ScoringConfig.lookback_years``. The
        negation is what makes high volatility contribute negative, so both
        components run the same way.

        ``R = 0.5 * dd_component + 0.5 * vol_component``, then bounded above at
        ``0.0``.

    ``R`` runs from ``-1.0`` at maximum risk-off to ``0.0`` in calm. The equal
    weighting is a choice rather than a fit: drawdown is where the money has been
    lost and volatility is what the market thinks comes next, and neither leads
    the other reliably enough to earn more.

    Why the upper bound at zero. Euphoria is not modelled as the mirror of panic.
    A risk-on melt-up is a far weaker and slower force in currency markets than a
    risk-off shock, and treating the two symmetrically would have the pillar
    making confident high-beta calls during quiet rallies, which is exactly when
    it knows least. So the pillar is silent or negative, never positive about the
    regime itself. Individual currencies still score positively, because a
    negative ``R`` times a negative ``risk_beta`` is a positive number: that is
    the haven bid, and it is the only way this pillar says anything good about
    anyone.

    Scoring a currency:

        ``score(c) = clip(2.0 * R * risk_beta(c), score_clip)``

    Check the signs in a risk-off, where ``R`` is negative. The yen at
    ``risk_beta = -0.9`` scores strongly positive; the Australian dollar at
    ``+0.9`` scores strongly negative. As the regime calms toward ``R = 0`` both
    fade to nothing together. The pillar re-signs itself because the regime term
    is a signed scalar, not because there is a table of cases anywhere.

    There is no regime classification and therefore no boundary. An earlier draft
    had thresholds separating risk-on, neutral and risk-off, which meant one
    session of the volatility index could flip every currency in the universe
    between large scores. The continuous form removes the question: the score is
    linear in ``R``, so a marginal change in the regime produces a marginal change
    in the score, and the only point where the sign could flip is ``R = 0``, where
    the score is zero anyway.

    Why this pillar does not use cross-sectional normalisation. It is one of the
    two exceptions, the other being positioning. ``R`` is a single number shared
    by all eight currencies, so ``2 * R * beta`` is an affine function of a fixed
    vector of betas. Dividing by its own cross-sectional standard deviation would
    cancel ``R`` out entirely and leave a constant ordering of betas that never
    changed from run to run, which is the one thing this pillar must not be. It is
    already signed, already comparable across currencies, and already on the
    band, so `_normalise` passes it through.

    A property worth knowing when reading a calm run. At ``R = 0`` every currency
    scores exactly ``0.0``, but the pillar is present rather than missing: its
    ``z`` is a real zero, so it still counts toward `coverage` and still occupies
    0.10 of the composite's denominator. In a quiet market it therefore pulls
    every composite about a tenth of the way toward zero. That is intended, since
    a calm market genuinely offers no regime signal in either direction, but it is
    different from the pillar being absent and the report should not read it as a
    data outage.

    Known failure modes: the proxies are American. ``equity_index`` and
    ``vol_index`` are a broad US index and its implied volatility, so what this
    pillar measures is US risk appetite, and a European or Japanese shock only
    registers once it crosses the Atlantic. It also fires late, because an equity
    drawdown is a consequence of risk-off rather than a leading indicator of it,
    and a 6% drawdown that is a healthy correction reads identically to a 6%
    drawdown that is the start of a crisis.

    ``risk_beta`` is a static constant standing in for a relationship that is
    neither static nor stable. The yen's haven behaviour weakens when Japanese
    rates are rising, the franc's changed under SNB intervention and negative
    rates, and the dollar's depends on whether the shock originated inside or
    outside the United States. Worst of all is the rates-led selloff, where bonds
    and equities fall together, the dollar rallies on rate differentials rather
    than on safety, and the yen falls in a risk-off tape. In that regime this
    pillar is confidently wrong on the yen, and the tell is disagreement with the
    monetary pillar, which surfaces as elevated ``dispersion`` on the
    `CurrencyScore` and demotes conviction on every pair using that leg.
    """

    name = PillarName.RISK
    requires: Sequence[str] = ("equity_index", "vol_index")
    headline_component = "regime"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weight map for this single-component pillar.

        Returns:
            ``{"risk_response": 1.0}``.

        """
        return {"risk_response": 1.0}

    @staticmethod
    def regime(drawdown_pct: float, vol_z: float) -> float:
        """Combine equity drawdown and volatility into the global regime reading.

        Args:
            drawdown_pct: Current drawdown of the equity index from its 52-week
                high, as a negative percent. A 6.5% fall is ``-6.5``.
            vol_z: Z-score of the volatility index over
                ``ScoringConfig.lookback_years``. Positive means volatility is
                above its own average.

        Returns:
            ``R`` in ``[-1.0, 0.0]``, where ``-1.0`` is maximum risk-off and
            ``0.0`` is calm. See the class docstring for the reasoning behind the
            upper bound.

        Note where the two bounds sit, because they are not in the same place.
        The drawdown component is bounded above at zero individually, since a
        market at a new high is calm rather than euphoric. The volatility
        component is not, so unusually low volatility can offset a shallow
        drawdown, and it is the combined ``R`` that is bounded above at zero
        afterwards.

        """
        dd = max(-1.0, min(0.0, float(drawdown_pct) / DRAWDOWN_SCALE_PCT))
        vol = max(-1.0, min(1.0, -float(vol_z) / VOL_SCALE_Z))
        return min(0.0, 0.5 * dd + 0.5 * vol)

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull the global equity and volatility series.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}`` with the same two global
            series repeated under every currency key. The repetition is wasteful
            and it is on purpose: it keeps this pillar inside the same
            per-currency contract as the other six, so `compute` needs no special
            case for it. Both series carry ``currency="GLOBAL"`` as published and
            the extractor routes them to every key.

        The equity series needs `DRAWDOWN_WINDOW_SESSIONS` of history to find the
        52-week high, and the volatility series needs
        ``ScoringConfig.lookback_years`` for its z-score.

        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Read the regime once, then apply each currency's beta to it.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {"risk_response": value, "regime": R}}``.
            ``risk_response`` is ``2.0 * R * risk_beta(currency)``, unitless and
            already on the score band, and is the only component carrying
            sub-weight. ``regime`` is ``R`` itself, identical across all eight
            currencies, carried so `headline_component` can report it; it takes
            no part in the blend.

        Missing data: if either global series is unusable the whole pillar
        returns ``None`` for every currency and the scorer drops 0.10 of weight
        from all eight composites at once. That is the correct behaviour, because
        an unmeasurable regime is unmeasurable for everybody, and it is also the
        one case where every currency's coverage falls together and the run should
        be read with more caution.

        """
        raise NotImplementedError

    def _normalise(
        self,
        components: Mapping[str, Mapping[str, float | None]],
    ) -> Mapping[str, float | None]:
        """Return the risk response unchanged, without cross-sectional scaling.

        Args:
            components: Output of `_transform`.

        Returns:
            ``{currency: value}`` taken straight from ``"risk_response"``. See
            the class docstring for why standardising here would cancel the
            regime out and leave a constant ranking of betas.

        """
        raise NotImplementedError
