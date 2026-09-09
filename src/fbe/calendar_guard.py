"""Blackout windows around high-impact economic releases.

The trading plan says to avoid trading during high-impact news. That rule is
easy to state and easy to break, because the calendar is long, the release
times are in someone else's timezone, and the temptation to take the setup
anyway arrives exactly when the setup looks best. This module turns the rule
into something checkable.

The guard is a filter, not a forecaster. It has no opinion on which way NFP
will print. It only knows that price around a high-impact release is driven by
positioning and surprise rather than by the fundamental picture the rest of
this engine models, so a bias derived from that picture has no edge in those
minutes.

All datetimes in this module are timezone-aware and UTC. Naive datetimes are
rejected rather than assumed to be UTC or local: a calendar that quietly treats
14:30 South African time as 14:30 UTC misses the NFP window by two hours, which
is the entire window plus change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum

from fbe.config import DataConfig
from fbe.types import CalendarEvent

__all__ = [
    "HIGH_IMPACT_KEYWORDS",
    "TIGHTEN_BUFFER_R",
    "OpenPositionAction",
    "is_high_impact",
    "blackout_windows",
    "is_blacked_out",
    "next_clear_time",
    "action_for_open_position",
]


HIGH_IMPACT_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "nfp": (
        "non-farm payroll",
        "nonfarm payroll",
        "non farm payroll",
        "nfp",
        "average hourly earnings",
        "unemployment rate",
    ),
    "rate_decision": (
        "interest rate decision",
        "rate statement",
        "monetary policy statement",
        "official cash rate",
        "official bank rate",
        "cash rate",
        "policy rate",
        "fomc statement",
        "ecb press conference",
        "boe",
        "boj",
        "snb",
        "rba",
        "rbnz",
        "boc rate",
    ),
    "gdp": (
        "gdp",
        "gross domestic product",
    ),
    "inflation": (
        "cpi",
        "consumer price index",
        "ppi",
        "producer price index",
        "core inflation",
        "inflation rate",
    ),
    "retail_sales": (
        "retail sales",
        "core retail sales",
    ),
    "employment_uk_ca": (
        "claimant count",
        "average earnings index",
        "employment change",
        "labour market",
        "labor force survey",
        "jobs report",
    ),
    "trade_balance": (
        "trade balance",
        "current account",
        "goods trade balance",
    ),
    "central_bank_speech": (
        "speaks",
        "speech",
        "press conference",
        "testimony",
        "semi-annual",
        "governor",
        "chair",
        "president",
    ),
    "minutes_accounts": (
        "fomc minutes",
        "meeting minutes",
        "monetary policy meeting accounts",
        "policy meeting minutes",
        "account of the monetary policy meeting",
    ),
    "geopolitical": (
        "election",
        "referendum",
        "summit",
        "g7",
        "g20",
        "budget",
        "vote of confidence",
        "tariff",
    ),
}
"""The ten event categories the trading plan names, as matchable keywords.

This constant exists because feeds mislabel. ForexFactory and its peers assign
impact ratings editorially, and an ECB member's unscheduled remarks or a
mid-cycle CPI revision routinely arrive tagged medium when they move a pair
forty pips in a minute. Matching the title against these keywords catches those
before the impact field does.

The keywords are matched case-insensitively as substrings of the event title.
That is deliberately loose, and it will produce false positives: "president"
catches a head of state as well as a central bank president, and "budget"
catches routine fiscal housekeeping. On a small account a false positive costs
one skipped setup and a false negative costs a stop-out on a spike, so the trade
is not close. Tighten a category only after seeing it block something real.
"""


TIGHTEN_BUFFER_R: float = 1.0
"""Open profit, in R, at or above which a position may be held into a window.

The threshold has to be a number rather than a judgement, because it is used in
the one place this module tells the owner to close a live trade, and "roughly
1R" resolves differently at 0.5R depending on who is reading it and how the
week has gone.

1.0R is chosen because it is the point at which the position can absorb a full
stop-distance move against it and still be at breakeven. That is the size of
adverse move a high-impact release routinely produces, so it is the buffer that
makes holding a defensible decision rather than a hopeful one. It is a
threshold, not a measurement: revisit it once `journal.evaluate` can group
outcomes by ``exit_reason`` and show what holding through windows has actually
cost or saved.

The comparison is inclusive. Exactly 1.0R is TIGHTEN, below it is FLATTEN.
"""


class OpenPositionAction(StrEnum):
    """What to do with a position that is already open as an event approaches.

    Attributes:
        HOLD: No high-impact event on either leg has a window containing the
            decision time. Manage the trade on the chart as normal.
        TIGHTEN: The decision time is inside a window and open profit is at or
            above `TIGHTEN_BUFFER_R`. The position has enough buffer to survive
            a normal spike, so apply the plan's partial-profit or trailing-stop
            rules to reduce what is exposed, without abandoning the trade.
        FLATTEN: The decision time is inside a window and open profit is below
            `TIGHTEN_BUFFER_R`. Not enough buffer. Close it and re-enter after
            the window if the setup survives.

    """

    HOLD = "hold"
    TIGHTEN = "tighten"
    FLATTEN = "flatten"


def is_high_impact(event: CalendarEvent) -> bool:
    """Decide whether an event should be treated as high impact.

    An event qualifies if ``event.impact`` is ``"high"`` (case-insensitive) OR
    its title matches any keyword in `HIGH_IMPACT_KEYWORDS`. The OR is the point:
    the feed's rating is a hint, not an authority, and the keyword list is the
    trading plan's own list of events to avoid.

    Args:
        event: The calendar event to classify.

    Returns:
        True when the event should generate a blackout window.

    """
    raise NotImplementedError


def blackout_windows(
    events: Sequence[CalendarEvent],
    config: DataConfig,
) -> Sequence[tuple[datetime, datetime]]:
    """Derive no-trade windows from the high-impact events in a calendar.

    For every event passing `is_high_impact`, produce
    ``(scheduled_for - calendar_blackout_before_min,
    scheduled_for + calendar_blackout_after_min)``, defaulting to 30 minutes
    before and 60 minutes after. Overlapping windows are merged, so a morning
    with CPI at 13:30 and a Fed speaker at 14:00 yields one continuous window
    rather than two that a naive check could fall between.

    The asymmetry is deliberate. Thirty minutes before a release is enough to
    cover the pre-positioning drift and the liquidity thinning that precedes it.
    Sixty minutes after is longer because the first move is frequently wrong.
    Price spikes on the headline number, then reverses as the detail is read,
    the revisions are noticed, and the algorithmic flow unwinds. A trader
    entering on the spike is entering at the worst price of the hour, in the
    direction that is about to fail. Standing aside through the reversal, not
    just through the release, is the whole point of the rule.

    Windows are not per-currency here. Filtering to the pair being traded is
    `is_blacked_out`'s job; this function reports the calendar's shape.

    Args:
        events: Calendar events, in any order, from any source. Events with
            naive ``scheduled_for`` values are rejected.
        config: Supplies the before and after minute counts.

    Returns:
        Merged, sorted, non-overlapping ``(start, end)`` windows in UTC.

    Raises:
        ValueError: If any event carries a naive ``scheduled_for``.

    """
    raise NotImplementedError


def is_blacked_out(
    pair: str,
    when: datetime,
    events: Sequence[CalendarEvent],
    config: DataConfig,
) -> tuple[bool, str | None]:
    """Whether a NEW entry in ``pair`` is blocked at ``when``, and why.

    A pair is blocked when EITHER leg has a high-impact event whose window
    contains ``when``. Both legs matter because a currency pair is a ratio and
    either side of it can move. Eurozone CPI moves EURUSD whatever the dollar is
    doing; the euro leg is half the price. Checking only the quote currency,
    which is the easy mistake because the quote currency carries the pip, would
    leave every EUR, GBP, AUD and NZD release unguarded on the dollar pairs the
    plan actually trades.

    The returned reason should name the event, its currency and its scheduled
    time, so the shortlist can show "EURUSD blocked: EUR CPI at 09:00 UTC"
    rather than a bare flag. That string ends up in ``PairBias.blockers`` and in
    the journal, where it later explains why an obvious setup was skipped.

    This function answers the ENTRY question only. See `action_for_open_position`
    for a position that is already on.

    Args:
        pair: Six-character pair, e.g. ``"EURUSD"``.
        when: Proposed entry time, timezone-aware UTC.
        events: Calendar events covering at least the surrounding window.
        config: Supplies the blackout minutes.

    Returns:
        ``(True, reason)`` when blocked, ``(False, None)`` when clear.

    Raises:
        ValueError: If ``when`` is naive or ``pair`` is malformed.

    """
    raise NotImplementedError


def next_clear_time(
    pair: str,
    events: Sequence[CalendarEvent],
    config: DataConfig,
    after: datetime | None = None,
) -> datetime | None:
    """Earliest time from ``after`` at which ``pair`` is not blacked out.

    Walk the merged windows affecting either leg of ``pair`` in order. If
    ``after`` falls outside all of them, it is already clear and is returned
    unchanged. Otherwise return the end of the window it falls in, advancing
    through any windows that butt up against it so the answer is genuinely clear
    rather than clear for four minutes.

    Populates ``TradeIdea.blackout_until``, which is what turns a blocked pair
    from "not today" into "check again at 15:00".

    Args:
        pair: Six-character pair.
        events: Calendar events. Only events on either leg are considered.
        config: Supplies the blackout minutes.
        after: Start the search here. Defaults to the current UTC time.

    Returns:
        The clear time in UTC, or ``None`` when the supplied calendar does not
        extend far enough to find one. ``None`` means unknown, not clear, and
        must not be treated as permission to trade.

    Raises:
        ValueError: If ``after`` is naive.

    """
    raise NotImplementedError


def action_for_open_position(
    pair: str,
    when: datetime,
    events: Sequence[CalendarEvent],
    config: DataConfig,
    unrealised_r: float = 0.0,
) -> tuple[OpenPositionAction, str | None]:
    """Decide what to do with an open position in ``pair`` as ``when`` nears.

    Holding through an event and entering into one are different decisions and
    the guard must not conflate them. Entering is free to decline: the setup
    either survives the window or it does not, and skipping it costs nothing but
    a missed trade. Exiting is not free. Closing a position to dodge a release
    pays the spread twice, abandons a stop that was placed on structure, and
    surrenders the trade's remaining expectancy on the strength of an event that
    might not touch it. A rule that flattens every position before every
    high-impact print will bleed the account through costs alone.

    So the guard distinguishes them, and the distinction is buffer, measured
    against `TIGHTEN_BUFFER_R`. The rule has exactly two branches inside a
    window and no gap between them:

        * ``unrealised_r >= TIGHTEN_BUFFER_R`` returns TIGHTEN. The position can
          absorb a full stop-distance spike and still be at breakeven, so manage
          it with the plan's own tools: take partial profit at the nearest
          support or resistance, or pull the trailing stop in.
        * ``unrealised_r < TIGHTEN_BUFFER_R`` returns FLATTEN. That includes a
          position in modest profit, not only one at or below breakeven. A trade
          up 0.5R with a rate decision ten minutes out has less than half a stop
          of cover, and a spike through a structural stop is exactly the loss
          the plan's news rule exists to prevent.

    This also connects to the plan's time-based exit. A trade that has stalled
    and is drifting toward a scheduled release is not a trade waiting for its
    thesis, it is a trade waiting for a coin flip. The stall and the approaching
    event are the same signal, and the plan already says to close a stalled
    trade before its time expires.

    Args:
        pair: Six-character pair the position is in.
        when: Time of the decision, timezone-aware UTC. Normally now, or the
            moment the next window opens.
        events: Calendar events on either leg.
        config: Supplies the blackout minutes.
        unrealised_r: Open profit in R multiples, positive for profit, measured
            against the position's ``realised_risk_amount`` so it matches what
            the journal will later record. Defaults to 0.0, which is breakeven
            and falls below `TIGHTEN_BUFFER_R`, so a caller that does not track
            open profit gets the conservative answer.

    Returns:
        ``(action, reason)``. The reason is ``None`` only for
        `OpenPositionAction.HOLD` when no event is in range, and otherwise names
        the event, so the journal records what prompted an early exit.

    Raises:
        ValueError: If ``when`` is naive or ``pair`` is malformed.

    Note:
        This is advisory. It never sends an order. The owner executes, which
        keeps the decision where the plan puts it and keeps this module honest
        about being a filter rather than a trading system.

    """
    raise NotImplementedError
