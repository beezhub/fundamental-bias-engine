"""Pair layer: differencing currency scores into a tradeable directional bias.

The engine never scores a pair. It scores the two legs and subtracts, because
there is no such thing as a strong currency, only a currency stronger than the
one it is quoted against. Everything in this module is that subtraction and the
rules that decide whether the result is worth acting on.

Two questions are kept apart throughout. Conviction is a statement about how much
the model believes its own view. Tradeability is a statement about whether that
view can be executed sensibly. A pair can be HIGH conviction and untradeable, and
the report shows both, because a blocked pair that was right is the most useful
thing a later review can look at.

The output is a bias, not a trade. Entry stays with the trader and with the
trendlines and channels in the trading plan. What this module supplies is which
way to lean, how hard, and when to stand aside.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date

from fbe.config import Config, ScoringConfig
from fbe.types import Conviction, CurrencyScore, Direction, PairBias

__all__ = [
    "build_pair_biases",
    "direction_for",
    "conviction_for",
    "agreement",
    "apply_filters",
    "shortlist",
    "AGREEMENT_TIE_EPSILON",
    "CalendarGuard",
    "EventHorizonGuard",
]


AGREEMENT_TIE_EPSILON: float = 1e-9
"""Below this, two legs are treated as having scored a pillar identically.

A float-equality guard rather than a modelling threshold. A pillar that scores
the two legs the same has no opinion on the pair, and it is excluded from both
sides of the agreement fraction rather than counted as agreeing.
"""

CalendarGuard = Callable[[str, date], Sequence[str]]
"""Injected hook reporting hard calendar blockers for one currency on one date.

Returns zero or more blocker strings for high-impact releases inside the
execution blackout window, which is ``DataConfig.calendar_blackout_before_min``
(30) and ``calendar_blackout_after_min`` (60) either side of the release. Inside
that window there is no order, full stop.
"""

EventHorizonGuard = Callable[[str, date], bool]
"""Injected hook answering whether a high-impact event is due within 24 hours.

Separate from `CalendarGuard` because the two questions have different answers
and different consequences. The 30/60 minute window is about execution and is a
hard block. The 24-hour window is about whether the model has seen the
information the position will be held through, and is a conviction cap: a pair
with a central bank decision tomorrow can still be traded, but the engine refuses
to call it better than LOW.

Both hooks are injected rather than imported. The implementation lives in
``fbe.calendar_guard``, and the dependency deliberately runs one way: the
calendar module knows about dates and events, this module knows about scores, and
neither needs to import the other. Keeping the window definitions and the keyword
matching that catches mislabelled events on the calendar side is the point, and
injection also lets a backtest run with the guards switched off without stubbing
a module.
"""


def build_pair_biases(
    scores: Sequence[CurrencyScore],
    config: Config,
    asof: date,
    event_horizon_guard: EventHorizonGuard | None = None,
) -> Sequence[PairBias]:
    """Difference every currency score into the 28 G10 pair biases.

    Args:
        scores: One `CurrencyScore` per currency, from `scoring.score_currencies`.
        config: Full run configuration. The scoring section supplies the
            thresholds; the data section supplies the blackout windows the guards
            read.
        asof: The date the run represents.
        event_horizon_guard: Optional `EventHorizonGuard` used for the 24-hour
            conviction cap. When ``None`` the cap is not applied and no event is
            assumed, which is the right default for a backtest and the wrong one
            for a live run, so the caller should pass it.

    Returns:
        One `PairBias` per entry in ``universe.ALL_PAIRS``, in that order, with
        `spread`, `direction`, `conviction` and `agreement` populated. Hard
        filters are applied separately by `apply_filters`, so what comes back
        here is the model's unfiltered view.

    Raises:
        KeyError: If a pair's base or quote currency is absent from ``scores``.
            Silently skipping the pair would leave a hole in the report that
            looks like an absence of opportunity rather than an absence of data.

    ``spread`` is ``composite(base) - composite(quote)``, in score-band units, so
    a spread of ``1.5`` means the base currency's fundamentals sit one and a half
    points above the quote's on a scale that runs from ``-3`` to ``+3``. Pairs are
    built in market convention, EUR/USD and never USD/EUR, because a report that
    inverts a pair inverts its bias without saying so.

    Direction and conviction must never disagree. If `conviction_for` returns
    `Conviction.NONE`, ``direction`` is forced to `Direction.NEUTRAL` regardless
    of the spread. A pair the model will not back at any size does not have a
    direction worth printing.

    A note on what the spread is not. It is not a forecast of how far the pair
    moves, and it carries no units the trader can size against. It is an ordering
    statement with a gap attached, which is why `conviction_for` maps it onto four
    discrete levels instead of passing the number through.

    """
    raise NotImplementedError


def direction_for(spread: float, config: ScoringConfig) -> Direction:
    """Turn a score spread into a directional call on the base currency.

    Args:
        spread: ``composite(base) - composite(quote)``.
        config: Scoring configuration supplying ``min_spread_low``.

    Returns:
        The direction, expressed on the base currency as `PairBias` requires.

    The table, with the default ``min_spread_low`` of 0.75:

        ``spread >= +0.75``: `Direction.LONG`. The base currency is the stronger
        leg by enough to matter.

        ``spread <= -0.75``: `Direction.SHORT`.

        otherwise: `Direction.NEUTRAL`.

    The neutral band is wide on purpose. Two currencies inside 0.75 points of
    each other on a seven-pillar composite are, as far as this model can tell,
    the same currency: that gap is comfortably inside the measurement error of
    z-scores built on revised macro data across an eight-name cross-section. The
    plan the engine serves says to be selective and to avoid low-certainty
    trades, and roughly half of the 28 pairs sitting in the neutral band on a
    typical run is that instruction expressed in arithmetic.

    `build_pair_biases` may override the result to `Direction.NEUTRAL` when
    conviction lands at `Conviction.NONE`. This function only reads the spread.

    """
    raise NotImplementedError


def conviction_for(
    spread: float,
    agreement_fraction: float,
    coverage_fraction: float,
    dispersion_value: float,
    event_within_24h: bool,
    config: ScoringConfig,
) -> Conviction:
    """Grade a directional call by size, breadth, completeness and timing.

    Args:
        spread: ``composite(base) - composite(quote)``. Only its magnitude is
            read; direction is `direction_for`'s job.
        agreement_fraction: Output of `agreement` for this pair, in ``[0, 1]``.
        coverage_fraction: The lower of the two legs' `CurrencyScore.coverage`.
            The lower rather than the mean, because a pair is only as well
            measured as its worse-measured side, and averaging would let a fully
            covered dollar hide a euro scored on three pillars.
        dispersion_value: The higher of the two legs' `CurrencyScore.dispersion`,
            for the mirror-image reason: one leg whose own pillars contradict
            each other is enough to make the spread an average of arguments.
        event_within_24h: True when a high-impact event is due on either leg
            within 24 hours, from `EventHorizonGuard`.
        config: Scoring configuration supplying the three spread thresholds,
            ``min_agreement``, ``coverage_demotion`` and ``max_dispersion``.

    Returns:
        The conviction level, which gates position size and shortlist entry.

    Step 1, the base tier from the size of the spread, using the defaults 0.75,
    1.50 and 2.50:

        ``|spread| < 0.75``: `Conviction.NONE`. Direction is neutral here too, so
        the pair carries no view at all.

        ``0.75 <= |spread| < 1.50``: `Conviction.LOW`.

        ``1.50 <= |spread| < 2.50``: `Conviction.MEDIUM`.

        ``|spread| >= 2.50``: `Conviction.HIGH`. On a band that runs to ``+/-3``
        with weighted pillar scores that rarely reach their own extremes, this is
        the top and bottom of the currency ranking disagreeing about almost
        everything, and it should be uncommon.

    Step 2, the demotions, applied in this order down the ladder
    ``HIGH -> MEDIUM -> LOW -> NONE``:

        ``agreement_fraction < config.min_agreement`` (0.60): cap at
        `Conviction.LOW`. One pillar is carrying the whole spread, and
        `agreement` is where the reasoning for that rule lives.

        ``coverage_fraction < config.coverage_demotion`` (0.80): demote one step.
        The view rests on partial data. The threshold is calibrated to the weight
        vector: missing a 0.15 pillar leaves coverage at 0.85 and does not demote,
        while missing the 0.30 monetary pillar leaves 0.70 and does.

        ``dispersion_value > config.max_dispersion`` (1.20): demote one step. A
        leg's own pillars contradict each other, so the composite in the middle
        is an average of two real views rather than a view of its own.

        ``event_within_24h``: cap at `Conviction.LOW`. A position opened today
        would be held through a repricing the model has not seen, and the rate
        path is exactly what the heaviest pillar is measuring.

    Demotions compound, and conviction never rises. A pair at ``|spread| = 2.8``
    with coverage 0.70, dispersion 1.4 and weak agreement falls from HIGH to
    NONE: the two one-step demotions take it to LOW and the agreement cap holds
    it there, or reaches it by another route to the same place. Every input to
    this function is a reason to doubt the spread, and none of them is a reason
    to believe it more than the spread already says.

    Note that ``config.min_coverage`` (0.60) does not appear here. Coverage below
    that floor is a hard filter in `apply_filters`, not a demotion, because at
    that point the question is no longer how much to believe the number but
    whether there is a number at all.

    """
    raise NotImplementedError


def agreement(base_leg: CurrencyScore, quote_leg: CurrencyScore) -> float:
    """Return the share of pillar weight pointing the way the headline does.

    Args:
        base_leg: The base currency's aggregate score, with its pillars. Named
            for the leg rather than for the score to keep it distinct from
            ``PairBias.base_score``, which is a bare float.
        quote_leg: The quote currency's aggregate score, with its pillars.

    Returns:
        A value in ``[0.0, 1.0]``. ``0.0`` when no pillar is considered, which
        pairs with a coverage figure low enough to block the trade anyway.

    The arithmetic, matching section 5.3 of ``docs/scoring-spec.md``:

        ``d(p)       = score(base, p) - score(quote, p)``

        ``w_pair(p)  = ( w_eff(base, p) + w_eff(quote, p) ) / 2``

        ``considered = pillars where |d(p)| > 1e-9``

        ``agreeing   = considered pillars where sign(d(p)) == sign(spread)``

        ``agreement  = sum of w_pair over agreeing / sum of w_pair over considered``

    Pillars where the two legs score identically are excluded from both numerator
    and denominator. They express no opinion on this pair and should neither
    support nor oppose it; leaving them in the denominator would let a thin run
    look like a disputed one.

    Weighted by ``w_pair`` rather than counting pillars. The monetary pillar at
    0.30 disagreeing is a materially worse sign than positioning at 0.10
    disagreeing, and a headcount would treat them as equal. Using the mean of the
    two legs' effective weights also means a pillar that is stale or missing on
    one side counts for less on this pair than one that is fresh on both, which
    is the correct treatment: agreement should measure the evidence that exists,
    not the slots on the form.

    Why seven pillars agreeing at a spread of 1.0 is a better trade than two
    disagreeing at a spread of 2.0. The wide spread is an average, and averages
    hide their construction. If it came from the monetary pillar alone with
    growth and external pulling the other way, then the trade is a single bet on
    a single mechanism measured through a single data vintage, and one revision,
    one meeting, or one soft print unwinds it. Worse, a spread that large from one
    pillar usually means that pillar sat near its clipping band, which is exactly
    where a z-score is least trustworthy, because it is the region populated by
    outliers and data errors rather than by information.

    Seven pillars agreeing at 1.0 is a different object. Rates, inflation, growth,
    labour, external balances, positioning and the risk regime are not
    independent, but they are far from collinear, and they fail for different
    reasons at different times. When all of them lean the same way, no single
    input can reverse the call, and the errors that would have to line up to make
    the call wrong are errors in unrelated data sets. That is a smaller edge held
    with more conviction, which on a small account with 1-2% risk per trade is the
    only kind worth taking: survival comes from the hit rate, not from the size of
    the occasional outlier.

    This is why agreement caps conviction rather than adding to it. A broad,
    modest signal reaches MEDIUM on its spread alone and stays there; a narrow,
    dramatic one gets pulled back down to LOW.

    """
    raise NotImplementedError


def apply_filters(
    bias: PairBias,
    scores: Mapping[str, CurrencyScore],
    config: Config,
    asof: date,
    calendar_guard: CalendarGuard | None = None,
    cost_ratio: float | None = None,
) -> PairBias:
    """Apply the hard filters and record why a pair is not tradeable.

    Args:
        bias: The unfiltered bias from `build_pair_biases`.
        scores: All currency scores for the run, keyed by ISO code, so the filter
            can read each leg's coverage.
        config: Full run configuration.
        asof: The date the run represents.
        calendar_guard: Optional `CalendarGuard` for the execution blackout
            window. When ``None`` the check is skipped and ``"event:unchecked"``
            is appended to ``blockers`` without setting ``tradeable`` to
            ``False``. Silence and an all-clear must not look the same.
        cost_ratio: Round-trip dealing cost as a share of the expected move over
            the bias horizon, supplied by the execution layer, which owns the ATR
            and the broker's spread table. ``None`` records
            ``"cost:unchecked"`` without blocking.

    Returns:
        A new `PairBias` with ``tradeable`` and ``blockers`` set. Frozen input,
        new object out. Blockers accumulate rather than short-circuiting, because
        a pair blocked for three reasons is a different thing from a pair blocked
        for one, and the trader should see all three.

    The filters, matching section 6 of ``docs/scoring-spec.md``. Each sets
    ``tradeable = False`` unless noted:

        ``no_edge``: direction is `Direction.NEUTRAL` or conviction is
        `Conviction.NONE`. Nothing to act on.

        ``cost``: ``cost_ratio > config.max_cost_ratio`` (0.05). The expected move
        is ``atr_20d_pips * sqrt(config.horizon_days)``, the standard random-walk
        approximation, which is adequate here because the filter only has to
        separate viable pairs from obviously uneconomic ones. Above 5% the broker
        takes more than a twentieth of the plausible move before the position
        starts, which on a R2,000 account risking R20 to R40 a trade is decisive.
        This is the mechanism by which the plan's "low spreads and trading costs"
        rule enters the model, and it is why wide G10 crosses usually fail even
        when the score spread is attractive. It is a cost test, not a view: the
        bias stands, it is just not worth the ticket.

        ``coverage``: ``min(coverage_base, coverage_quote) < config.min_coverage``
        (0.60). Under 60% of pillar weight the composite is a guess, and the size
        of the spread says more about which pillars happened to have data than
        about the two currencies.

        ``no_coverage``: either leg has ``coverage == 0.0``. No composite exists.

        ``event``: whatever `CalendarGuard` returns for either leg, for the
        execution blackout window only. A pair has two legs, so an FOMC evening
        blocks every dollar pair and not only the one the trader was watching.
        The wider 24-hour test is not here: it is a conviction cap in
        `conviction_for`, and the two are kept apart deliberately.

        ``event:unchecked`` and ``cost:unchecked``: recorded when the
        corresponding input was not supplied, and do not block, so an offline run
        still produces biases.

    What this function does not do. It never changes ``direction``, ``spread`` or
    ``conviction``. The model's view and the tradeability of that view are
    separate facts, and a report that quietly neutralised a blocked pair would
    lose the record of what the engine actually thought, which is the only thing
    a later review can learn from.

    """
    raise NotImplementedError


def shortlist(biases: Sequence[PairBias], limit: int = 3) -> Sequence[PairBias]:
    """Pick the handful of pairs worth putting on a chart today.

    Args:
        biases: Filtered biases from `apply_filters`, normally all 28.
        limit: How many pairs to return. Defaults to 3, matching
            ``RiskConfig.max_concurrent_positions``: there is no purpose in
            shortlisting more trades than the risk rules permit to be open.

    Returns:
        Up to ``limit`` tradeable biases, best first. Ordered by conviction
        first and by absolute spread within a conviction level, with ties broken
        by pair name so two identical runs produce identical shortlists.

    Selection rules, in order:

        1. Drop anything with ``tradeable`` false or conviction `Conviction.NONE`.
        2. Sort by conviction, then by ``abs(spread)``.
        3. Walk the sorted list and skip any pair sharing a currency with one
           already taken.
        4. Stop at ``limit``.

    Why the extremes of the ranking and not the middle. The composite is an
    ordering statement, and an ordering is most reliable at its ends. The gap
    between the first and second ranked currencies is usually the same size as
    the measurement error in either one, but the gap between first and eighth is
    several times it, so the pairs that survive as high-conviction are the ones
    where the model's uncertainty is small next to the separation it is claiming.
    The middle of the table is where the model knows the least and is most likely
    to reorder itself on the next data release. The same arithmetic is what makes
    the ends attractive commercially: pairing rank 1 against rank 8 maximises the
    fundamental separation obtained per unit of spread cost paid, and on a small
    account the cost side of that ratio is not a rounding error.

    Why two pairs must not share a leg. Long AUD/USD and short USD/JPY is one
    short dollar position wearing two tickets. The dollar leg is common, its
    moves arrive in both positions on the same tick, and the diversification the
    second ticket appears to buy is an illusion that shows up as a doubled loss
    on the day the dollar goes the wrong way.
    ``RiskConfig.max_correlated_exposure`` caps combined risk across positions
    sharing a leg at 4% for exactly this reason, and enforcing the constraint at
    the shortlist is cheaper than enforcing it at the sizing stage, where the
    trader has already committed to the idea.

    The rule is deliberately strict: any shared currency, in either position,
    disqualifies. It is stricter than correlation alone would demand, since
    EUR/USD and USD/JPY are less correlated than EUR/USD and GBP/USD, but the
    simple version is the one that gets followed under pressure, and a shortlist
    of three genuinely independent ideas is worth more than five overlapping ones.
    A consequence to expect: on a run where the dollar is the strongest or weakest
    currency, only one dollar pair reaches the shortlist even though seven of them
    look attractive, and the second and third slots go to crosses. That is the
    rule working, not failing.

    """
    raise NotImplementedError
