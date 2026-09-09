"""Pair layer: differencing currency scores into a tradeable directional bias.

The engine never scores a pair. It scores the two legs and subtracts, because
there is no such thing as a strong currency, only a currency stronger than the
one it is quoted against. Everything in this module is that subtraction and the
rules that decide whether the result is worth acting on.

The output is a bias, not a trade. Entry stays with the trader and with the
trendlines and channels in the trading plan. What this module supplies is which
way to lean, how hard, and when to stand aside.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date

from fbe.config import Config, ScoringConfig
from fbe.types import CalendarEvent, Conviction, CurrencyScore, Direction, PairBias

__all__ = [
    "build_pair_biases",
    "direction_for",
    "conviction_for",
    "agreement",
    "apply_filters",
    "shortlist",
    "MIN_COVERAGE",
    "FULL_COVERAGE",
    "AGREEMENT_DEADBAND",
    "CalendarGuard",
]


MIN_COVERAGE: float = 0.60
"""Coverage floor below which a pair is not tradeable at any spread.

Under 60% of pillar weight the composite is an extrapolation from under half the
model, and the size of the spread says more about which pillars happened to have
data than about the two currencies. Belongs in `ScoringConfig` alongside
``min_agreement``; it lives here until that field exists, and the integrator
should move it.
"""

FULL_COVERAGE: float = 0.85
"""Coverage above which no conviction demotion is applied. Between this and
`MIN_COVERAGE` a pair drops one conviction step."""

AGREEMENT_DEADBAND: float = 0.10
"""Per-pillar spread inside which a pillar is counted as having no opinion.

Without a deadband a pillar that scores the two legs at 1.20 and 1.19 would be
recorded as agreeing, and the agreement figure would drift toward the fraction
of pillars that happen to have data rather than the fraction that have a view.
"""


CalendarGuard = Callable[[str, date], Sequence[str]]
"""Injected hook that reports calendar blockers for one currency on one date.

Returns zero or more blocker strings, conventionally ``"calendar:<ccy>:<event>"``.
The implementation lives in ``fbe.calendar_guard`` and is owned elsewhere. It is
injected rather than imported so the dependency runs one way: the calendar knows
about dates and events, and this module knows about scores, and neither needs to
import the other to do its job. It also lets a backtest run with the guard
switched off without stubbing a module.
"""


def build_pair_biases(
    scores: Sequence[CurrencyScore],
    config: Config,
    asof: date,
) -> Sequence[PairBias]:
    """Difference every currency score into the 28 G10 pair biases.

    Args:
        scores: One `CurrencyScore` per currency, from `scoring.score_currencies`.
        config: Full run configuration. The scoring section supplies the
            thresholds; the data section supplies the calendar blackout windows
            the guard reads.
        asof: The date the run represents.

    Returns:
        One `PairBias` per entry in ``universe.ALL_PAIRS``, in that order, with
        `spread`, `direction`, `conviction` and `agreement` populated. Filters
        are applied separately by `apply_filters`, so what comes back here is the
        model's unfiltered view and the caller can report both.

    Raises:
        KeyError: If a pair's base or quote currency is absent from ``scores``.
            Silently skipping the pair would leave a hole in the report that
            looks like an absence of opportunity rather than an absence of data.

    ``spread`` is ``composite(base) - composite(quote)``, in score-band units, so
    a spread of ``1.5`` means the base currency's fundamentals sit one and a half
    points above the quote's on a scale that runs from ``-3`` to ``+3``. Pairs are
    built in market convention, EUR/USD and never USD/EUR, because a report that
    inverts a pair inverts its bias without saying so.

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

    """
    raise NotImplementedError


def conviction_for(
    spread: float,
    agreement_fraction: float,
    coverage_fraction: float,
    config: ScoringConfig,
) -> Conviction:
    """Grade a directional call by size, breadth, and completeness.

    Args:
        spread: ``composite(base) - composite(quote)``. Only its magnitude is
            read; direction is `direction_for`'s job.
        agreement_fraction: Output of `agreement` for this pair, in ``[0, 1]``.
        coverage_fraction: The lower of the two legs' `CurrencyScore.coverage`.
            The lower rather than the mean, because a pair is only as well
            measured as its worse-measured side, and averaging would let a fully
            covered dollar hide a euro scored on three pillars.
        config: Scoring configuration supplying the three spread thresholds and
            ``min_agreement``.

    Returns:
        The conviction level, which gates position size and shortlist entry.

    Step 1, the base tier from the size of the spread, using the defaults 0.75,
    1.50 and 2.50:

        ``|spread| < 0.75``: `Conviction.NONE`. Direction is neutral here too,
        so the pair carries no view at all.

        ``0.75 <= |spread| < 1.50``: `Conviction.LOW`.

        ``1.50 <= |spread| < 2.50``: `Conviction.MEDIUM`.

        ``|spread| >= 2.50``: `Conviction.HIGH`. On a band that runs to ``+/-3``
        with weighted pillar scores that rarely reach their own extremes, this is
        the top and bottom of the currency ranking disagreeing about almost
        everything, and it should be uncommon.

    Step 2, the demotions, applied in this order to the tier from step 1:

        ``coverage_fraction < MIN_COVERAGE`` (0.60): drop to `Conviction.NONE`
        outright. Not a demotion but a veto: too much of the model is missing for
        the spread to mean anything.

        ``MIN_COVERAGE <= coverage_fraction < FULL_COVERAGE`` (0.85): demote one
        step, HIGH to MEDIUM, MEDIUM to LOW, LOW to NONE.

        ``agreement_fraction < config.min_agreement`` (0.60): cap at
        `Conviction.LOW`, whatever the spread. A large spread the pillars are
        arguing about is one pillar's opinion, and `agreement` is where the
        reasoning for that rule lives.

    The two demotions compose, so a pair at ``|spread| = 2.8`` with coverage 0.70
    and agreement 0.50 lands at LOW: HIGH, demoted once for coverage to MEDIUM,
    then capped by agreement to LOW. Conviction never rises, only falls, which is
    the point: every input to this function is a reason to doubt the spread, and
    none of them is a reason to believe it more than the spread already says.

    """
    raise NotImplementedError


def agreement(base_score: CurrencyScore, quote_score: CurrencyScore) -> float:
    """Return the fraction of pillars pointing the way the headline does.

    Args:
        base_score: The base currency's aggregate score, with its pillars.
        quote_score: The quote currency's aggregate score, with its pillars.

    Returns:
        A value in ``[0.0, 1.0]``. ``0.0`` when no pillar is usable on both legs,
        which pairs with a coverage figure low enough to veto the trade anyway.

    Method. For every pillar usable on both legs, take the per-pillar spread
    ``base.score - quote.score``. A pillar agrees when the sign of its own spread
    matches the sign of the composite spread. The denominator is the count of
    pillars usable on both legs; the numerator is the count that agree. Pillars
    whose own spread falls inside `AGREEMENT_DEADBAND` count in the denominator
    but not the numerator: they have no view, and a pillar with no view is
    evidence of thinness, not of agreement.

    Unweighted, for the same reason `scoring.dispersion` is unweighted. The
    question is how many independent channels point the same way, and each
    channel gets one vote regardless of how much of the composite it carries.

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
    events: Sequence[CalendarEvent] = (),
) -> PairBias:
    """Apply the hard filters and record why a pair is not tradeable.

    Args:
        bias: The unfiltered bias from `build_pair_biases`.
        scores: All currency scores for the run, keyed by ISO code, so the filter
            can read each leg's coverage and staleness.
        config: Full run configuration.
        asof: The date the run represents.
        calendar_guard: Optional `CalendarGuard`. When ``None`` the calendar
            check is skipped and ``"calendar:unchecked"`` is added to
            ``blockers`` without setting ``tradeable`` to ``False``. Silence and
            an all-clear must not look the same to the trader.
        events: Calendar events already loaded for the run, passed through to the
            guard so it does not re-fetch.

    Returns:
        A new `PairBias` with ``tradeable`` and ``blockers`` set. Frozen input,
        new object out. Blockers accumulate rather than short-circuiting, because
        a pair blocked for three reasons is a different thing from a pair blocked
        for one, and the trader should see all three.

    The filters, each of which sets ``tradeable = False`` unless noted:

        ``no_edge``: direction is `Direction.NEUTRAL` or conviction is
        `Conviction.NONE`. Nothing to act on.

        ``spread_cost``: the pair is not in ``universe.MAJORS`` and conviction is
        below `Conviction.MEDIUM`. The trading plan calls for pairs with low
        spreads and trading costs, and the dollar pairs are the only G10 set that
        reliably clears that bar on a retail account. A cross such as GBP/NZD can
        cost several times a dollar pair's spread, and on a R2,000 account risking
        R20 to R40 a trade, that cost is a real share of the expected edge. So a
        cross has to bring more fundamental separation than a major to be worth
        paying for. This is a cost filter, not a view: the bias stands, it is just
        not worth the ticket.

        ``low_coverage:<ccy>``: either leg's coverage is below `MIN_COVERAGE`.
        The currency is named so the trader knows which side is thin.

        ``stale:<ccy>``: either leg's newest input across all pillars is older
        than ``ScoringConfig.max_staleness_days``. This usually means a source
        outage rather than a quiet calendar, and it should be investigated rather
        than traded around.

        ``calendar:<ccy>:<event>``: whatever `CalendarGuard` returns for either
        leg. High-impact releases inside the blackout windows in `DataConfig` are
        the plan's own rule, and note that a pair has two legs, so an FOMC evening
        blocks every dollar pair and not only the one the trader was watching.

        ``calendar:unchecked``: no guard was injected. Recorded, but does not
        block, so a backtest or an offline run still produces biases.

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
    on the day the dollar goes the wrong way. ``RiskConfig.max_correlated_exposure``
    caps combined risk across positions sharing a leg at 4% for exactly this
    reason, and enforcing the constraint at the shortlist is cheaper than
    enforcing it at the sizing stage, where the trader has already committed to
    the idea.

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
