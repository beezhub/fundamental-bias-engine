"""Risk regime pillar: the same currencies, re-signed by the weather.

Weight 0.10. The other six pillars hold a fixed view of what makes a currency
strong. This one does not: the yen is the best currency in the world on the
worst day of the year and the worst currency in a quiet rally, and it is the
same yen. The pillar measures the regime once, globally, and then hands each
currency its own share of it through ``CurrencyMeta.risk_beta``.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = ["RiskPillar", "REGIME_THRESHOLD", "DRAWDOWN_WINDOW_SESSIONS"]


REGIME_THRESHOLD: float = 0.5
"""Absolute stress score at which a regime is called risk-on or risk-off, and
the point at which the pillar reaches full strength."""

DRAWDOWN_WINDOW_SESSIONS: int = 252
"""Trailing sessions used for the equity peak the drawdown is measured from."""


class RiskPillar(BasePillar):
    """Classify the risk regime and score each currency by its beta to it.

    Constructing the regime. Two global inputs, neither of them currency
    specific, are combined into one stress score:

        Drawdown: ``equity_index`` divided by its trailing
        `DRAWDOWN_WINDOW_SESSIONS` maximum, less one, in percent. Always zero or
        negative. This series is z-scored against its own history over
        ``ScoringConfig.lookback_years`` and the sign is flipped, so a deep
        drawdown contributes positive stress.

        Volatility: ``vix``, z-scored against its own history over the same
        window. Higher implied volatility is positive stress.

    ``stress`` is the equally weighted mean of the two. The equal weighting is a
    choice, not a fit: drawdown is where the money has been lost and volatility
    is what the market thinks comes next, and neither leads the other reliably
    enough to earn more weight.

    Classifying it. ``stress >= +REGIME_THRESHOLD`` is risk-off,
    ``stress <= -REGIME_THRESHOLD`` is risk-on, and the interval between them is
    neutral. The labels appear in ``PillarScore.notes`` and in the report; they
    do not enter the arithmetic, which is deliberate.

    What happens at the boundary. A hard switch at a threshold would let one
    session of the VIX flip every currency in the universe from a large positive
    score to a large negative one, which is not how regimes behave and is not a
    signal anyone should trade. Instead the pillar damps itself continuously:
    the score is multiplied by ``min(|stress| / REGIME_THRESHOLD, 1.0)``, so it
    reaches full strength exactly where the regime is declared and fades linearly
    to nothing at ``stress = 0``. Inside the neutral band the pillar is quiet
    rather than absent, and the sign change passes through zero, so a currency
    can never jump across the boundary. The cost is that the pillar is slow to
    speak at the start of a shock, which is the right way round for a model whose
    output is held for days.

    Scoring a currency. ``score = -risk_beta_relative * stress * damping``, where
    ``risk_beta_relative`` is the currency's ``risk_beta`` less the mean beta
    across the scored universe. Check the signs: in risk-off ``stress`` is
    positive, and the yen at ``risk_beta = -0.9`` scores strongly positive while
    the Australian dollar at ``+0.9`` scores strongly negative. In risk-on both
    reverse. The pillar re-signs itself with the regime because the regime term
    is a signed scalar, not because there is a table of cases anywhere.

    Why this pillar does not use cross-sectional normalisation. Everywhere else
    in the engine, standardising across the eight currencies is what makes scores
    comparable. Here it would destroy the signal. ``stress`` is one number shared
    by all eight currencies, so ``-beta * stress`` is an affine function of a
    fixed vector of betas, and dividing by its own cross-sectional standard
    deviation would cancel ``stress`` out entirely and leave a constant ordering
    of betas that never changes from run to run. The pillar therefore overrides
    `_normalise` to pass its value through unchanged. It is already unitless,
    being a bounded beta multiplied by a z-score, so it lands on the same scale
    as the other pillars without the second step. Demeaning the betas is what
    preserves the relative-value discipline: the pillar cannot push all eight
    currencies the same way, only rank them against each other.

    Known failure modes: the proxies are American. ``equity_index`` and ``vix``
    are the S&P 500 and its implied volatility, so what this pillar actually
    measures is US risk appetite, and a European or Japanese shock only registers
    once it crosses the Atlantic. The betas are static constants set by hand and
    they drift: the franc's haven behaviour changed under SNB intervention and
    negative rates, and the yen's weakened badly once it became the market's
    funding currency at zero rates. Worst of all is the rates-led selloff, where
    bonds and equities fall together, the dollar rallies on rate differentials
    rather than on safety, and the yen falls in a risk-off tape. In that regime
    this pillar is confidently wrong on the yen and should be watched for
    disagreement with the monetary pillar, which shows up as elevated
    ``dispersion`` on the `CurrencyScore`.
    """

    name = PillarName.RISK
    requires: Sequence[str] = ("equity_index", "vix")
    headline_component = "stress"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weight map for this single-component pillar.

        Returns:
            ``{"risk_response": 1.0}``.
        """
        return {"risk_response": 1.0}

    @staticmethod
    def damping(stress: float) -> float:
        """Return the boundary damping factor for a stress score.

        Args:
            stress: The combined drawdown and volatility stress score.

        Returns:
            ``min(abs(stress) / REGIME_THRESHOLD, 1.0)``, in ``[0.0, 1.0]``.
            One inside a declared regime, tapering linearly to zero at neutral.
        """
        return min(abs(float(stress)) / REGIME_THRESHOLD, 1.0)

    @staticmethod
    def classify(stress: float) -> str:
        """Return the regime label for a stress score.

        Args:
            stress: The combined stress score.

        Returns:
            ``"risk_off"``, ``"risk_on"`` or ``"neutral"``. The label is for the
            report only; the score is computed from ``stress`` directly and does
            not branch on the label.
        """
        if stress >= REGIME_THRESHOLD:
            return "risk_off"
        if stress <= -REGIME_THRESHOLD:
            return "risk_on"
        return "neutral"

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
            case for it. Both series carry ``currency="GLOBAL"`` as published;
            the extractor routes them to every key. Both need at least
            ``ScoringConfig.lookback_years`` of history for the time-series
            z-scores, and the equity series needs
            `DRAWDOWN_WINDOW_SESSIONS` of history on top of that.
        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Build the stress score once, then apply each currency's beta.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {"risk_response": value}}``. The value is
            ``-risk_beta_relative * stress * damping(stress)``, unitless.

        Missing data: if either global series is unusable the whole pillar
        returns ``None`` for every currency and the scorer drops 0.10 of weight
        from all eight composites at once. That is the correct behaviour, because
        an unmeasurable regime is unmeasurable for everybody, and it is also the
        one case where every currency's coverage falls together and the whole
        run should be read with more caution.
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
