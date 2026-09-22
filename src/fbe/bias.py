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
from dataclasses import replace
from datetime import date
from math import isfinite
from typing import Literal

from fbe.config import Config, ScoringConfig
from fbe.types import Conviction, CurrencyScore, Direction, PairBias
from fbe.universe import ALL_PAIRS, split_pair

__all__ = [
    "build_pair_biases",
    "direction_for",
    "conviction_for",
    "agreement",
    "apply_filters",
    "shortlist",
    "AGREEMENT_TIE_EPSILON",
    "BLOCKERS",
    "at_least",
    "blocking",
    "UNCHECKED_SUFFIX",
    "UNKNOWN_SUFFIX",
    "CalendarGuard",
    "EventHorizonGuard",
]


UNCHECKED_SUFFIX: str = ":unchecked"
"""Suffix marking a blocker that records a check which did not run at all.

A naming convention rather than a list, so a marker for a different kind of
absence can be added by naming it rather than by editing every consumer.
Anything carrying this suffix is a statement about the engine's knowledge, not
about the pair. Paired with `UNKNOWN_SUFFIX` for the other half of that
statement: no guard supplied, versus a guard supplied that could not tell.
"""

UNKNOWN_SUFFIX: str = ":unknown"
"""Suffix marking a blocker that records a check which ran and could not tell.

`UNCHECKED_SUFFIX` was already documented, before this suffix existed, as "a
naming convention rather than a list, so the marker for a check that ran and
failed... can be added by naming it rather than editing every consumer"
(ADR 0002 rule 4). This is that marker. It is a second suffix and not a reuse
of `UNCHECKED_SUFFIX`, because the two failures call for different responses:
no guard supplied is a configuration choice, usually a backtest or an offline
run, and nothing is wrong. A guard supplied that could not check, which is
`calendar_guard.is_blacked_out` reporting `(None, reason)` because the
calendar fetch failed or the cached week does not reach the date in question,
is not a choice, and a trader reading the report needs to be able to tell
which one happened from the marker's name alone.
"""

BLOCKERS: Mapping[str, bool] = {
    "no_edge": True,
    "cost": True,
    "coverage": True,
    "no_coverage": True,
    "event": True,
    "cost" + UNCHECKED_SUFFIX: False,
    "event" + UNCHECKED_SUFFIX: False,
    "event" + UNKNOWN_SUFFIX: False,
}
"""Every kind of blocker `apply_filters` can append, and whether it blocks.

``True`` means the blocker sets ``PairBias.tradeable = False``. ``False`` means
it is recorded and the pair stays tradeable, which is both the
`UNCHECKED_SUFFIX` and `UNKNOWN_SUFFIX` cases: an offline run, and a run whose
calendar fetch failed, both still produce biases, and both say which checks
they could not perform or complete. Whether unknown calendar coverage should
keep this pair tradeable at all is not settled: `apply_filters` marks this
provisional pending issue #24 and the child of #41 that consumes its ruling.

Kinds rather than literal strings, and the distinction matters for two entries.
Six of these eight are emitted as the key itself. ``event`` is not: `CalendarGuard`
returns a reason naming the event, its currency and its scheduled time, so the
shortlist can say why an obvious setup was skipped, and `apply_filters` appends
that reason as ``"event: <reason>"``. ``event:unknown`` is not either: it carries
why the guard could not check, as ``"event:unknown: <reason>"``, from
`calendar_guard.CoverageGap`'s categories. So a run's ``blockers`` can hold
strings this mapping does not contain verbatim, and a consumer matching on
exact equality will miss both the blocked case and the unknown case.

A consumer mapping a string back to its kind must take the **longest** key that
prefixes it. Four of the emitted strings begin with a shorter key than their
own: ``"cost:unchecked"`` starts with ``"cost"``, and ``"event:unchecked"``,
``"event:unknown: ..."`` and ``"event: ..."`` all start with ``"event"``. Taking
the first key that matches reads three non-blocking markers as hard blocks and
refuses every pair in an offline run, which is the outcome the two suffixes
exist to avoid. `apply_filters` sidesteps the question by carrying each kind
alongside the string it emits rather than parsing it back.

`apply_filters` now fixes its side of both formats, ``"event: <reason>"`` and
``"event:unknown: <reason>"``, and `tests/test_pair_filters.py` holds it to
them. What is still unsettled is the reason text itself, which the guard
supplies and which is scaffolded, so neither renderer matches on the part after
the colon.

This exists because the list was written out twice, here in the `apply_filters`
docstring and in section 6 of ``docs/scoring-spec.md``, and the two had already
drifted: the spec listed four of the seven. ``tests/test_blockers.py`` asserts
this mapping and the spec table agree, so the next addition cannot land in one
place only.

Adding an entry here is a contract change. A renderer must show it, because a
marker that is not rendered does not exist (ADR 0002 rule 3).
"""


AGREEMENT_TIE_EPSILON: float = 1e-9
"""Below this, two legs are treated as having scored a pillar identically.

A float-equality guard rather than a modelling threshold. A pillar that scores
the two legs the same has no opinion on the pair, and it is excluded from both
sides of the agreement fraction rather than counted as agreeing.
"""

_CONVICTION_RANK: Mapping[Conviction, int] = {
    Conviction.NONE: 0,
    Conviction.LOW: 1,
    Conviction.MEDIUM: 2,
    Conviction.HIGH: 3,
}
"""Sort order for `shortlist`, highest conviction first.

`Conviction` is a string enum with no ordering of its own, so the ladder is
spelled out here rather than relied on through declaration order.

Private. Consumers outside this module ask `at_least` rather than borrowing
the table, so the comparison exists once instead of once per caller.
"""

_LADDER: tuple[Conviction, ...] = tuple(
    sorted(_CONVICTION_RANK, key=lambda tier: _CONVICTION_RANK[tier])
)
"""The same ladder as a sequence, weakest first, for stepping down it.

Derived from `_CONVICTION_RANK` rather than written out again, so a tier added
to one cannot be missing from the other.
"""

CalendarGuard = Callable[[str, date], tuple[Sequence[str], str | None]]
"""Injected hook reporting hard calendar blockers for one currency on one date.

Returns ``(blockers, unknown_reason)``.

``blockers`` is zero or more reason strings for high-impact releases inside
the execution blackout window, which is
``DataConfig.calendar_blackout_before_min`` (30) and
``calendar_blackout_after_min`` (60) either side of the release. Inside that
window there is no order, full stop.

``unknown_reason`` is ``None`` when the guard checked and ``blockers`` is the
final answer. It is a reason string, never ``None``, when the guard could not
determine whether this currency has a blocker on this date at all: a failed
calendar fetch, a cached week that ends before the date, or a date beyond the
horizon of the data supplied, in `calendar_guard.CoverageGap`'s own words. A
guard reporting ``unknown_reason`` must return an empty ``blockers``, so the
two fields are never in tension about whether this currency is clear.

This tuple exists because a bare ``Sequence[str]`` cannot say "could not
check" distinctly from "no blockers": an empty sequence from a guard that
tried and failed to reach the calendar and an empty sequence from a guard that
checked and found a genuinely quiet day were the same value, which is the
ambiguity issue #43 removes. `apply_filters` reads ``unknown_reason`` and
appends ``"event:unknown: <reason>"`` rather than treating the currency as
clear.
"""

EventHorizonGuard = Callable[[str, date], bool | None]
"""Injected hook answering whether a high-impact event is due within 24 hours.

Three answers, not two. ``True`` is a high-impact event found in the horizon,
``False`` is a horizon the guard reached and found quiet, and ``None`` is a
guard that was asked and could not tell, most often a failed fetch or a cached
week that does not reach ``asof``. The third is why this is not a ``bool``: a
guard with only two answers has to return ``False`` when it cannot see, and
``False`` is the one answer that buys full conviction. An unseen calendar was
therefore worth more than a seen one, which is the defect issue #45 closes.
`conviction_for` treats ``None`` exactly as it treats ``True``.

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
    by_currency = {score.currency: score for score in scores}
    # Checked before the guard runs. The guard may be a calendar fetch per
    # currency, and a run missing a leg is going to raise either way, so it
    # should raise before spending them.
    for pair in ALL_PAIRS:
        for currency in split_pair(pair):
            if currency not in by_currency:
                raise KeyError(
                    f"{currency} has no CurrencyScore, so {pair} cannot be "
                    "built. Skipping it would leave a hole in the report that "
                    "reads as an absence of opportunity rather than of data."
                )
    event_near = _events_within_24h(by_currency, asof, event_horizon_guard)
    built: list[PairBias] = []
    for pair in ALL_PAIRS:
        base, quote = split_pair(pair)
        # Indexed rather than fetched with a default, so a currency the scorer
        # did not produce raises here and names itself. A skipped row reads as
        # an absence of opportunity when it is an absence of data.
        base_leg = by_currency[base]
        quote_leg = by_currency[quote]
        spread = base_leg.composite - quote_leg.composite
        share = agreement(base_leg, quote_leg)
        conviction = conviction_for(
            spread,
            share,
            # The worse-covered and the more-dispersed leg, never the mean of
            # the two. `conviction_for` documents why; picking them is this
            # function's job because only it holds both legs.
            min(base_leg.coverage, quote_leg.coverage),
            max(base_leg.dispersion, quote_leg.dispersion),
            _worst_horizon_answer(event_near[base], event_near[quote]),
            config.scoring,
        )
        direction = direction_for(spread, config.scoring)
        if conviction is Conviction.NONE:
            # Direction and conviction must never disagree. A pair the model
            # will not back at any size has no direction worth printing.
            direction = Direction.NEUTRAL
        built.append(
            PairBias(
                pair=pair,
                base=base,
                quote=quote,
                spread=spread,
                direction=direction,
                conviction=conviction,
                asof=asof,
                base_score=base_leg.composite,
                quote_score=quote_leg.composite,
                agreement=share,
            )
        )
    return tuple(built)


def _events_within_24h(
    by_currency: Mapping[str, CurrencyScore],
    asof: date,
    guard: EventHorizonGuard | None,
) -> Mapping[str, bool | None]:
    """Ask the guard once per currency rather than once per pair.

    Args:
        by_currency: The run's scores, keyed by ISO code.
        asof: The date the run represents.
        guard: The injected `EventHorizonGuard`, or ``None``.

    Returns:
        One answer per currency, passed through from the guard: ``True`` for an
        event found in the horizon, ``False`` for a horizon reached and quiet,
        ``None`` for a guard that could not tell. All ``False`` when no guard
        was supplied, which applies no cap: the right default for a backtest
        and the wrong one for a live run.

        No guard at all is deliberately not ``None``. A caller that passed
        nothing knows it passed nothing, and turning that into a universe-wide
        conviction cap would demote every backtest ever run. A guard that was
        asked and failed is the different fact, and it is the one ``None``
        carries. ADR 0002 rule 4 is the general form.

    Each currency appears in seven of the 28 pairs, so asking per pair would
    make the same call seven times. A guard is a calendar lookup and may be a
    fetch, and more importantly two calls for one currency could disagree
    inside a single run, which would put two pairs sharing a leg on different
    sides of the cap with nothing recording why.

    """
    if guard is None:
        return dict.fromkeys(by_currency, False)
    return {
        currency: _horizon_answer(guard(currency, asof)) for currency in by_currency
    }


def _horizon_answer(raw: object) -> bool | None:
    """Normalise one guard answer without collapsing its third state.

    Args:
        raw: Whatever the injected guard returned.

    Returns:
        ``None`` unchanged, anything else through ``bool``. The truthiness
        conversion is kept for the other two so a guard returning ``1`` or an
        empty tuple still works, and ``None`` is routed around it because
        ``bool(None)`` is ``False``, which is precisely the collapse this
        function exists to prevent.

    """
    return None if raw is None else bool(raw)


def _worst_horizon_answer(first: bool | None, second: bool | None) -> bool | None:
    """Combine one pair's two legs into the answer the cap should act on.

    Args:
        first: The base leg's answer.
        second: The quote leg's answer.

    Returns:
        ``True`` if either leg has an event, otherwise ``None`` if either leg
        could not be told, otherwise ``False``.

    The order matters and ``or`` cannot express it. ``None or False`` is
    ``False``, so a pair whose base leg the guard could not see and whose quote
    leg was quiet would read as a fully checked quiet pair, which is the same
    defect one level up from the one the third state fixes. A found event
    outranks an unknown because it is the more specific fact, and both cap at
    `Conviction.LOW` anyway, so the ordering changes what a reader is told
    rather than what the engine does.

    """
    if first is True or second is True:
        return True
    if first is None or second is None:
        return None
    return False


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
    if spread >= config.min_spread_low:
        return Direction.LONG
    if spread <= -config.min_spread_low:
        return Direction.SHORT
    return Direction.NEUTRAL


def conviction_for(
    spread: float,
    agreement_fraction: float,
    coverage_fraction: float,
    dispersion_value: float,
    event_within_24h: bool | None,
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
        event_within_24h: ``True`` when a high-impact event is due on either
            leg within 24 hours, ``False`` when the horizon was reached and is
            quiet, ``None`` when the guard was asked and could not tell. From
            `EventHorizonGuard` by way of `_worst_horizon_answer`, which
            combines the pair's two legs. ``None`` caps exactly as ``True``
            does: an unseen calendar must never be worth more than a seen one,
            and a position opened today is held through whatever the model did
            not see whether the release was found or merely not ruled out.
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

        ``event_within_24h`` is ``True`` **or** ``None``: cap at
        `Conviction.LOW`. A position opened today would be held through a
        repricing the model has not seen, and the rate path is exactly what the
        heaviest pillar is measuring. ``None`` caps for the same reason and not
        as a precaution: the engine cannot say the horizon is clear, so it must
        not price the call as though it had.

        Only ``False`` leaves the tier alone, and ``False`` means the guard
        reached the horizon and found it quiet. Written as an explicit
        three-way test rather than a truthiness check, because ``bool(None)``
        is ``False`` and a truthiness check silently awards an unseen calendar
        the uncapped answer.

    Demotions compound, and conviction never rises. A pair at ``|spread| = 2.8``
    with coverage 0.70, dispersion 1.4 and weak agreement falls from HIGH to
    NONE: the agreement cap takes it to LOW first, and the coverage and
    dispersion demotions then take LOW to NONE. The order above is not
    presentational. Applying the two demotions before the cap lands on LOW
    instead, two tiers apart on the same four inputs, and section 5.4 of
    ``docs/scoring-spec.md`` lists them in the order used here. Every input to
    this function is a reason to doubt the spread, and none of them is a reason
    to believe it more than the spread already says.

    Note that ``config.min_coverage`` (0.60) does not appear here. Coverage below
    that floor is a hard filter in `apply_filters`, not a demotion, because at
    that point the question is no longer how much to believe the number but
    whether there is a number at all.

    """
    size = abs(spread)
    if size >= config.min_spread_high:
        tier = Conviction.HIGH
    elif size >= config.min_spread_medium:
        tier = Conviction.MEDIUM
    elif size >= config.min_spread_low:
        tier = Conviction.LOW
    else:
        tier = Conviction.NONE

    # Applied in the order the docstring lists, and the order is load-bearing
    # rather than presentational: a cap before two one-step demotions lands two
    # tiers below the same cap after them. The issue's own case is
    # |spread| 2.8 with weak agreement, coverage 0.70 and dispersion 1.4, which
    # reaches NONE this way round and LOW the other.
    if agreement_fraction < config.min_agreement:
        tier = _cap(tier, Conviction.LOW)
    if coverage_fraction < config.coverage_demotion:
        tier = _demote(tier)
    if dispersion_value > config.max_dispersion:
        tier = _demote(tier)
    # ``is not False`` rather than a truthiness test, so the unknown answer
    # caps alongside the found one. A plain ``if event_within_24h`` reads
    # ``None`` as "no event" and hands an outage the HIGH tier.
    if event_within_24h is not False:
        tier = _cap(tier, Conviction.LOW)
    return tier


def _demote(tier: Conviction) -> Conviction:
    """Move one step down the ladder, stopping at `Conviction.NONE`.

    Args:
        tier: The tier before this demotion.

    Returns:
        The next tier down, or `Conviction.NONE` if already there. The bottom
        absorbs rather than wrapping or raising, because a pair can attract
        more reasons to doubt it than there are steps to take.

    """
    return _LADDER[max(0, _CONVICTION_RANK[tier] - 1)]


def _cap(tier: Conviction, ceiling: Conviction) -> Conviction:
    """Hold a tier at or below a ceiling, never raising it.

    Args:
        tier: The tier before this cap.
        ceiling: The highest tier this objection permits.

    Returns:
        The lower of the two. A cap differs from a demotion in that it does not
        get worse as the spread widens: one pillar carrying the whole spread is
        the same objection at 1.6 as at 2.8, so it sets a ceiling rather than
        subtracting a step.

    """
    if _CONVICTION_RANK[tier] <= _CONVICTION_RANK[ceiling]:
        return tier
    return ceiling


def at_least(tier: Conviction, floor: Conviction) -> bool:
    """Whether ``tier`` reaches ``floor`` on the conviction ladder.

    Args:
        tier: The conviction a pair carries.
        floor: The lowest conviction the caller will accept.

    Returns:
        True when ``tier`` is at or above ``floor``. Inclusive at the level
        named: asking for medium keeps medium.

    `Conviction` is a string enum with no ordering of its own, so a caller
    comparing two tiers has to get the ladder from somewhere. This is that
    somewhere. Exported as a question rather than as `_CONVICTION_RANK`,
    because a caller handed the table writes the comparison itself and two
    callers write it twice: add a tier, or change the ends of the ladder, and
    each copy has to be found again. `fbe.cli.bias` filters
    ``--min-conviction`` through this and states the reason a pair was hidden
    from the same call.

    """
    return _CONVICTION_RANK[tier] >= _CONVICTION_RANK[floor]


def blocking(blockers: Sequence[str]) -> tuple[str, ...]:
    """Keep only the entries whose kind actually stops a pair being traded.

    Args:
        blockers: A `PairBias.blockers` tuple, as `apply_filters` set it.

    Returns:
        The subset `BLOCKERS` marks as blocking, in the order given. An entry
        whose kind is not declared raises rather than being dropped: an
        unrecognised marker is a defect in whatever produced it, and silently
        ignoring it would let a real block disappear from an explanation.

    Raises:
        ValueError: An entry matching no key in `BLOCKERS`.

    Three of the eight kinds do not block, and an offline run carries two of
    them on every pair, so "why was this pair removed" and "what does this
    pair carry" are different questions with different answers. A renderer
    listing the whole tuple as the reason names ``cost:unchecked`` and
    ``event:unchecked``, two checks that never ran, as reasons a pair was
    dropped.

    The longest matching prefix wins, which is the rule `BLOCKERS` documents
    and the reason this lives here rather than in a renderer. Taking the first
    key that matches reads those same two markers as hard blocks.

    """
    kept: list[str] = []
    for entry in blockers:
        matches = [kind for kind in BLOCKERS if entry.startswith(kind)]
        if not matches:
            raise ValueError(
                f"{entry!r} matches no kind in BLOCKERS, so whether it stops a "
                "pair being traded cannot be answered. Every string reaching "
                "blockers is one apply_filters emitted."
            )
        if BLOCKERS[max(matches, key=len)]:
            kept.append(entry)
    return tuple(kept)


def agreement(base_leg: CurrencyScore, quote_leg: CurrencyScore) -> float:
    """Return the share of pillar weight pointing the way the headline does.

    Args:
        base_leg: The base currency's aggregate score, with its pillars, whose
            `PillarScore.weight` values must already carry the staleness penalty
            from `scoring.apply_staleness_penalty`. Named for the leg rather than
            for the score to keep it distinct from ``PairBias.base_score``, which
            is a bare float.
        quote_leg: The quote currency's aggregate score, on the same terms.

    The post-penalty requirement is not a formality. ``w_pair`` is built from
    ``PillarScore.weight``, which holds the configured weight before the penalty
    is applied and the effective weight after it. Handed unpenalised scores this
    function still returns a plausible-looking number, computed as though every
    pillar were fresh, and the stale-leg discount disappears from the agreement
    fraction without any error being raised. A pair whose euro leg is running on
    two-month-old growth data would then report the same agreement as one whose
    data landed this morning, and the conviction ladder would take it at face
    value.

    Returns:
        A value in ``[0.0, 1.0]``. ``0.0`` when no pillar is considered, which
        pairs with a coverage figure low enough to block the trade anyway.

        Two exclusions, and they are different rules. A pillar scoring the two
        legs identically has no opinion on this pair. A pillar with ``z`` of
        ``None`` on either leg had no data at all, and `missing_score` gives it
        a neutral ``0.0`` that would otherwise read as an opinion held by
        whichever leg does have data. See section 5.3 of
        ``docs/scoring-spec.md`` for the worked case: a missing POSITIONING on
        one leg moves a pair from LOW to MEDIUM, which is half as much of the
        account again at risk.

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
    the call wrong are errors in unrelated data sets. The model's prior is that a
    smaller signal held with more confidence is the one worth taking on a small
    account at 1-2% risk per trade, because survival comes from being right
    consistently rather than from the size of the occasional outlier. None of
    that has been measured; Phase 6 is the first point at which it could be.

    This is why agreement caps conviction rather than adding to it. A broad,
    modest signal reaches MEDIUM on its spread alone and stays there; a narrow,
    dramatic one gets pulled back down to LOW.

    """
    spread = base_leg.composite - quote_leg.composite
    considered = 0.0
    agreeing = 0.0
    for name, base_pillar in base_leg.pillars.items():
        quote_pillar = quote_leg.pillars.get(name)
        if quote_pillar is None:
            # A pillar only one leg carries cannot be differenced, so it has no
            # d(p) to have an opinion with. It is absent from both sides rather
            # than counted as a disagreement, for the same reason a tie is.
            continue
        if base_pillar.z is None or quote_pillar.z is None:
            # A pillar with no usable data still arrives here, carrying
            # `missing_score`'s neutral 0.0 and an effective weight the
            # staleness penalty has taken to zero. Its difference would be
            # `0.0 - score(other leg)`, whose sign the leg with data decides on
            # its own, so counting it turns an absence into half an opinion.
            # `z` is the marker rather than a zero weight: a pillar whose weight
            # decayed through staleness still had data, and a fresh pillar can
            # genuinely score zero.
            continue
        difference = base_pillar.score - quote_pillar.score
        if abs(difference) <= AGREEMENT_TIE_EPSILON:
            continue
        pair_weight = (base_pillar.weight + quote_pillar.weight) / 2.0
        considered += pair_weight
        if _sign(difference) == _sign(spread):
            agreeing += pair_weight
    if considered <= 0.0:
        return 0.0
    return agreeing / considered


def _sign(value: float) -> int:
    """Return -1, 0 or +1, matching the spec's ``sign`` rather than a boolean.

    Args:
        value: Any float.

    Returns:
        The sign, with zero as its own answer.

    Section 5.3 compares ``sign(d(p))`` against ``sign(spread)``, and a spread
    of exactly zero has sign zero, which no pillar's difference can match. A
    boolean test such as ``(d > 0) == (spread > 0)`` reads differently there: it
    counts every pillar leaning negative as agreeing with a spread that leans
    neither way. The pair is `Conviction.NONE` and `Direction.NEUTRAL` at a zero
    spread whatever this returns, so nothing downstream moves, but
    ``PairBias.agreement`` is rendered and would show a number the published
    formula does not produce.

    """
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


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
            window. Must give the same answer for the same currency throughout
            one run, memoised by the caller if the underlying fetch is not
            already stable. This function sees one pair, so it asks about two
            legs and cannot dedupe across the 28; each currency is therefore
            asked seven times. `build_pair_biases` asks its own guard once per
            currency for exactly this reason, and the argument is stronger
            here, because two answers that disagree would put two pairs sharing
            a leg on different sides of a hard block with nothing recording
            why. When ``None`` the check is skipped and ``"event:unchecked"``
            is appended to ``blockers`` without setting ``tradeable`` to
            ``False``. Silence and an all-clear must not look the same. When a
            guard is supplied but reports ``unknown_reason`` for either leg,
            ``"event:unknown: <reason>"`` is appended instead, also without
            setting ``tradeable`` to ``False``; a guard that tried and failed
            is a different fact from no guard at all, and the two get
            different names, but neither is currently allowed to block a pair
            on its own. See `UNKNOWN_SUFFIX`.
        cost_ratio: Round-trip dealing cost as a share of the expected move over
            the bias horizon, supplied by the execution layer, which owns the ATR
            and the broker's spread table. ``None`` records
            ``"cost:unchecked"`` without blocking.

    Returns:
        A new `PairBias` with ``tradeable`` and ``blockers`` set. Frozen input,
        new object out. Blockers accumulate rather than short-circuiting, because
        a pair blocked for three reasons is a different thing from a pair blocked
        for one, and the trader should see all three. ``blockers`` is set rather
        than appended to, so filtering an already-filtered bias says the same
        thing rather than doubling every reason.

    Raises:
        KeyError: Either leg is absent from ``scores``, naming the currency.
            Not skipped and not read as zero coverage: `build_pair_biases`
            refuses the same shape for the same reason, because a missing row
            reads as an absence of opportunity rather than an absence of data.
        ValueError: A ``cost_ratio`` that is not finite or is negative, a leg
            whose ``coverage`` is not finite, or a guard that reports an
            unknown reason for a leg and returns blockers for it too. See
            `_refuse_unusable` and `_calendar_entries`; each is an input no
            comparison here can judge, and each would otherwise reach a report
            as a pair that looked checked.

    The filters, matching section 6 of ``docs/scoring-spec.md``. `BLOCKERS` is
    the enumeration of every string this function may append and whether each
    one blocks; the prose below says why each exists. If the two disagree,
    `BLOCKERS` is what the code emits and the prose is stale.

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

        ``no_coverage``: either leg has ``coverage <= 0.0``. No composite
        exists. The comparison admits a negative, which is unreachable while
        coverage is a sum of non-negative weights, so that an upstream defect
        reads as no data rather than as data. It fires alongside ``coverage``
        at zero, because both statements are true and they say different
        things: one that the composite rests on too little, the other that
        there is no composite.

        ``event``: whatever `CalendarGuard` returns as ``blockers`` for either
        leg, for the execution blackout window only. A pair has two legs, so an
        FOMC evening blocks every dollar pair and not only the one the trader
        was watching. The wider 24-hour test is not here: it is a conviction
        cap in `conviction_for`, and the two are kept apart deliberately.

        ``event:unchecked`` and ``cost:unchecked``: recorded when the
        corresponding input was not supplied, and do not block, so an offline run
        still produces biases. Both renderers show these on a tradeable pair
        rather than printing an unqualified "yes", because a marker that is not
        rendered does not exist. See `UNCHECKED_SUFFIX` and ADR 0002 rule 3.

        ``event:unknown``: recorded when `CalendarGuard` was supplied and
        reported ``unknown_reason`` for either leg, meaning it tried to check
        and could not, most commonly because the calendar fetch behind it
        failed or a cached week does not reach `asof`. Distinct from
        ``event:unchecked``, which means no guard ran at all: ADR 0002 rule 4
        requires a check that ran and failed to be told apart from a check
        that never ran, because a trader's response differs, check by hand
        immediately versus nothing is wrong. Does not currently block, for the
        same reason ``event:unchecked`` does not: refusing every pair on a
        single failed fetch would cost a full trading day over an outage that
        may resolve on the next run. **Provisional.** Whether unknown calendar
        coverage should instead set ``tradeable = False``, for some or all
        currencies, is open on issue #24 and is the child of #41 that consumes
        this marker rather than the marker's own concern. This function's
        behaviour today is the same non-blocking default `event:unchecked`
        already documents, chosen because it is the only option that does not
        pre-empt #24 by changing behaviour that already shipped, not because
        it is the final answer. See `UNKNOWN_SUFFIX`.

    What this function does not do. It never changes ``direction``, ``spread`` or
    ``conviction``. The model's view and the tradeability of that view are
    separate facts, and a report that quietly neutralised a blocked pair would
    lose the record of what the engine actually thought, which is the only thing
    a later review can learn from. It also never chooses an entry time: the
    blackout windows this function reads decide whether a pair is `tradeable`
    at all, and when it is, the trendline or channel touch that starts the
    trade stays with the trader's own technical rules, exactly as it does for
    every other pair.

    """
    base = scores[bias.base]
    quote = scores[bias.quote]
    _refuse_unusable(bias, base, quote, cost_ratio)

    # Each entry is the `BLOCKERS` kind paired with the string emitted for it.
    # The two differ for `event` and `event:unknown`, which carry a reason.
    # Keeping the kind rather than re-deriving it from the text is what makes
    # `tradeable` safe: four of the eight emitted strings begin with a shorter
    # key than their own. "cost:unchecked" starts with "cost", both
    # "event:unchecked" and "event:unknown: ..." start with "event", and
    # "event: ..." does too, so a consumer taking the first prefix that matches
    # reads three non-blocking markers as hard blocks and refuses every pair in
    # an offline run.
    entries: list[tuple[str, str]] = []

    if bias.direction is Direction.NEUTRAL or bias.conviction is Conviction.NONE:
        entries.append(("no_edge", "no_edge"))

    # Both coverage statements can be true at once and both are recorded, since
    # they say different things: one that the composite rests on too little, the
    # other that there is no composite. The comparison is ``<= 0.0`` rather than
    # ``== 0.0`` so a negative coverage, which would be a defect upstream, reads
    # as no data rather than as data.
    if base.coverage <= 0.0 or quote.coverage <= 0.0:
        entries.append(("no_coverage", "no_coverage"))
    if min(base.coverage, quote.coverage) < config.scoring.min_coverage:
        entries.append(("coverage", "coverage"))

    if cost_ratio is None:
        entries.append(_unchecked("cost"))
    elif cost_ratio > config.scoring.max_cost_ratio:
        entries.append(("cost", "cost"))

    if calendar_guard is None:
        entries.append(_unchecked("event"))
    else:
        entries.extend(_calendar_entries(bias, calendar_guard, asof))

    return replace(
        bias,
        tradeable=not any(BLOCKERS[kind] for kind, _ in entries),
        blockers=tuple(text for _, text in entries),
    )


def _refuse_unusable(
    bias: PairBias,
    base: CurrencyScore,
    quote: CurrencyScore,
    cost_ratio: float | None,
) -> None:
    """Refuse an input no comparison in this function can judge.

    Args:
        bias: The pair, named in the message.
        base: The base leg's score, read for ``coverage``.
        quote: The quote leg's score, read for ``coverage``.
        cost_ratio: The dealing cost as supplied, or ``None``.

    Raises:
        ValueError: A ``cost_ratio`` that is not finite or is negative, or a
            leg whose ``coverage`` is not finite.

    Every filter below is a ``<`` or a ``>``, and every comparison against NaN
    is False, so an unusable number passes all of them and the pair comes back
    with ``tradeable`` True and an empty ``blockers``. That is the one state
    this function reserves for "every check ran and every check passed", so a
    NaN cost ratio would print exactly like a pair verified as cheap.

    ``None`` and only ``None`` means a value was not supplied. A cost ratio
    that is negative, or either value not being a real number, is a defect in
    whatever produced it, and this is the last place it can be caught before it
    becomes a row on a report.

    """
    if cost_ratio is not None and (not isfinite(cost_ratio) or cost_ratio < 0.0):
        raise ValueError(
            f"{bias.pair} was given a cost_ratio of {cost_ratio!r}. A dealing "
            "cost is a non-negative share of the expected move, and None is "
            "how a caller says it was not supplied"
        )
    for leg, score in ((bias.base, base), (bias.quote, quote)):
        if not isfinite(score.coverage):
            raise ValueError(
                f"{leg} has a coverage of {score.coverage!r}, which no "
                "threshold can judge. Coverage is a fraction of pillar weight "
                "between 0.0 and 1.0"
            )


def _unchecked(kind: Literal["cost", "event"]) -> tuple[str, str]:
    """Return the entry recording that ``kind``'s check did not run.

    Args:
        kind: The base blocker name, ``"cost"`` or ``"event"``.

    Returns:
        The `BLOCKERS` kind and the string to emit, which are the same here:
        a check that never ran has no reason to carry, only a name.

    """
    marker = kind + UNCHECKED_SUFFIX
    return (marker, marker)


def _calendar_entries(
    bias: PairBias,
    calendar_guard: CalendarGuard,
    asof: date,
) -> list[tuple[str, str]]:
    """Ask the guard about both legs and record what it said.

    Args:
        bias: The pair, read for its two legs.
        calendar_guard: The injected guard.
        asof: The run's date, which is what the guard is asked about rather
            than today. A guard asked about the wrong day answers about the
            wrong day, and on a historical run every answer would be wrong the
            same way.

    Returns:
        Zero or more entries, in base-then-quote order. A leg the guard could
        not check contributes one ``event:unknown`` entry carrying the reason;
        a leg it checked contributes one ``event`` entry per release it found,
        each carrying the guard's own reason so a reader sees which release and
        when. A leg that was checked and is quiet contributes nothing, which is
        the only state that prints an unqualified clear.

        Both legs are asked and both are recorded. A pair has two legs, so an
        FOMC evening blocks every dollar pair and not only the one being
        watched, and a leg the guard could not reach leaves a marker even when
        the other leg already blocked the pair: otherwise a reader would take
        the calendar to have been fully consulted.

    """
    entries: list[tuple[str, str]] = []
    for leg in (bias.base, bias.quote):
        found, unknown_reason = calendar_guard(leg, asof)
        if unknown_reason is not None:
            if found:
                # `CalendarGuard` promises the two fields are never in tension
                # about whether a currency is clear, and this is the one check
                # that hard-blocks a pair. Discarding ``found`` here would have
                # marked an evening on which the guard positively identified a
                # release inside the blackout window as tradeable, carrying a
                # note saying the calendar was uncertain, which reads as a row
                # that was handled carefully. The guard is this codebase's own,
                # so a breach is a defect to fix rather than a condition to
                # tolerate.
                raise ValueError(
                    f"the calendar guard reported {leg} as unknown "
                    f"({unknown_reason}) and also returned {len(found)} "
                    f"blocker(s) for it: {list(found)}. A guard reporting an "
                    "unknown reason must return no blockers, so these two "
                    "answers cannot both be acted on"
                )
            marker = "event" + UNKNOWN_SUFFIX
            entries.append((marker, f"{marker}: {unknown_reason}"))
            continue
        entries.extend(("event", f"event: {reason}") for reason in found)
    return entries


def shortlist(biases: Sequence[PairBias], limit: int) -> Sequence[PairBias]:
    """Pick the handful of pairs worth putting on a chart today.

    Args:
        biases: Filtered biases from `apply_filters`, normally all 28.
        limit: How many pairs to return at most. The caller passes
            ``config.risk.max_concurrent_positions``: there is no purpose in
            shortlisting more trades than the risk rules permit to be open, and
            the number lives in `RiskConfig` and nowhere else. There is no
            default on purpose. A default here would be a second copy of the
            cap, and the two would drift apart silently: a trader who lowers the
            cap to 2 would still be handed three ideas and refused the third at
            the ticket. ``0`` returns nothing, for a run where the risk rules
            permit no new positions.

    Returns:
        Up to ``limit`` tradeable biases, best first, as a tuple. Ordered by
        conviction first and by absolute spread within a conviction level, with
        ties broken by pair name so two identical runs produce identical
        shortlists. An empty ``biases`` yields an empty tuple.

    Raises:
        ValueError: If ``limit`` is negative. A negative cap is a configuration
            error, not a request for an empty shortlist.

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
    if limit < 0:
        raise ValueError(f"shortlist limit must be non-negative, got {limit}")

    candidates = [
        bias for bias in biases if bias.tradeable and bias.conviction != Conviction.NONE
    ]
    candidates.sort(
        key=lambda bias: (
            -_CONVICTION_RANK[bias.conviction],
            -abs(bias.spread),
            bias.pair,
        )
    )

    taken: list[PairBias] = []
    legs_in_use: set[str] = set()
    for bias in candidates:
        if len(taken) >= limit:
            break
        if bias.base in legs_in_use or bias.quote in legs_in_use:
            continue
        taken.append(bias)
        legs_in_use.update((bias.base, bias.quote))
    return tuple(taken)
