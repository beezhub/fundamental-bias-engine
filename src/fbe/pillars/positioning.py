"""Positioning pillar: what speculative money has already done.

Weight 0.10. Every other pillar in this engine asks what should happen. This one
asks what has already been bought, which is a question about how much fuel is
left rather than about direction. Its response function is the only
non-monotonic transform in the model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = [
    "PositioningPillar",
    "MOMENTUM_PEAK_Z",
    "SIGN_FLIP_Z",
    "CONTRARIAN_SLOPE",
    "CONTRARIAN_CAP",
]


MOMENTUM_PEAK_Z: float = 1.0
"""Positioning z-score at which the momentum-confirming reading is strongest."""

SIGN_FLIP_Z: float = 2.0
"""Positioning z-score at which the pillar changes sign from confirming a move
to fading it."""

CONTRARIAN_SLOPE: float = 1.5
"""Slope of the contrarian branch beyond `SIGN_FLIP_Z`, in score-band units per
unit of positioning z."""

CONTRARIAN_CAP: float = 2.0
"""Largest magnitude the contrarian branch may reach, hit at ``|p| = 3.33``.

Deliberately short of ``ScoringConfig.score_clip`` (3.0). The other five
cross-sectional pillars are z-scores over an eight-name universe, where the
arithmetic maximum is ``sqrt(7) = 2.65`` and blended sub-indicators land nearer
1.5 in practice, so a pillar allowed to reach 3.0 would be the loudest voice in
the model at its extreme. This is the pillar with the weakest data behind it, so
the clip is left as what it is elsewhere in the engine, a backstop against data
errors, rather than becoming a routine operating point for CFTC futures data.
"""


class PositioningPillar(BasePillar):
    """Score CFTC futures positioning with a non-monotonic response.

    The single component is ``cot_net_pct_oi``, net non-commercial positioning
    as a share of open interest in percent, computed by the data source as
    ``(non_commercial_long - non_commercial_short) / open_interest`` and
    z-scored against that currency's own history over
    ``ScoringConfig.lookback_years``. The share is used rather than the raw
    contract count because open interest itself trends over years, and a net long
    of 100,000 contracts means one thing in a market of 200,000 and something
    else entirely in a market of 800,000. Call that z-score ``p``.

    The response function. ``p`` is mapped through ``response(p)``, which is
    piecewise linear, continuous, and odd, so ``f(-p) == -f(p)`` and there is no
    long or short asymmetry:

        ``|p| <= 1.0``: ``f = p``. Moderate positioning confirms. Money is
        flowing the same way the fundamentals point and the flow is itself part
        of why the pair moves.

        ``1.0 < |p| <= 2.0``: ``f = sign(p) * (2.0 - |p|)``. The confirming
        reading tapers back to zero. The trade is getting crowded, so the flow
        stops being evidence.

        ``|p| > 2.0``: ``f = -sign(p) * min(1.5 * (|p| - 2.0), 2.0)``. The sign
        has flipped and the pillar now fades the crowd with increasing force
        until it saturates at magnitude `CONTRARIAN_CAP` when ``|p|`` reaches
        3.33, and goes no further however extreme positioning becomes.

    Properties worth asserting in tests: continuous at both joins and at the
    saturation point, magnitude 1.0 from either side at ``|p| = 1.0`` and 0.0
    from either side at ``|p| = 2.0``; odd about zero; peak confirming magnitude
    1.0 at ``|p| = 1.0``; sign change at ``|p| = 2.0``; magnitude 2.0 from
    ``|p| = 3.33`` upward, never reaching the ``3.0`` clip.

    The sign flips at ``|p| = 2.0``, where the function passes through zero, so
    the flip is continuous: a currency sitting on the boundary cannot jump from a
    large positive score to a large negative one on one week's data. The
    confirming branch peaks at 1.0 while the contrarian branch runs to 2.0, so
    the pillar is twice as loud when it disagrees with a crowded consensus as
    when it agrees with a moderate one. That asymmetry is deliberate: a crowded
    position is a fact about who is left to buy, which is firmer evidence than a
    comfortable position, which is only evidence that a trade is working. The
    saturation is what keeps the asymmetry from running away; see
    `CONTRARIAN_CAP`.

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
    position as a percent of open interest, ``PillarScore.z`` carries
    ``response(p)`` before clipping, and ``p`` itself is recorded in ``notes`` so
    a report can show why a currency with a large net long scored negatively.

    No cross-sectional step. This is one of the two pillars that does not
    normalise across the universe, the other being risk. Positioning is
    meaningful relative to how crowded that currency usually is, not relative to
    how crowded the other seven happen to be, so the normalisation is
    time-series and the shape function already emits on the ``-3..+3`` band.
    Z-scoring the result across eight currencies afterwards would rescale a
    construction that already has meaning in its own units, and would destroy the
    property the shape function exists to create: that ``f`` near zero means this
    currency's positioning says nothing, not that it is average for today.

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
    requires: Sequence[str] = ("cot_net_pct_oi",)
    headline_component = "net_share"

    component_indicators: Mapping[str, tuple[str, ...]] = {
        "positioning_response": ("cot_net_pct_oi",),
    }
    """One component, one weekly series. The COT report is published on Friday
    for the Tuesday, so this is one of the two pillars whose inputs were never
    at risk from the ramp.
    """

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
            The response, bounded to ``[-2.0, +2.0]`` by `CONTRARIAN_CAP`, or
            ``None`` when ``p`` is ``None``. Positive supports a long, negative
            fades the crowd. See the class docstring for the branch definitions
            and the reasoning.

        """
        if p is None:
            return None
        magnitude = abs(float(p))
        sign = 1.0 if p >= 0 else -1.0
        if magnitude <= MOMENTUM_PEAK_Z:
            return float(p)
        if magnitude <= SIGN_FLIP_Z:
            return sign * (SIGN_FLIP_Z - magnitude)
        excess = CONTRARIAN_SLOPE * (magnitude - SIGN_FLIP_Z)
        return -sign * min(excess, CONTRARIAN_CAP)

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull the share-of-open-interest history per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later report dates are dropped.

        Returns:
            ``{currency: {"cot_net_pct_oi": observations}}`` covering at least
            ``ScoringConfig.lookback_years`` of weekly reports, since the
            time-series z-score needs the history and not just the latest print.

        The key carries its own normalisation: ``cot_net_pct_oi`` arrives already
        divided by open interest, in percent, so this pillar never sees a raw
        contract count and never has to guess which form it was handed.

        """
        raise NotImplementedError(
            "fbe.pillars.positioning.PositioningPillar._extract is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )

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
            ``{currency: {"positioning_response": value, "net_share": share}}``.
            ``positioning_response`` is the shape function's output and is the
            only component carrying sub-weight. ``net_share`` is the latest net
            non-commercial position as a percent of open interest, carried purely
            so `headline_component` can report a number a human recognises; it
            takes no part in the blend.

        A currency with fewer than two years of weekly reports inside the
        lookback window returns ``None`` for the response, because a positioning
        extreme has no meaning without a record of what normal looked like for
        that contract. The positioning z-score ``p`` is written to
        ``PillarScore.notes`` so a report can explain a currency that scored
        negatively while holding a large net long.

        """
        raise NotImplementedError(
            "fbe.pillars.positioning.PositioningPillar._transform is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )

    def _normalise(
        self,
        components: Mapping[str, Mapping[str, float | None]],
    ) -> Mapping[str, float | None]:
        """Return the shape function's output without cross-sectional scaling.

        Args:
            components: Output of `_transform`.

        Returns:
            ``{currency: value}`` taken straight from ``"positioning_response"``.
            See the class docstring for why standardising across the universe
            would undo what the shape function is for.

        """
        raise NotImplementedError(
            "fbe.pillars.positioning.PositioningPillar._normalise is scaffolded; "
            "see docs/roadmap.md Phase 3"
        )
