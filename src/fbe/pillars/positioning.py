"""Positioning pillar: what speculative money has already done.

Weight 0.10. Every other pillar in this engine asks what should happen. This one
asks what has already been bought, which is a question about how much fuel is
left rather than about direction. Its response function is the only
non-monotonic transform in the model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar, _years_earlier
from fbe.types import Observation, PillarName

__all__ = [
    "INDICATOR",
    "PositioningPillar",
    "MOMENTUM_PEAK_Z",
    "SIGN_FLIP_Z",
    "CONTRARIAN_SLOPE",
    "CONTRARIAN_CAP",
]


INDICATOR = "cot_net_pct_oi"
"""The one series this pillar reads, named once rather than at four call sites.

Section 3.6 gives POSITIONING a single sub-indicator at sub-weight 1.00, so the
key appears in `requires`, in `component_indicators`, in `_transform` and in
`_notes`, and a typo in any one of them would read as a currency with no
contract rather than as an error."""

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

    The single component is ``cot_net_pct_oi``, the net leveraged-funds
    position as a percent of open interest, computed by the data source as
    ``(lev_money_long - lev_money_short) / open_interest * 100`` and z-scored
    against that currency's own history over ``ScoringConfig.lookback_years``.
    The percent is used rather than the raw contract count because open interest
    itself trends over years, and a net long of 100,000 contracts means one thing
    in a market of 200,000 and something else entirely in a market of 800,000.
    Leveraged funds rather than the Legacy report's non-commercial bucket, for
    the reason ADR 0011 records. Call that z-score ``p``.

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
    position is by construction a short dollar position. The dollar reading is
    derived in the data source, as the negative of the **sum** of the other
    seven legs, each already a percent of its own open interest. It arrives
    under ``cot_net_pct_oi`` for ``"USD"`` like any other reading, so the
    derivation is behind the time-series z-score and the synthetic series has a
    history on the same footing as the real ones.

    Not an open-interest-weighted mean, which this docstring specified until
    ADR 0011. That quantity is ``sum(net) / sum(open_interest)``, the raw
    contract-count sum over total open interest, which is the euro-dominated
    reading the source exists to avoid: on the 2026-09-08 capture it reads
    +2.98 against the implemented +30.64. Not the ICE Dollar Index contract
    either. The registry does carry a ref for it, contract 098662 in the Legacy
    report, and `fbe.datasources.cot.CotSource` never queries it: it is too
    thinly held to lead and is registered as a cross-check.
    """

    name = PillarName.POSITIONING
    requires: Sequence[str] = (INDICATOR,)
    headline_component = "net_percent"

    component_indicators: Mapping[str, tuple[str, ...]] = {
        "positioning_response": (INDICATOR,),
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
            p: Net position percent z-scored against the currency's own history
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

    # No `_extract` override. `BasePillar._extract` returns one row per period
    # over the whole visible history, sorted by period ascending, which is
    # exactly what a time-series z-score reads: the base class reduces vintages
    # within each period and does not reduce the history to its newest period.
    # A copy here would carry the visibility and vintage rules a second time and
    # miss the next correction to either, which is the defect #122 exists to
    # prevent. The key carries its own normalisation, so what arrives is already
    # a percent of open interest and this pillar never sees a raw contract count.

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
            ``{currency: {"positioning_response": f, "net_percent": pct,
            "positioning_z": p, "window_reports": count}}``.
            ``positioning_response`` is the shape function's output and is the
            only component carrying sub-weight. The other three are report-only
            and `component_indicators` names none of them.

            ``net_percent`` is the latest net leveraged-funds position as a
            percent of open interest, which fills ``PillarScore.raw`` through
            `headline_component` so a report can quote a number a human
            recognises. Named for the percent it holds: it was ``net_share``
            while this pillar's docstring said the source emitted a share, and
            ADR 0011 settled that it does not. ``positioning_z`` is ``p``
            itself and ``window_reports`` is how many weekly prints the z-score
            was measured over, both so `_notes` can show its working and a
            reader can check it.

        **The floor is twelve observations, not twelve months.** An earlier
        version of this paragraph said a currency with fewer than two years of
        weekly reports returns ``None``, and that is not what happens.
        `BasePillar.time_series_z` refuses a window under
        `MIN_TIME_SERIES_WINDOW`, which is a count with no notion of the
        series' frequency, so on a weekly series it is about eleven weeks. A
        contract with twelve prints therefore scores, at full weight, and with
        ``ddof=1`` over twelve readings ``|p|`` can reach 3.17, which is inside
        the contrarian branch and close to its saturation. The freshness ramp
        does not cover this: it ages the newest print and has nothing to say
        about a short history. Issue #214 is the defect, and `window_reports`
        is on the note so the case is visible to the one person who would
        catch it.

        """
        transformed: dict[str, dict[str, float | None]] = {}
        for currency, series in extracted.items():
            # Indexed, not fetched with a default: `BasePillar._extract`
            # guarantees a key for every entry in `requires`, carrying an empty
            # sequence where the currency has nothing. A `.get` here would read
            # a mapping built some other way as a currency with no contract.
            history = series[INDICATOR]
            p = self.time_series_z(history, self.config.lookback_years, asof)
            # The same bound `time_series_z` applies internally, taken from the
            # same helper rather than restated, because a second copy of the
            # window rule would be free to disagree with the one that decides
            # the score.
            earliest = _years_earlier(asof, self.config.lookback_years)
            window = [item for item in history if earliest <= item.period <= asof]
            transformed[currency] = {
                "positioning_response": self.response(p),
                # Three report-only values, none of them a scoring component:
                # `component_indicators` names only ``positioning_response``
                # and `_normalise` reads only that. ``net_percent`` fills
                # ``PillarScore.raw``; the other two are the working `_notes`
                # shows, and ``positioning_z`` also reaches
                # ``PillarScore.diagnostics``.
                "net_percent": history[-1].value if history else None,
                "positioning_z": p,
                "window_reports": float(len(window)),
            }
        return transformed

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

        Raises:
            KeyError: If any currency's mapping has no
                ``"positioning_response"``. `_transform` always emits the key,
                carrying ``None`` where the history was too short to z-score, so
                a missing key means a caller built the mapping some other way.
                A ``.get`` here would turn that into an absent reading for all
                eight, which reads exactly like a dead feed.

        """
        return {
            currency: values["positioning_response"]
            for currency, values in components.items()
        }

    def _diagnostics(
        self,
        extracted: Mapping[str, Sequence[Observation]],
        inputs: Sequence[Observation],
        asof: date,
    ) -> dict[str, float]:
        """Add ``p`` to the base measurements, since nothing may parse the note.

        Args:
            extracted: One currency's slice of `_extract`'s output.
            inputs: The same observations flattened.
            asof: Run date.

        Returns:
            The base class's ``freshness.<component>`` and
            ``assumed_lag_inputs``, plus ``positioning_z``: how many standard
            deviations from its own mean the currency's newest print sits at.

        `fbe.types.PillarScore.notes` says any fact a consumer needs must have a
        field or a `diagnostics` key of its own, and this pillar's score cannot
        be read at all without ``p``: a score of ``+0.60`` is a crowded short
        being faded and a score of ``+0.60`` from the confirming branch is a
        moderate long, and nothing else on the score tells the two apart.
        Recomputed here rather than carried from `_transform`, because
        `_diagnostics` is handed the extracted slice and not the components,
        and both call sites reach `time_series_z` with the same window so they
        cannot disagree.

        Absent where the history could not be z-scored, which is the same rule
        `component_freshness` follows for a component with no data: a key that
        appeared with a placeholder would be read as a reading.

        """
        diagnostics = super()._diagnostics(extracted, inputs, asof)
        p = self.time_series_z(
            extracted.get(INDICATOR, ()), self.config.lookback_years, asof
        )
        if p is not None:
            diagnostics["positioning_z"] = p
        return diagnostics

    def _notes(
        self,
        currency: str,
        components: Mapping[str, float | None],
        extracted: Mapping[str, Sequence[Observation]],
    ) -> str:
        """Show the working behind a score whose sign may surprise the reader.

        Args:
            currency: The currency being scored.
            components: That currency's values from `_transform`.
            extracted: That currency's slice of `_extract`'s output, for the
                period of the print being described.

        Returns:
            One line naming the net position as a percent of open interest, the
            Tuesday it was snapped, how many standard deviations that sits from
            the currency's own mean, how many weekly prints that mean was taken
            over, and which branch of the response function it landed on. Empty
            when any of those is absent, which is the unscored case `compute`
            does not call this for.

        Two things here a reader cannot recover from the numbers. The branch,
        because a currency holding a large net long and scoring negatively is
        this pillar working as specified and the word "fading" or "contrarian"
        is what says so. And the print count, because the window is whatever
        history the contract has and not the five years the config asked for: an
        earlier version of this line said "its own 5-year mean" whatever the
        window held, which is the one part of the note a reader could not check
        and would have been wrong to trust. It is also what makes the short
        history of #214 visible on the page.

        Nothing may parse this. ``p`` has a typed home in
        ``PillarScore.diagnostics`` and the response is on ``PillarScore.z``.

        """
        p = components.get("positioning_z")
        net = components.get("net_percent")
        reports = components.get("window_reports")
        history = extracted.get(INDICATOR, ())
        if p is None or net is None or not reports or not history:
            return ""
        magnitude = abs(p)
        if magnitude <= MOMENTUM_PEAK_Z:
            branch = "momentum"
        elif magnitude <= SIGN_FLIP_Z:
            branch = "fading"
        else:
            branch = "contrarian"
        return (
            f"{currency} net leveraged funds {net:+.1f}% of open interest on "
            f"{history[-1].period.isoformat()}, {p:+.2f} standard deviations "
            f"from its own mean over {int(reports)} weekly reports, "
            f"{branch} branch"
        )
