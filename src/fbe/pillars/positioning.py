"""Positioning pillar: what speculative money has already done.

Weight 0.10. Every other pillar in this engine asks what should happen. This one
asks what has already been bought, which is a question about how much fuel is
left rather than about direction. Its response function is the only
non-monotonic transform in the model.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = [
    "PositioningPillar",
    "MOMENTUM_PEAK_Z",
    "SIGN_FLIP_Z",
    "CONTRARIAN_SATURATION_Z",
]


MOMENTUM_PEAK_Z: float = 1.0
"""Positioning z-score at which the momentum-confirming reading is strongest."""

SIGN_FLIP_Z: float = 2.0
"""Positioning z-score at which the pillar changes sign from confirming a move
to fading it."""

CONTRARIAN_SATURATION_Z: float = 3.0
"""Positioning z-score beyond which the contrarian reading stops growing."""


class PositioningPillar(BasePillar):
    """Score CFTC futures positioning with a non-monotonic response.

    The single component is ``cot_net_position`` expressed as a share of open
    interest in percent, then z-scored against that currency's own history over
    ``ScoringConfig.lookback_years``. The share is used rather than the raw
    contract count because open interest itself trends over years, and a net long
    of 100,000 contracts means one thing in a market of 200,000 and something
    else entirely in a market of 800,000. Call that z-score ``p``.

    The response function. ``p`` is mapped through ``response(p)``, which is
    piecewise linear, continuous, and symmetric about zero:

        ``|p| <= 1.0``: ``f = 0.5 * p``. Moderate positioning confirms. Money is
        flowing the same way the fundamentals point and the flow is itself part
        of why the pair moves.

        ``1.0 < |p| <= 2.0``: ``f = sign(p) * 0.5 * (2.0 - |p|)``. The confirming
        reading tapers back to zero. The trade is getting crowded, so the flow
        stops being evidence.

        ``|p| > 2.0``: ``f = -sign(p) * min(|p| - 2.0, 1.0)``. The sign has
        flipped and the pillar now fades the crowd, growing linearly until it
        saturates at ``-1.0`` when ``|p|`` reaches 3.0.

    The sign flips at ``|p| = 2.0``, where the function passes through zero, so
    the flip is continuous: a currency sitting on the boundary cannot jump from a
    large positive score to a large negative one on one week's data. The
    confirming branch peaks at ``+0.5`` while the contrarian branch reaches
    ``1.0``, so this pillar is deliberately twice as loud when it disagrees with
    a crowded consensus as when it agrees with a moderate one.

    Why crowded positioning is a risk signal, not a direction signal. A two
    standard deviation net long says the people who wanted to be long already
    are. The marginal buyer who would push the pair higher has been used up, and
    the remaining flow is asymmetric: good news buys little because it is priced
    into positions as well as into price, while bad news forces liquidation from
    holders who are all on the same side and all reach for the same exit. That is
    a statement about the fragility of the existing move, not a forecast that it
    reverses. The pillar expresses it by subtracting from the fundamental case
    rather than by asserting a direction of its own, and it is capped at 0.10 of
    the composite so it can shade a call and never make one.

    Sign rule after the transform: positive means positioning supports a long,
    either because moderate flow is confirming or because the crowd is extremely
    short and vulnerable to a squeeze. ``PillarScore.raw`` carries the net
    position as a percent of open interest, ``PillarScore.z`` carries the
    cross-sectional z-score of ``response(p)``, and ``p`` itself is recorded in
    ``notes`` so a report can show why a currency with a large net long scored
    negatively.

    Known failure modes: the data is old before it arrives. The COT report is a
    Tuesday snapshot published Friday afternoon, so it is three days stale on
    release and up to ten days stale by the end of the week it informs, which in
    a fast repricing is the entire move. It also covers listed futures only, a
    small and unrepresentative slice of a market that trades overwhelmingly in
    spot and forwards through bank channels, so it reads leveraged speculative
    accounts and is blind to real money and corporate flow. And extremes persist:
    positioning can sit above two standard deviations for months in a strong
    trend, during which this pillar fights the trend and loses.

    The USD problem. The CFTC currency futures complex has no dollar contract;
    every contract in it is quoted against the dollar, so a net long euro
    position is by construction a short dollar position. USD is handled by taking
    the ICE US Dollar Index futures COT where the registry supplies it, and
    otherwise by deriving the USD share of open interest as the negative of the
    open-interest-weighted mean of the other seven shares. The derivation happens
    before the time-series z-score so that the synthetic USD series has a history
    on the same footing as the real ones.
    """

    name = PillarName.POSITIONING
    requires: Sequence[str] = ("cot_net_position", "cot_open_interest")
    headline_component = "net_share"

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weight map for this single-component pillar.

        Returns:
            ``{"positioning_response": 1.0}``.
        """
        return {"positioning_response": 1.0}

    @staticmethod
    def response(p: float | None) -> float | None:
        """Map a positioning z-score onto the pillar's non-monotonic response.

        Args:
            p: Net position share z-scored against the currency's own history
                over ``ScoringConfig.lookback_years``, or ``None`` when the
                history is too short to z-score.

        Returns:
            The response in ``[-1.0, +0.5]``, or ``None`` when ``p`` is ``None``.
            Positive supports a long, negative fades the crowd. See the class
            docstring for the branch definitions and the reasoning.
        """
        if p is None:
            return None
        magnitude = abs(float(p))
        sign = 1.0 if p >= 0 else -1.0
        if magnitude <= MOMENTUM_PEAK_Z:
            return 0.5 * float(p)
        if magnitude <= SIGN_FLIP_Z:
            return sign * 0.5 * (SIGN_FLIP_Z - magnitude)
        excess = min(magnitude - SIGN_FLIP_Z, CONTRARIAN_SATURATION_Z - SIGN_FLIP_Z)
        return -sign * excess

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull the COT net position and open interest history per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later report dates are dropped.

        Returns:
            ``{currency: {indicator: observations}}`` covering at least
            ``ScoringConfig.lookback_years`` of weekly reports, since the
            time-series z-score needs the history and not just the latest print.

        If the registry publishes ``cot_net_position`` already divided by open
        interest, ``cot_open_interest`` will be absent and is not required. The
        extractor detects which form it has from ``Observation.unit``,
        ``"percent"`` for the normalised form and ``"contracts"`` for the raw
        count. This is the one contract this pillar needs the data source owner
        to confirm.
        """
        raise NotImplementedError

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Convert positioning history into the response value per currency.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {"positioning_response": value}}`` in the response
            function's own bounded units. A currency with fewer than two years of
            weekly reports inside the lookback window returns ``None``, because a
            positioning extreme has no meaning without a record of what normal
            looked like for that contract.
        """
        raise NotImplementedError
