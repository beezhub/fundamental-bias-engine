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

from fbe.datasources.registry import GLOBAL
from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName
from fbe.universe import meta

__all__ = [
    "RiskPillar",
    "DRAWDOWN_WINDOW_SESSIONS",
    "MIN_DRAWDOWN_WINDOW_SESSIONS",
    "DRAWDOWN_SCALE_PCT",
    "VOL_SCALE_Z",
    "RISK_SCALE",
]


DRAWDOWN_WINDOW_SESSIONS: int = 252
"""Trailing sessions defining the 52-week high the drawdown is measured from."""

MIN_DRAWDOWN_WINDOW_SESSIONS: int = 12
"""Fewest observations `_drawdown_pct` will measure a drawdown from.

Its own literal, deliberately. It used to alias the single count that
`BasePillar.time_series_z` applied to every cadence, and when #214 made that
floor frequency-aware the alias would have dragged this minimum from twelve
sessions to 252 as a side effect of a change about z-scores. The two answer
different questions. The z-score floor asks whether a standard deviation
computed from this window means anything. This asks how much of a 252-session
window must be present before a fall from its high is worth reporting at all.
Whether twelve sessions is the right answer to the second question is open and
was explicitly not ruled on with #214; what is settled is that it does not move
because the first answer did.

It is not a claim that a window this short is a 52-week high. It is not. A short
window finds a lower high than the real one and so reports a smaller fall, and
that error is one-sided: it always reads calmer than the market is. What the
floor refuses is the degenerate end of it, where a one-observation series is its
own high and the function returns ``0.0``, meaning "at the 52-week high", from a
single number. Between this floor and `DRAWDOWN_WINDOW_SESSIONS` the reading is
real but understated, and a series that short is a cold cache rather than a live
feed: the registered ref serves years of daily closes in one request.
"""

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
scores ``2.0 * -1.0 * 0.9 = -1.8``, which is the same order of magnitude as the
clip band the z-scored pillars are bounded to. That is a comparison of two ranges
and nothing more: what either pillar produces on real data has not been measured.
Without it this pillar would be systematically quieter than the other six and
would carry less than the 0.10 it is configured for.
"""


class RiskPillar(BasePillar):
    """Read one global risk regime and score each currency by its beta to it.

    Constructing the regime. Two global inputs, neither of them currency
    specific, are combined into a single number ``R`` shared by all eight:

        ``dd_component = clip(equity_drawdown_pct / DRAWDOWN_SCALE_PCT, 1.0)``,
        upper bounded at ``0.0``, where ``equity_drawdown_pct`` is the current
        drawdown of a broad equity index from its 52-week high, as a negative
        percent.

        ``vol_component = clip(-vol_z / VOL_SCALE_Z, 1.0)``, where ``vol_z`` is the
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

        ``score(c) = clip(RISK_SCALE * R * risk_beta(c), score_clip)``

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

    Known failure modes: the proxies are American. ``world_equity_index`` and
    ``vol_index`` are a broad US index and its implied volatility, so what this
    pillar measures is US risk appetite, and a European or Japanese shock only
    registers once it crosses the Atlantic. The substitution is deliberate and
    recorded on the key rather than hidden in a ticker: no free daily world
    index could be verified, ``SP500`` is the stand-in, and
    `fbe.datasources.registry.WORLD_EQUITY_INDEX` says so in its own
    description so that replacing it later changes nothing here. The
    per-currency ``equity_index`` is not the missing world index and is
    deliberately not read: eight local indices give eight regimes, which leaves
    the betas with nothing to act on. It also fires late, because an equity
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
    requires: Sequence[str] = ("world_equity_index", "vol_index")
    headline_component = "regime"

    component_indicators: Mapping[str, tuple[str, ...]] = {
        "risk_response": ("world_equity_index", "vol_index"),
    }
    """The regime reading is built from both market series, so it is as stale as
    the later of the two. Both are daily, so in practice this checks that
    neither feed has stopped rather than asking a question about cadence.
    """

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

        The volatility component's own ``min(1.0, ...)`` is defensive and has no
        observable effect. Whenever the unclamped value would exceed ``1.0``, the
        drawdown component's floor of ``-1.0`` already forces the average above
        zero, and the combined bound truncates it to ``0.0`` either way. It is
        recorded here because a reader who finds it will otherwise try to write a
        test that distinguishes the two, and no such test exists.

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

        All eight keys are the same mapping object rather than eight copies.
        Nothing downstream writes to what `_extract` returns, which is the base
        class's contract and not a local convenience, so copying would buy
        nothing and would hide a mutation that is a defect wherever it happens.

        The equity series needs `DRAWDOWN_WINDOW_SESSIONS` of history to find the
        52-week high, and the volatility series needs
        ``ScoringConfig.lookback_years`` for its z-score.

        """
        # The shared loop keys on the observation's own currency, which for
        # these two series is `GLOBAL`, so it would file them under a currency
        # nobody asked to score and leave all eight empty. Asking it about
        # `GLOBAL` and then repeating the answer is the whole of the override:
        # the visibility and vintage rules still come from the base class, and
        # this pillar does not restate them.
        routed = super()._extract(observations, (GLOBAL,), asof)
        globals_only = routed[GLOBAL]
        return {currency: globals_only for currency in currencies}

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
        absent: dict[str, float | None] = {
            "risk_response": None,
            "regime": None,
            "drawdown_pct": None,
            "vol_z": None,
        }
        if not extracted:
            return {}

        # Every currency holds the same two series, so the regime is read once
        # from whichever slice comes to hand rather than eight times.
        any_slice = next(iter(extracted.values()))
        # Indexed, not fetched with a default, for the reason `_normalise`
        # gives eight lines further down. `_extract` seeds a key for every
        # entry in `requires`, so a missing one means the mapping was built
        # some other way, and an empty tuple in its place would come back as a
        # market sitting at its 52-week high with volatility unreadable: a dead
        # feed reported as half a calm market.
        drawdown = _drawdown_pct(any_slice["world_equity_index"])
        vol_z = self.time_series_z(
            any_slice["vol_index"], self.config.lookback_years, asof
        )
        if drawdown is None or vol_z is None:
            # Not half a reading. The regime is defined as both series, so one
            # of them missing leaves it unmeasurable, and an unmeasurable
            # regime is unmeasurable for everybody: all eight lose the pillar
            # together rather than eight different partial answers.
            return {currency: dict(absent) for currency in extracted}

        regime = self.regime(drawdown, vol_z)
        return {
            currency: {
                # The trailing ``+ 0.0`` exists only to turn a negative zero
                # back into a zero. At ``R = 0`` a negative beta produces
                # ``-0.0``, which compares equal to ``0.0`` and renders as
                # ``-0.00``, and a reader seeing the yen at ``-0.00`` in a calm
                # market would read a small short this pillar is not taking.
                "risk_response": RISK_SCALE * regime * meta(currency).risk_beta + 0.0,
                "regime": regime,
                # Both halves of the regime, carried so `_notes` can show its
                # working. Neither is a scoring component: `component_indicators`
                # names only ``risk_response`` and `_normalise` reads only that.
                "drawdown_pct": drawdown,
                "vol_z": vol_z,
            }
            for currency in extracted
        }

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

        Raises:
            KeyError: If any currency's mapping has no ``"risk_response"``.
                `_transform` always emits the key, so this is deliberate and is
                explained in the comment below: swallowing it would report a
                mapping built the wrong way as a universe-wide data outage.

        """
        # Indexed, not fetched with a default. `_transform` always emits the
        # key, carrying ``None`` when the regime could not be read, so a
        # missing key means a caller built the mapping some other way. A
        # ``.get`` here turns that into an absent regime for all eight, which
        # reads exactly like a dead feed and is the quiet answer this codebase
        # refuses everywhere else.
        return {
            currency: values["risk_response"] for currency, values in components.items()
        }

    def _notes(
        self,
        currency: str,
        components: Mapping[str, float | None],
        extracted: Mapping[str, Sequence[Observation]],
    ) -> str:
        """Name both halves of the regime and the beta that re-signed them.

        Args:
            currency: The currency this note belongs to.
            components: That currency's row from `_transform`.
            extracted: That currency's observations. Unused: every number the
                note needs is already on ``components``, and recomputing from
                the series here could disagree with the score beside it.

        Returns:
            One line giving the drawdown in percent, the volatility z-score,
            the regime the two combine into and this currency's ``risk_beta``.
            Empty when any of the three it reads from ``components`` is absent.
            The beta is not one of them: `CurrencyMeta` holds it for every
            currency in the universe, so it cannot go missing here.

        Why this pillar overrides the hook when most do not. Its sign comes
        from the run rather than from the indicator, so a reader who sees the
        yen positive and the Australian dollar negative cannot tell from the
        scores alone whether the regime was read correctly or the betas were
        applied backwards. The note is what makes that checkable by hand. The
        class docstring's tell for the rates-led selloff, disagreement with the
        other pillars, also needs the regime visible before anyone can act on
        it.

        Prose only. `fbe.types.PillarScore.notes` forbids anything parsing this
        for a decision, so these numbers are for a person reading the report and
        nothing else may act on them. ``R`` itself has a typed home on
        `PillarScore.raw`; its two halves have none, which is why they are here
        rather than only here.

        """
        # `.get` rather than indexing, unlike `_normalise`. `compute` passes
        # ``components.get(currency, {})``, so an empty mapping arrives here by
        # design rather than by defect, and the answer to a missing number is
        # an empty note: an absence stated, not a number invented.
        drawdown = components.get("drawdown_pct")
        vol_z = components.get("vol_z")
        regime = components.get("regime")
        if drawdown is None or vol_z is None or regime is None:
            return ""
        return (
            f"drawdown {drawdown:+.1f}%, vol z {vol_z:+.2f}, "
            f"regime {regime:+.2f}, beta {meta(currency).risk_beta:+.2f}"
        )


def _drawdown_pct(series: Sequence[Observation]) -> float | None:
    """Return the current fall from the trailing window's high, as a percent.

    Args:
        series: The global equity series, oldest period first, as `_extract`
            leaves it.

    Returns:
        ``(latest - high) / high * 100`` over the last
        `DRAWDOWN_WINDOW_SESSIONS` observations, so a 6.5% fall is ``-6.5`` and
        a market at its own high is ``0.0``. ``None`` when the series holds
        fewer than `MIN_DRAWDOWN_WINDOW_SESSIONS` observations, or when its
        window high is not positive.

        Never ``0.0`` for an absent series. A zero here means a market sitting
        at its 52-week high, which is the calmest reading this pillar has and a
        finding in its own right, so conflating it with no data would turn a
        dead feed into a confident all-clear.

        The window is trailing observations rather than trailing calendar days,
        which is what `DRAWDOWN_WINDOW_SESSIONS` names: a year of sessions is
        about 252 of them, and counting days instead would shorten the window
        by every weekend and holiday in it.

        A non-positive high is refused rather than divided by. An index cannot
        be zero or negative, so reaching that means the series is not what the
        caller thinks it is, and returning a number built from it would be a
        drawdown computed against nonsense.

        A series shorter than the floor is refused for the same reason stated
        one step further along: with one observation the high is the latest
        close, the fall is ``0.0``, and the calmest reading this pillar can
        produce would come out of a feed that had sent almost nothing. See
        `MIN_DRAWDOWN_WINDOW_SESSIONS` for why the floor is where it is and
        what it does not claim.

    """
    if len(series) < MIN_DRAWDOWN_WINDOW_SESSIONS:
        return None
    window = series[-DRAWDOWN_WINDOW_SESSIONS:]
    high = max(entry.value for entry in window)
    if high <= 0.0:
        return None
    return (window[-1].value - high) / high * 100.0
