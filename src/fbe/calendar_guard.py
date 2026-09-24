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

A calendar can fail as well as report. `CalendarCoverage` and `coverage_gap`
exist because a bare `Sequence[CalendarEvent]` cannot say whether it was ever
fetched: an empty sequence from a genuinely quiet week and an empty sequence
from a feed that returned "Request Denied" are the same value, and
`docs/decisions/0002-representing-not-known.md` rule 4 requires the two to be
told apart. `is_blacked_out` therefore answers blocked, clear, or unknown, and
`(False, None)` is never returned for a moment the supplied data does not
cover.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite

from fbe.config import DataConfig
from fbe.types import CalendarEvent
from fbe.universe import G10, split_pair

__all__ = [
    "HIGH_IMPACT_KEYWORDS",
    "TIGHTEN_BUFFER_R",
    "OpenPositionAction",
    "CoverageGap",
    "CalendarCoverage",
    "coverage_gap",
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


def _require_aware(moment: datetime, label: str) -> None:
    """Refuse a naive datetime rather than assuming a timezone for it.

    Args:
        moment: The datetime to check.
        label: What it is, for the message. The event's currency and title, or
            the name of the argument.

    Raises:
        ValueError: If ``moment`` carries no timezone.

    Assuming UTC and assuming local time are both wrong and neither is
    detectable afterwards. Reading 14:30 South African time as 14:30 UTC misses
    the payrolls window by two hours, which is the entire window plus change,
    and the comparison that got it wrong still returns a bool.

    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            f"{label} carries a naive datetime ({moment.isoformat()}); the guard "
            "compares moments and will not assume a timezone for one"
        )


def _merge(
    windows: Sequence[tuple[datetime, datetime]],
) -> tuple[tuple[datetime, datetime], ...]:
    """Collapse overlapping or touching intervals into the fewest that cover them.

    Args:
        windows: ``(start, end)`` pairs in any order, already in UTC.

    Returns:
        The same coverage as the fewest ordered, disjoint intervals. Touching
        intervals merge as well as overlapping ones: a window ending exactly
        when the next begins leaves no tradeable instant between them, so
        reporting two would invite a caller to find a gap that is not there.

    `fbe.datasources.calendar` holds the same algorithm for its own per-currency
    mapping. The duplication is deliberate: what is duplicated is interval
    arithmetic rather than a fact about the model, and `docs/data-sources.md`
    rules on this exact edge, that "`CalendarCoverage` lives in the guard, which
    is downstream, and the import edge between the two stays absent in both
    directions". Not `CLAUDE.md`, which rules only that data flows one way, and
    which this package already crosses in the other direction: two pillars
    import `fbe.datasources.registry`.

    """
    merged: list[tuple[datetime, datetime]] = []
    for opens, closes in sorted(windows):
        if merged and opens <= merged[-1][1]:
            previous_open, previous_close = merged[-1]
            merged[-1] = (previous_open, max(previous_close, closes))
        else:
            merged.append((opens, closes))
    return tuple(merged)


def _describe(event: CalendarEvent) -> str:
    """Name an event the way every reason in this module names one.

    Args:
        event: The release being reported.

    Returns:
        ``"<CCY> <title> at <YYYY-MM-DD HH:MM> UTC"``, converted to UTC
        whatever zone the feed carried. That string reaches
        `fbe.types.PairBias.blockers`, the report and the journal, where it
        later explains why an obvious setup was skipped or a working position
        was closed early.

    Raises:
        ValueError: If ``scheduled_for`` is naive. Both callers already check
            every event's shape before they reach here, so this never fires
            today. It is checked anyway because ``astimezone`` on a naive value
            assumes the *system* zone: a naive 09:00 read on the owner's
            machine would print as "07:00 UTC", which is plausible, wrong by
            two hours, and permanent once it is in the journal. Extracting this
            raised the number of sites that can reach it, and a precondition
            that lives only in a caller is one a third caller will not read.

    One function rather than one format string per caller. `is_blacked_out`
    and `action_for_open_position` write the same sentence into the same two
    places, and two copies of it drift: the journal would then hold two shapes
    of the same fact and a later reader could not group them.

    """
    _require_aware(event.scheduled_for, f"{event.currency} {event.title}")
    moment = event.scheduled_for.astimezone(UTC)
    return f"{event.currency} {event.title} at {moment:%Y-%m-%d %H:%M} UTC"


def _checked_legs(pair: str) -> tuple[str, str]:
    """Split a pair into legs, refusing one whose legs are not both G10.

    Args:
        pair: The pair as the caller wrote it, in any case. Uppercased before
            splitting, as `fbe.risk.currency_exposure` does, so a hand-typed
            ``eurusd`` from the pre-trade check is answered rather than read as
            a pair with no G10 legs.

    Returns:
        ``(base, quote)``, uppercased. Returned rather than discarded so a
        caller cannot validate one split and then use a second one that was
        derived differently.

    Raises:
        ValueError: If it is not six characters, or either leg is outside
            `fbe.universe.G10`.

    `split_pair` checks length only, so one mistyped character splits as
    cleanly as a real pair and then matches no event. The guard would answer
    that the morning is clear, or that a position may be held, which are both
    the quiet wrong answer this module exists to avoid.

    """
    base, quote = split_pair(pair.upper())
    for leg in (base, quote):
        if leg not in G10:
            raise ValueError(
                f"{pair!r} has a leg {leg!r} that is not a G10 currency. "
                "`split_pair` checks length only, so one mistyped character "
                "splits as cleanly as a real pair and then matches no event, "
                "and this function would answer that the morning is clear. "
                "`fbe.risk.currency_exposure` refuses the same input for the "
                "same reason."
            )
    return base, quote


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
    # Folded, not compared. The feed publishes ``"High"`` capitalised, which
    # `fbe.datasources.calendar.IMPACT_LEVELS` records along with the warning
    # that a consumer comparing against the lower-case spelling "would match
    # nothing and report every week as clear". That failure is silent, so the
    # fold is the load-bearing part of this line rather than tidiness.
    if event.impact.strip().casefold() == "high":
        return True
    folded = event.title.casefold()
    return any(
        keyword in folded
        for keywords in HIGH_IMPACT_KEYWORDS.values()
        for keyword in keywords
    )


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

    `CalendarSource.blackout_windows` carries the same name and the same
    interval arithmetic and differs on what goes into it: it windows every
    event it is handed, while this one windows only the events passing
    `is_high_impact`. That is right for each. The source reads a feed and has
    no standing to decide which releases the plan avoids; the guard is where
    that decision lives. The consequence is the part worth writing down: the
    source's mapping holds windows around minor releases too, so a caller
    reading it instead of this function inherits no filter and owes one.

    Args:
        events: Calendar events, in any order, from any source. Events with
            naive ``scheduled_for`` values are rejected.
        config: Supplies the before and after minute counts.

    Returns:
        Merged, sorted, non-overlapping ``(start, end)`` windows in UTC.

    Raises:
        ValueError: If any event carries a naive ``scheduled_for``.

    """
    # Materialised once, because this reads ``events`` twice. The annotation
    # says `Sequence`, so a typed caller cannot pass a generator, and an
    # untyped one would have the second pass see an exhausted iterator: every
    # window silently absent from a calendar full of releases.
    events = tuple(events)

    # Every event, before any is filtered out. A feed sending naive times is
    # broken in a way that reaches a qualifying event on the next run, so the
    # refusal is not conditional on this run's events happening to be quiet.
    for event in events:
        _require_aware(event.scheduled_for, f"{event.currency} {event.title}")

    # Refused here rather than in `Config.validate`, which covers no
    # `DataConfig` field, because this is the consumer that cannot tell. A
    # negative count inverts the window, so ``opens <= when <= closes`` is
    # false at every instant and the guard reports every moment clear,
    # payrolls included, on a sign typo in one config line. Zero on both
    # collapses it to a single instant, which is the same answer reached a
    # different way.
    if (
        config.calendar_blackout_before_min < 0
        or config.calendar_blackout_after_min < 0
    ):
        raise ValueError(
            "blackout minutes must not be negative; got "
            f"{config.calendar_blackout_before_min} before and "
            f"{config.calendar_blackout_after_min} after, which inverts every "
            "window and would report every moment as clear"
        )
    if (
        config.calendar_blackout_before_min == 0
        and config.calendar_blackout_after_min == 0
    ):
        raise ValueError(
            "both blackout minute counts are zero, so every window is a "
            "single instant and the guard can only block an entry timed to "
            "the release's exact second. Widen one, or take the guard out of "
            "the run deliberately rather than by configuring it to nothing."
        )

    before = timedelta(minutes=config.calendar_blackout_before_min)
    after = timedelta(minutes=config.calendar_blackout_after_min)
    spans = [
        (
            event.scheduled_for.astimezone(UTC) - before,
            event.scheduled_for.astimezone(UTC) + after,
        )
        for event in events
        if is_high_impact(event)
    ]
    return _merge(spans)


class CoverageGap(StrEnum):
    """Why `CalendarCoverage` cannot vouch for the moment it was asked about.

    Three answers rather than one, because each calls for a different response
    from the trader, not just a different word on the page.

    Attributes:
        FETCH_FAILED: The most recent fetch attempt errored and there is no
            cached week to fall back on. Nothing to reason from at all; check
            the calendar by hand before trading either leg.
        STALE_CACHE: The most recent fetch attempt errored, but an earlier
            successful fetch's coverage is being reused. The data is probably
            still accurate, since a scheduled release rarely moves once
            published, and it mainly needs a refresh when one becomes
            convenient rather than an immediate manual check.
        BEYOND_HORIZON: The most recent fetch succeeded and its coverage still
            ends before the moment asked about. Not a fault: the feed
            (`docs/data-sources.md`) publishes one week at a time by design, so
            every Friday run is beyond horizon for the following Monday until
            it is refetched. No amount of retrying helps; only time does.

            One state lands here that the sentence above does not describe: a
            fetch reporting success with no horizon at all, which nothing
            should build. `coverage_gap` files it here rather than inventing a
            fourth category or treating an absent horizon as one that reaches
            every moment. Retrying does help in that case, so the reason says
            so instead of naming a time.

    """

    FETCH_FAILED = "fetch_failed"
    STALE_CACHE = "stale_cache"
    BEYOND_HORIZON = "beyond_horizon"


@dataclass(frozen=True, slots=True)
class CalendarCoverage:
    """The calendar handed to the guard, with its own honesty attached.

    Replaces a bare `Sequence[CalendarEvent]` everywhere the guard has to
    decide whether a moment is covered. An empty sequence cannot say which of
    two very different situations produced it: `docs/data-sources.md` records
    both a genuinely quiet week with nothing scheduled, and an export that hit
    the publisher's rate limit and came back as a "Request Denied" page, which
    the parser is required to treat as an error rather than as an empty week.
    `events` alone gives `is_blacked_out` no way to tell those apart, which is
    exactly the defect issue #43 removes.

    Attributes:
        events: Whatever the source produced. Meaningful only when `fetch_ok`
            is `True`. A failed fetch should carry an empty tuple here, and a
            caller must check `fetch_ok` before reading `events`, not after,
            or a failure sitting on top of a stale cache reads as an empty
            week rather than as a fetch that failed.
        covers_through: The latest moment, in UTC, that this data can vouch
            for. A currency with no matching event before this time is
            genuinely clear up to it. `None` means there is no usable coverage
            at all: the state after a first-ever fetch fails with no cached
            week underneath it.
        fetch_ok: Whether the most recent fetch attempt for this run
            succeeded. Defaults to `True` so a caller that never touches this
            field, because its source never fails, does not accidentally claim
            a failure it did not have.
        fetch_error: Why the fetch failed, carried through to the report and
            the journal. `None` when `fetch_ok` is `True`.

    """

    events: Sequence[CalendarEvent]
    covers_through: datetime | None
    fetch_ok: bool = True
    fetch_error: str | None = None


def coverage_gap(
    calendar: CalendarCoverage,
    when: datetime,
) -> tuple[CoverageGap, str] | None:
    """Say why `calendar` cannot vouch for `when`, or that it can.

    `is_blacked_out` runs this before it looks at a single event, on each leg,
    because a blocked or clear answer is only honest once the data is known to
    reach `when` at all. Any future function in this module that decides
    blocked-or-clear should run this first too, rather than re-deriving the
    same three categories a second way.

    Args:
        calendar: The coverage supplied to the guard.
        when: The moment being checked, timezone-aware UTC.

    Returns:
        `None` when `calendar.fetch_ok` is `True` **and**
        `calendar.covers_through` is at or after `when`, which together are the
        only condition under which BLOCKED or CLEAR is an honest answer for it.
        The ``fetch_ok`` half was missing from this sentence while the bullets
        below carried it, so the two disagreed about a failed fetch whose cache
        still reaches ``when``. The bullets are right, and
        `CalendarCoverage.events` says why: its events are meaningful only when
        the fetch succeeded, so a clear answer read off them is a failure
        wearing a quiet week's clothes.

        Otherwise a `(CoverageGap, reason)` pair. Every reason names a concrete
        time or error rather than only the category, so it can be shown on the
        report rather than the trader having to look the category up. The one
        exception is the success-with-no-horizon state described on
        `CoverageGap.BEYOND_HORIZON`, where there is no time to name:

            `CoverageGap.FETCH_FAILED`: `calendar.fetch_ok` is `False` and
            `calendar.covers_through` is `None`.

            `CoverageGap.STALE_CACHE`: `calendar.fetch_ok` is `False` and
            `calendar.covers_through` is not `None`, naming the time that
            older coverage ends.

            `CoverageGap.BEYOND_HORIZON`: `calendar.fetch_ok` is `True` but
            `calendar.covers_through` is still short of `when`, naming the
            time the fresh coverage ends. The Friday-cache-queried-for-Monday
            case is this one.

    Raises:
        ValueError: If `when` is naive.

    """
    _require_aware(when, "when")

    # The fetch is judged before the horizon, and that order is the point.
    # `CalendarCoverage.events` is documented as meaningful only when
    # ``fetch_ok`` is true, and a caller must read ``fetch_ok`` "before reading
    # ``events``, not after, or a failure sitting on top of a stale cache reads
    # as an empty week rather than as a fetch that failed". So a failed fetch is
    # a gap whatever its cached horizon says, and the horizon only chooses which
    # of the two failure categories it is.
    if not calendar.fetch_ok:
        if calendar.covers_through is None:
            return (
                CoverageGap.FETCH_FAILED,
                calendar.fetch_error or "the calendar fetch failed and gave no reason",
            )
        return (
            CoverageGap.STALE_CACHE,
            f"cached through {calendar.covers_through.isoformat()}",
        )

    if calendar.covers_through is not None:
        _require_aware(calendar.covers_through, "covers_through")

    if calendar.covers_through is None:
        # A fetch that reports success with no horizon at all. Nothing should
        # build this, and it is a gap rather than a crash: the alternative is
        # treating an absent horizon as one that reaches every moment, which is
        # the quiet answer this function exists to refuse.
        return (
            CoverageGap.BEYOND_HORIZON,
            "the fetch reported success with no coverage at all, so there is "
            "no horizon to compare against; a refetch is worth trying",
        )

    if calendar.covers_through < when:
        return (
            CoverageGap.BEYOND_HORIZON,
            f"covers through {calendar.covers_through.isoformat()}",
        )
    return None


def is_blacked_out(
    pair: str,
    when: datetime,
    calendar: CalendarCoverage,
    config: DataConfig,
) -> tuple[bool | None, str | None]:
    """Whether a NEW entry in ``pair`` is blocked at ``when``, clear, or unknown.

    Three answers, and ``(False, None)`` must never stand in for the third.
    Before `CalendarCoverage` existed, this function took a bare
    ``Sequence[CalendarEvent]`` and returned ``(False, None)`` whenever nothing
    in it covered ``when``, which a genuinely clear calendar, a failed fetch
    and a stale Friday cache all produce identically. ``calendar`` is what
    makes the difference visible: `coverage_gap` is checked against ``when``
    on both legs before a single event is inspected, because a blocked or
    clear answer is only honest once the data is known to reach that far.

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
        calendar: The coverage available for both legs. `coverage_gap` decides
            per leg whether it reaches ``when`` at all before this function
            considers a single event on either one.
        config: Supplies the blackout minutes.

    Returns:
        ``(True, reason)`` when blocked. ``(False, None)`` when the data
        covers ``when`` for both legs and neither carries a qualifying event:
        genuinely clear, and the only outcome the pre-trade checklist's news
        box may be ticked on. ``(None, reason)`` when `coverage_gap` reports a
        gap on either leg: the guard could not determine an answer, ``reason``
        names why using `CoverageGap`'s categories, and this must not be read
        as clear.

    Raises:
        ValueError: If ``pair`` is not six characters or carries a leg that is
            not a G10 currency, if any event in ``calendar`` has a naive
            ``scheduled_for``, or if ``when`` or ``calendar.covers_through`` is
            naive. The last comes from the `coverage_gap` call this makes
            before it reads a single event.

    Three limits of the answer, stated because a caller cannot see them.

    **A window opens before its event.** It opens
    ``calendar_blackout_before_min`` ahead, so a release scheduled just past
    ``calendar.covers_through`` has a window reaching back inside the covered
    span. This function compares the coverage against ``when`` alone, so such a
    release is invisible to it and the answer can be ``(False, None)`` for a
    moment a wider payload would have shown as blocked. The exposure is narrow,
    because `CalendarSource.horizon` is drawn from the events actually present
    rather than from the calendar week, so the unseen releases are next week's.
    Widening the comparison would mark the last half hour of every payload
    unknown, including moments that are genuinely answerable, so the fix belongs
    to the ``covers_through`` contract rather than to this function.

    **A global event never blocks.** An event whose currency is ``GLOBAL``,
    which is how `fbe.datasources.calendar` records the feed's "All" country,
    matches neither leg. That makes `HIGH_IMPACT_KEYWORDS["geopolitical"]`
    unreachable on real input, since a G20 summit arrives global rather than
    attributed to a currency. Whether a global event should block every pair is
    a decision rather than an oversight, and it is recorded as one rather than
    taken here.

    **The reason names the first blocking event, not the nearest.** First in
    ``calendar.events`` order, base leg before quote. The string reaches
    ``PairBias.blockers`` and the journal, so a row can name the euro release
    when the payrolls print thirty minutes later is the one that kept the
    window shut.

    **Containment is inclusive at both ends.** ``opens <= when <= closes``, so
    an entry timed to the exact instant a window closes is blocked. The
    selection window in `CalendarSource.events` is half-open at the top,
    ``start <= event.scheduled_for < end``, so the two conventions differ by one
    instant. Both are right for their own job: a selection window that is asked
    for repeatedly needs the open end so consecutive runs do not return one
    event twice, while a no-trade window is safer closed than open. The
    difference is recorded here because the two read alike and neither side
    says so.

    """
    base, quote = _checked_legs(pair)

    # Every event's shape, before the coverage question. A naive time is a
    # broken feed rather than a fact about this pair, so the refusal is not
    # conditional on the event happening to sit on a leg of it.
    for event in calendar.events:
        _require_aware(event.scheduled_for, f"{event.currency} {event.title}")

    # No naive check of ``when`` here. `coverage_gap` makes it, and it is the
    # first thing this function calls for every pair, so a second check would
    # be a line no test could distinguish from its absence. The ordering that
    # makes it reachable is itself pinned, by
    # ``test_the_coverage_check_runs_before_any_event_is_inspected``.

    # One call, before a single event is read. `coverage_gap` takes no
    # currency, so it answers for the whole calendar rather than per leg, and
    # the calendar handed over is one payload covering both. An earlier version
    # of this ran the identical call once per leg to look like the per-leg check
    # the criterion asks for, which is a loop whose second iteration cannot
    # differ from its first. If coverage ever becomes per-currency its signature
    # changes and this line changes with it.
    gap = coverage_gap(calendar, when)
    if gap is not None:
        category, reason = gap
        return None, f"{category.value}: {reason}"

    for leg in (base, quote):
        for event in calendar.events:
            if event.currency != leg:
                continue
            # No `is_high_impact` filter here. `blackout_windows` applies it and
            # returns nothing for an event that does not qualify, so the loop
            # below simply does not run. A second filter would be a line no test
            # could distinguish from its absence, and two places deciding what
            # qualifies is how the two drift apart.
            # This event's own window rather than the merged set. Merging is for
            # display: a merged window contains ``when`` exactly when one of the
            # windows it was built from does, and only the individual event can
            # say which release to name in the reason.
            for opens, closes in blackout_windows([event], config):
                if opens <= when <= closes:
                    return True, _describe(event)
    return False, None


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

    This function's ``None`` and `is_blacked_out`'s ``(None, reason)`` describe
    the same gap and must not describe it two different ways. Both mean "the
    data does not reach far enough to answer", and this function has meant
    that since before `is_blacked_out` grew a matching third outcome. It still
    takes a bare ``Sequence[CalendarEvent]`` rather than a `CalendarCoverage`,
    so unlike `is_blacked_out` it cannot distinguish *why* the search ran out,
    only that it did; a caller holding a `CalendarCoverage` should read
    `coverage_gap` for the reason and treat this function's ``None`` as
    confirmation, not as a second source of truth.

    """
    raise NotImplementedError(
        "fbe.calendar_guard.next_clear_time is scaffolded; see docs/roadmap.md Phase 4"
    )


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

    Three limits come with the signature, stated here because a reader holding
    ``(HOLD, None)`` cannot see any of them in the value.

    **A bare sequence cannot say whether it was ever fetched.** Unlike
    `is_blacked_out` this takes a ``Sequence[CalendarEvent]`` rather than a
    `CalendarCoverage`, so an empty sequence from a failed scrape and a
    genuinely quiet morning both return ``(HOLD, None)``. That is a fail-open
    policy, and `docs/decisions/0002-representing-not-known.md` declines to
    choose the direction while requiring the two cases to be tellable apart.
    Here they are not. A caller holding a `CalendarCoverage` must read
    `coverage_gap` itself before acting on HOLD. Whether the signature should
    change is on issue #199 beside the `next_clear_time` question rather than
    decided here.

    **A global event never acts.** An event whose currency is ``GLOBAL``,
    which is how `fbe.datasources.calendar` records the feed's "All" country,
    matches neither leg, exactly as in `is_blacked_out`. A G20 summit inside
    the window leaves a position up 0.1R on HOLD.

    **The reason names the first event in range, not the nearest.** Base leg
    before quote, and within a leg first in ``events`` order. The action is the
    same whichever of them is named, so this decides only which release the
    journal shows, and it is fixed rather than left to the order the caller
    happened to pass.

    Args:
        pair: Six-character pair the position is in.
        when: Time of the decision, timezone-aware UTC. Normally now, or the
            moment the next window opens.
        events: Calendar events on either leg.
        config: Supplies the blackout minutes.
        unrealised_r: Open profit in R multiples, positive for profit, measured
            against the position's ``realised_risk_amount`` so it matches what
            the journal will later record. Defaults to 0.0, which falls below
            `TIGHTEN_BUFFER_R`, so a caller that does not track open profit is
            answered FLATTEN. Pass the figure rather than relying on that: the
            default is conservative in one direction only, and a position
            actually up 2.0R whose caller omitted the argument is told to
            close, which is the cost the paragraphs above say this function
            exists to avoid. 0.0 is also a reading a live position genuinely
            holds, so it cannot double as a marker for "not tracked" under
            `docs/decisions/0002-representing-not-known.md` rule 1.

    Returns:
        ``(action, reason)``. The reason names the event whenever one is in
        range, in the shape `is_blacked_out` uses, so an early exit can be
        explained later from the same string. It is ``None`` for
        `OpenPositionAction.HOLD`, which, per the first limit above, covers
        both a clear calendar and one that was never fetched.
        `fbe.journal.TradeRecord.exit_reason` has no value for a news flatten
        today, so the reason currently reaches the journal as free-text
        ``notes`` and the grouping `TIGHTEN_BUFFER_R` asks for is not yet
        available.

    Raises:
        ValueError: If ``when`` is naive, ``pair`` is malformed, an event
            carries a naive ``scheduled_for``, or ``unrealised_r`` is not
            finite. The last of those is not a fact about the trade either: a
            NaN fails every comparison, so it would fall past the buffer test
            into FLATTEN and read as a decision to close a live position when
            it is the absence of one. `fbe.risk.position_size` refuses a
            non-finite ``risk_fraction`` for the same reason.

    Note:
        This is advisory. It never sends an order. The owner executes, which
        keeps the decision where the plan puts it and keeps this module honest
        about being a filter rather than a trading system.

    """
    base, quote = _checked_legs(pair)
    _require_aware(when, "when")
    if not isfinite(unrealised_r):
        raise ValueError(
            f"unrealised_r must be finite, got {unrealised_r!r}. A NaN fails "
            "every comparison, so it would fall through to FLATTEN and read as "
            "an instruction to close a live position that the guard reached by "
            "refusing to answer rather than by deciding."
        )

    # Materialised before it is read, as `blackout_windows` does and for the
    # same reason: this function walks ``events`` once per leg after walking it
    # once for shape, and a generator would leave the later passes empty. The
    # answer would be HOLD on a morning holding two high-impact releases.
    events = tuple(events)

    # Every event's shape, before any of them is matched against a leg. A naive
    # time is a broken feed rather than a fact about this pair, the same
    # ordering `is_blacked_out` uses and for the same reason.
    for event in events:
        _require_aware(event.scheduled_for, f"{event.currency} {event.title}")

    for leg in (base, quote):
        for event in events:
            if event.currency != leg:
                continue
            # This event's own window rather than the merged set, as
            # `is_blacked_out` does: a merged window contains ``when`` exactly
            # when one of its parts does, and only the individual event can say
            # which release to name.
            for opens, closes in blackout_windows([event], config):
                if opens <= when <= closes:
                    reason = _describe(event)
                    # Read from the module global at call time rather than
                    # captured, so moving the threshold moves this decision.
                    if unrealised_r >= TIGHTEN_BUFFER_R:
                        return OpenPositionAction.TIGHTEN, reason
                    return OpenPositionAction.FLATTEN, reason

    # Nothing in range. Not "the calendar said nothing", which is
    # `coverage_gap`'s question and `is_blacked_out`'s to ask: flattening on an
    # empty sequence would close every position the first time a fetch failed.
    return OpenPositionAction.HOLD, None
