"""Economic calendar: the news blackout the trading plan requires.

The plan is explicit. "Avoid trading during high-impact news events to minimize
exposure to excessive volatility and unpredictable price movements", and its
daily routine opens by "reviewing the financial news and economic calendar".
This module supplies the machine-readable half of that so the check happens
every run instead of when the trader remembers.

What it feeds
-------------
`CalendarEvent` objects, consumed by the execution layer to set
``PairBias.tradeable`` and to fill ``TradeIdea.blackout_until``. The windows
come from `DataConfig.calendar_blackout_before_min` and
``calendar_blackout_after_min``, defaulting to 30 minutes before and 60 after.
An event on either leg of a pair blacks out the pair: a EUR release moves
EURUSD whether or not the dollar has anything scheduled.

Whether a moment is inside a blackout window is decided by
`fbe.calendar_guard.is_blacked_out`, not by this class. This source's job ends
at producing `events` and, once wired up, a `fbe.calendar_guard.CalendarCoverage`
naming how far those events reach and whether the fetch that produced them
succeeded. A rate-limited export or a Friday run holding only the current week
must be visible as `CalendarCoverage.fetch_ok = False` or a short
`covers_through`, not as a bare `True`/`False` blackout flag that reads a
failed or partial fetch the same as a genuinely clear one.

The feed
--------
``https://nfs.faireconomy.media/ff_calendar_thisweek.json``. Verified live:
HTTP 200, ``application/json``, an array of objects with exactly six keys.

    {
      "title": "ANZ Job Advertisements m/m",
      "country": "AUD",
      "date": "2026-09-06T21:30:00-04:00",
      "impact": "Low",
      "forecast": "",
      "previous": "0.8%"
    }

Schema notes, each confirmed against a live payload:

* ``country`` holds an ISO 4217 currency code, not a country code, despite the
  name. Codes seen include the G10 set plus ``CNY`` and the literal ``"All"``
  for events with no single currency.
* ``impact`` is one of ``High``, ``Medium``, ``Low`` or ``Holiday``. Capitalised
  exactly like that. ``Holiday`` is not an impact level at all, it is a market
  closure, and treating it as low impact means trading into a thin book.
* ``date`` is ISO 8601 with an explicit offset, ``-04:00`` in the payload
  checked, which is US Eastern. The offset tracks US daylight saving, so parse
  it rather than assuming, then convert to UTC before comparing to anything.
* ``forecast``, ``previous`` and ``actual`` are display strings, not numbers:
  ``"0.8%"``, ``"-1.2K"``, ``"3.75%"``, or empty. ``actual`` is absent until the
  release lands, which is why `CalendarEvent` types all three as ``str | None``.
* A week typically carries 80 to 100 rows across all currencies.

Terms and status
----------------
This feed is unofficial. It is not affiliated with, endorsed by, or sponsored by
Forex Factory or Fair Economy, Inc. Only ``ff_calendar_thisweek`` exists at that
host; ``nextweek``, ``lastweek`` and ``thismonth`` all return 404, verified. The
same week is also published as ``.xml`` and ``.csv`` at the same path, both
verified live.

The publisher rate-limits calendar exports by IP and returns a "Request Denied"
page when a caller exceeds it. Their guidance is to download once a week and
work from the copy. So: fetch at most once a day, cache aggressively, and never
poll it on a timer. The cache TTL for this source should be measured in hours,
not minutes.

Because the feed only covers the current week, the blackout check is blind to
anything from Sunday onwards. A Friday run cannot see Monday's events. Fetch
early in the week and keep the cached copy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime

from fbe.config import DataConfig
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy
from fbe.datasources.registry import SeriesRef
from fbe.types import CalendarEvent, Observation

__all__ = [
    "AVOID_PATTERNS",
    "BLACKOUT_IMPACTS",
    "CURRENCY_MAP",
    "FEED_URL",
    "IMPACT_LEVELS",
    "RATE_LIMIT",
    "CalendarSource",
]


FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
"""Verified live. The only week available; siblings return 404."""

FEED_URL_XML = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FEED_URL_CSV = "https://nfs.faireconomy.media/ff_calendar_thisweek.csv"
"""Same week, other encodings. Both verified live. JSON is the default here
because it needs no schema guessing."""

IMPACT_LEVELS: tuple[str, ...] = ("High", "Medium", "Low", "Holiday")
"""Exact strings the feed emits, case included. ``Holiday`` marks a market
closure rather than a release."""

BLACKOUT_IMPACTS: frozenset[str] = frozenset({"High"})
"""Impact levels that trigger a blackout by default.

Only ``High``. The plan says high-impact events, and widening this to ``Medium``
would black out most of the London session on most days, which for a trader
already limited to a few positions would mean never trading. ``Holiday`` is
handled separately: it thins liquidity rather than spiking it, so it belongs in
a liquidity check, not a volatility blackout."""

CURRENCY_MAP: Mapping[str, str] = {
    "USD": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
    "JPY": "JPY",
    "CHF": "CHF",
    "CAD": "CAD",
    "AUD": "AUD",
    "NZD": "NZD",
    "All": "GLOBAL",
}
"""Feed ``country`` codes mapped onto the scored universe.

The feed already uses ISO 4217 codes, so seven of these are identity mappings
and exist only so the parser fails loudly on an unexpected code rather than
passing it through. ``"All"`` becomes ``"GLOBAL"``, matching the convention
`Observation.currency` documents.

Codes outside this mapping are dropped. ``CNY`` appears regularly and matters
for AUD and NZD through the commodity channel, but a Chinese release is not a
reason to stand aside from a G10 pair, and the plan's list does not include
one."""

AVOID_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "nfp": ("non-farm employment change", "nonfarm payrolls", "employment change"),
    "rate_decision": (
        "federal funds rate",
        "main refinancing rate",
        "official bank rate",
        "official cash rate",
        "overnight rate",
        "policy rate",
        "cash rate",
        "snb policy rate",
        "boj policy rate",
        "monetary policy statement",
    ),
    "gdp": ("gdp m/m", "gdp q/q", "gdp y/y", "prelim gdp", "final gdp"),
    "inflation": ("cpi ", "core cpi", "ppi ", "core ppi", "trimmed mean cpi"),
    "retail_sales": ("retail sales", "core retail sales"),
    "employment_other": (
        "claimant count change",
        "average earnings index",
        "unemployment rate",
        "unemployment claims",
        "employment change",
    ),
    "trade_balance": ("trade balance",),
    "central_bank_speech": (
        "speaks",
        "press conference",
        "testimony",
        "monetary policy report hearings",
    ),
    "minutes": ("fomc meeting minutes", "monetary policy meeting accounts"),
    "geopolitical": ("election", "referendum", "summit", "budget"),
}
"""The ten event classes the trading plan names, mapped onto substrings that
appear in real feed titles.

Matching is on a lower-cased title substring. That is crude and it is the right
trade-off: the feed has no event type field, titles are stable in wording but
vary in prefix ("Prelim", "Flash", "Final", "Core"), and a regex tuned to today
would break on the next wording change. The patterns above were drawn from
titles observed in a live payload, including "ECB Press Conference", "Main
Refinancing Rate", "Monetary Policy Statement", "Core CPI y/y",
"SNB Chairman Schlegel Speaks" and "Monetary Policy Report Hearings".

Two deliberate imprecisions. ``central_bank_speech`` matches the bare word
"speaks", which also catches non-central-bank speakers such as heads of
government; that is a false positive the plan's tenth item arguably wants
anyway. And ``geopolitical`` is close to useless, because scheduled political
events are the ones this feed covers worst. The plan's item 10 stays a human
judgement, and the report should say so rather than implying it is handled."""

RATE_LIMIT = RateLimit(requests=4, per_seconds=3600.0, min_interval_seconds=5.0)
"""Four an hour, well inside the publisher's guidance of one download a week.
Exceeding their export limit returns a "Request Denied" page, and the parser
must treat that as an error rather than as an empty week: an empty calendar
reads as "nothing scheduled", which would clear every blackout at once."""


class CalendarSource(BaseDataSource):
    """Fetches the weekly economic calendar and produces `CalendarEvent`s.

    Unusual among the sources here in that it produces no `Observation`s at
    all. It feeds no pillar. It feeds the execution filter, which is why
    `fetch` returns an empty sequence and `events` is the method that matters.

    Attributes:
        name: ``"forexfactory"``, matching the default on `CalendarEvent`.

    """

    name = "forexfactory"
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=2, backoff_seconds=5.0)

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: Effective `DataConfig`, which carries the blackout windows
                and the cache TTL this source leans on heavily.

        """
        super().__init__(config)

    def available(self) -> bool:
        """Report whether the feed is reachable or a cached copy is on disk.

        A cached week is genuinely usable here, unlike for price data: the
        calendar is a schedule, and a schedule downloaded on Monday is still
        correct on Thursday.

        Returns:
            Whether this source can be used on this run.

        """
        raise NotImplementedError(
            "fbe.datasources.calendar.CalendarSource.available is scaffolded; "
            "see docs/roadmap.md Phase 4"
        )

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Return no observations. This source feeds the filter, not a pillar.

        Present only to satisfy the `DataSource` protocol so the collector can
        treat every source alike.

        Args:
            indicators: Ignored.
            currencies: Ignored.
            start: Ignored.
            end: Ignored.

        Returns:
            An empty sequence, always.

        """
        raise NotImplementedError(
            "fbe.datasources.calendar.CalendarSource.fetch is scaffolded; "
            "see docs/roadmap.md Phase 4"
        )

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return an empty mapping. This source owns no registry entries.

        Returns:
            An empty mapping.

        """
        raise NotImplementedError(
            "fbe.datasources.calendar.CalendarSource.refs is scaffolded; "
            "see docs/roadmap.md Phase 4"
        )

    def events(
        self,
        currencies: Iterable[str],
        start: datetime,
        end: datetime,
        min_impact: str = "High",
    ) -> Sequence[CalendarEvent]:
        """Return scheduled events for the requested currencies and window.

        Args:
            currencies: ISO 4217 codes. Events mapping to ``"GLOBAL"`` are
                returned for every request, since an event with no single
                currency can move any of them.
            start: Window start, timezone-aware.
            end: Window end, timezone-aware.
            min_impact: Lowest impact level to include, from `IMPACT_LEVELS`.

        Returns:
            Events with ``scheduled_for`` in UTC, ordered by time.

        Raises:
            SourceError: On repeated request failure, or when the body is the
                publisher's "Request Denied" page rather than the feed.

        """
        raise NotImplementedError(
            "fbe.datasources.calendar.CalendarSource.events is scaffolded; "
            "see docs/roadmap.md Phase 4"
        )

    def blackout_windows(
        self,
        events: Sequence[CalendarEvent],
    ) -> Mapping[str, Sequence[tuple[datetime, datetime]]]:
        """Turn events into per-currency intervals during which not to trade.

        Overlapping windows are merged, so a currency with three releases
        thirty minutes apart yields one long window rather than three that a
        caller has to reconcile.

        Args:
            events: Events from `events`.

        Returns:
            Currency to ``(start, end)`` intervals in UTC, ordered and merged.

        """
        raise NotImplementedError(
            "fbe.datasources.calendar.CalendarSource.blackout_windows is scaffolded; "
            "see docs/roadmap.md Phase 4"
        )

    def classify(self, title: str) -> tuple[str, ...]:
        """Match an event title against the plan's ten classes to avoid.

        Args:
            title: The feed's ``title`` field, verbatim.

        Returns:
            The `AVOID_PATTERNS` keys this title matched, empty when none. A
            title can match more than one, and "Unemployment Rate" on an NFP
            Friday legitimately matches two.

        """
        raise NotImplementedError(
            "fbe.datasources.calendar.CalendarSource.classify is scaffolded; "
            "see docs/roadmap.md Phase 4"
        )

    # Whether a pair is inside a blackout window at a given moment is answered
    # by fbe.calendar_guard.is_blacked_out, which returns three outcomes
    # (blocked, clear, unknown) rather than the bare bool this class used to
    # return here. A bool cannot represent "the fetch that produced these
    # events failed" or "this cache does not reach that far", both of which
    # this class can now report through CalendarCoverage, so the check itself
    # does not belong on this class a second time. See issue #43.
