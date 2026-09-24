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
* A week carries roughly 100 rows across all currencies. The payload captured
  for the tests holds 105, so treat a hundred as the middle of the range rather
  than the top of it.

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
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from fbe.config import DataConfig
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy, SourceError
from fbe.datasources.registry import SeriesRef
from fbe.types import CalendarEvent, Observation

__all__ = [
    "AVOID_PATTERNS",
    "BLACKOUT_IMPACTS",
    "CURRENCY_MAP",
    "FEED_URL",
    "IMPACT_LEVELS",
    "IMPACT_SEVERITY",
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
closure rather than a release.

`fbe.types.CalendarEvent.impact` states the same four values and names this
constant as the authority, so a value the feed adds lands here first and the
shared vocabulary follows. It described the field as ``"high"``, ``"medium"``
or ``"low"``, lower case and without ``Holiday``, until #182 corrected it.
`docs/data-sources.md` records the four capitalised, and a `CalendarEvent`
built here carries them unchanged.

Two consequences worth knowing before the guard is written.
`fbe.calendar_guard.is_high_impact` already specifies a case-insensitive
comparison, so it is safe. `fbe.cli.Impact` spells the three in lower case, which
is why `CalendarSource.events` folds the case of ``min_impact`` rather than
making that caller discover the mismatch as a crash. A consumer that compares
``event.impact == "high"`` would match nothing and report every week as clear,
whichever file it was written against.
"""

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

IMPACT_SEVERITY: Mapping[str, int] = {"High": 3, "Medium": 2, "Low": 1}
"""How the three release severities order, for the ``min_impact`` filter.

``Holiday`` is deliberately absent. It is in `IMPACT_LEVELS` because the feed
emits it, not because it is a severity: a closure thins the book rather than
spiking it, so ordering it below ``Low`` would hand a caller asking for
everything down to low impact a set of market closures dressed as releases. A
caller wanting closures asks for them by name, which is the only way to get
them.
"""

DEFAULT_MIN_IMPACT = min(BLACKOUT_IMPACTS, key=lambda level: IMPACT_SEVERITY[level])
"""The ``min_impact`` `CalendarSource.events` uses when a caller names none.

Derived from `BLACKOUT_IMPACTS` rather than written again, because the two are
the same decision: the least severe level that triggers a blackout is the least
severe level worth returning by default. Written twice they drift, and the
drift is silent in the safe-looking direction: `BLACKOUT_IMPACTS` widened to
include ``Medium`` while the default stayed ``High`` would mean the blackout
policy says one thing and every call that does not override it does another.
"""

_DENIED_MARKER = "request denied"
"""Substring identifying the publisher's rate-limit page, lower-cased.

Matched on the body rather than on the status, because that page is served with
HTTP 200. See `CalendarSource._decode`.
"""

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
    base_url = f"{urlsplit(FEED_URL).scheme}://{urlsplit(FEED_URL).netloc}"
    feed_path = urlsplit(FEED_URL).path
    """Split out of `FEED_URL` rather than written twice.

    `BaseDataSource._request` takes a path against ``base_url``, and `FEED_URL`
    is the constant the docstring, the tests and `docs/data-sources.md` all
    name. Deriving both from it means a change to the host or the filename
    cannot leave the two disagreeing, which would show up as a 404 rather than
    as anything a reader could trace back here.
    """

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
            Whether this source can be used on this run. Online, always
            ``True``: the feed needs no credential and no directory, so there
            is nothing about the configuration that could rule it out. Offline,
            ``True`` only when the cache holds a week this source can still
            read.

        This asks about configuration rather than connectivity, which is the
        contract `BaseDataSource.available` sets and
        `tests/test_datasource_base.py` enforces across every source: no
        implementation here may make a request. So an unreachable feed is not
        reported here, it is reported by `events` raising, which is the only
        place that distinction can be made without a network call.

        The offline branch decodes the stored body rather than merely checking
        that a file exists, which catches the case this source cares about: a
        rate-limit page left on disk by an older version or put there by hand
        would otherwise answer ``True`` and promise a week that `events` then
        refuses. It catches bodies that fail to decode and nothing more. A
        cached body that is valid JSON but not an array still answers ``True``
        here and raises in `events`, because the array check lives in `_week`
        where the parse happens. That gap is narrow and deliberate: moving the
        whole parse into this method would make an availability check as
        expensive as a fetch.

        Does not raise. A source asked whether it is usable cannot answer by
        failing, and every way this can fail is already a ``False``.

        """
        if not self.config.offline:
            return True
        try:
            self._request(self.feed_path, {})
        except SourceError:
            return False
        return True

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
            An empty sequence, always. Not a coverage gap and not a failure:
            this source has no observations to give, which is different from
            having none today.

        """
        return ()

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return an empty mapping. This source owns no registry entries.

        Returns:
            An empty mapping. The feed is one weekly URL rather than a set of
            per-currency series, so there is nothing for the registry to hold
            and nothing for `coverage_report` to measure.

        """
        return {}

    def events(
        self,
        currencies: Iterable[str],
        start: datetime,
        end: datetime,
        min_impact: str = DEFAULT_MIN_IMPACT,
    ) -> Sequence[CalendarEvent]:
        """Return scheduled events for the requested currencies and window.

        Args:
            currencies: ISO 4217 codes. Events mapping to ``"GLOBAL"`` are
                returned for every request, since an event with no single
                currency can move any of them.
            start: Window start, timezone-aware.
            end: Window end, timezone-aware.
            min_impact: Lowest impact level to include, from `IMPACT_LEVELS`.
                Matched without regard to case, because the feed capitalises
                these and `fbe.cli.Impact` spells the same three in lower case.
                An unrecognised level still raises: the fold is about the two
                spellings already in this codebase, not about accepting
                anything.

        Returns:
            Events with ``scheduled_for`` in UTC, ordered by time.

            The window is closed at ``start`` and half-open at ``end``, so an
            event exactly at ``start`` is inside and one exactly at ``end``
            belongs to the next window. Without that, a caller walking a week in
            day-long steps would count a midnight release twice and black out
            two days for one event.

            An empty sequence means no event matched, which is data. Every way
            this can fail raises instead, and that separation is the whole point
            of the module: an empty calendar clears every blackout at once.

            **The filter is on the event instant, not on its blackout window,
            and a caller composing this with `blackout_windows` has to widen
            ``start`` itself.** A release already past can still be inside its
            own window: at the default 60 minutes after, an 12:30 print is still
            blacked out at 13:00, and a caller asking from 13:00 forward will not
            receive it and will see no window for it. Widen ``start`` by
            `DataConfig.calendar_blackout_after_min` before handing the result
            to `blackout_windows`. This method cannot do it, because it does not
            know whether the caller wants windows or the schedule.

        Raises:
            SourceError: On repeated request failure, or when the body is the
                publisher's "Request Denied" page rather than the feed.
            ValueError: If ``min_impact`` is not in `IMPACT_LEVELS`. A typo
                there would otherwise match nothing and report a clear week.

        """
        canonical = {level.lower(): level for level in IMPACT_LEVELS}
        resolved = canonical.get(min_impact.lower())
        if resolved is None:
            raise ValueError(
                f"min_impact must be one of {IMPACT_LEVELS}, got {min_impact!r}"
            )

        # Every rejection below answers a caller mistake that would otherwise
        # render as a week with nothing in it, which is the one answer this
        # module may never give by accident. `_event` refuses an impact it does
        # not know for the same reason; these are that rule applied to the
        # request rather than to the payload.
        wanted = set(currencies)
        known = set(CURRENCY_MAP.values())
        unknown = sorted(wanted - known)
        if unknown:
            raise ValueError(
                f"events() asked about {unknown}, which are not currencies this "
                f"feed is mapped onto. Expected codes from {sorted(known)}, so a "
                "pair such as 'EURUSD' or a lower-cased code arrives here as a "
                "clear calendar rather than as a mistake."
            )

        for label, moment in (("start", start), ("end", end)):
            if moment.tzinfo is None:
                raise ValueError(
                    f"{label} must be timezone-aware; a naive bound compares "
                    "against nothing on an empty week and raises TypeError on a "
                    "full one, so the same caller error renders two ways"
                )
        if end <= start:
            raise ValueError(
                f"end must be after start, got start={start} end={end}. "
                "Transposed bounds match no event and report a clear week."
            )
        floor = IMPACT_SEVERITY.get(resolved)
        selected = [
            event
            for event in self._week()
            if (event.currency in wanted or event.currency == CURRENCY_MAP["All"])
            and start <= event.scheduled_for < end
            and (
                event.impact == resolved
                if floor is None
                else IMPACT_SEVERITY.get(event.impact, 0) >= floor
            )
        ]
        return tuple(sorted(selected, key=lambda event: event.scheduled_for))

    def horizon(self) -> datetime | None:
        """Report the latest moment this source's current data can vouch for.

        Returns:
            The instant of the last event the held week carries, in UTC.
            ``None`` when this source can vouch for no moment, which happens two
            ways: a fetch that failed with no cached week underneath it, and a
            fetch that succeeded on a week holding no event this universe
            scores. Both are genuinely "no coverage" and neither is a horizon,
            so both answer the same way.

            Which of the two happened is not this method's to report and cannot
            be read off the return value. `events` raises on the first and
            returns an empty sequence on the second, and that is where the guard
            tells them apart before it fills `CalendarCoverage.fetch_ok`. A
            reader who sees ``None`` here and concludes the fetch failed will be
            wrong some of the time.

        Why this is not the end of the calendar week. The feed carries no field
        saying how far it runs, so the only thing this source has evidence for
        is the span it actually holds. Reporting a full week from a payload that
        ends on Tuesday would be claiming coverage of three days it has never
        seen, and a caller would then read Thursday as clear.

        Why ``None`` rather than a date in the past or the present. Both of
        those are claims. A horizon in the past reads as a week that has
        expired, and one at the current instant reads as coverage of now and no
        further. Neither is true of a source holding nothing, and ADR 0002 is
        about saying so rather than picking the least wrong number.

        Why this does not raise where `events` does. The question is how far the
        coverage reaches, and "nowhere" is a real answer to it. `events` is
        asked for the schedule itself, where an empty answer and a failure are
        the two things that must never render alike. `fbe.calendar_guard` reads
        both, and builds the `CalendarCoverage` that carries them together: this
        module does not import it, because `datasources` sits upstream of the
        guard and that edge is deliberately absent in both directions.

        """
        try:
            week = self._week()
        except SourceError:
            return None
        if not week:
            return None
        return max(event.scheduled_for for event in week)

    def _week(self) -> Sequence[CalendarEvent]:
        """Fetch and parse the published week, unfiltered.

        Returns:
            Every event the feed carries whose currency is in `CURRENCY_MAP`,
            in the order the publisher wrote them. Callers sort; this does not,
            so that `horizon` and `events` read the same set.

        Raises:
            SourceError: On a failed fetch, a body that is not the feed, or a
                row this module cannot read.

        """
        payload = self._request(self.feed_path, {})
        if not isinstance(payload, list):
            raise SourceError(
                f"{self.name} returned {type(payload).__name__} where the feed "
                "publishes an array of events"
            )
        parsed = [self._event(entry) for entry in payload]
        return tuple(event for event in parsed if event is not None)

    def _event(self, entry: object) -> CalendarEvent | None:
        """Turn one feed row into an event, or ``None`` to drop it.

        Args:
            entry: One element of the decoded payload.

        Returns:
            The event, or ``None`` when its ``country`` is outside
            `CURRENCY_MAP`. That drop is the only one: a code this universe
            does not score is not a reason to stand aside from a G10 pair.

        Raises:
            SourceError: On a row that is not an object, a missing required
                key, an unparseable ``date``, a naive ``date`` with no offset,
                or an ``impact`` outside `IMPACT_LEVELS`. Every one of those is
                a schema change rather than a row to skip, and skipping would
                remove events from the blackout while reporting a clear week.

        """
        if not isinstance(entry, dict):
            raise SourceError(
                f"{self.name} returned a row that is not an object: "
                f"{type(entry).__name__}"
            )

        try:
            title = str(entry["title"])
            country = str(entry["country"])
            stamp = str(entry["date"])
            impact = str(entry["impact"])
        except KeyError as missing:
            raise SourceError(
                f"{self.name} returned a row without {missing}"
            ) from missing

        currency = CURRENCY_MAP.get(country)
        if currency is None:
            return None

        if impact not in IMPACT_LEVELS:
            raise SourceError(
                f"{self.name} published impact {impact!r} for {title!r}, which "
                f"is not one of {IMPACT_LEVELS}"
            )

        try:
            when = datetime.fromisoformat(stamp)
        except ValueError as error:
            raise SourceError(
                f"{self.name} published an unreadable date for {title!r}: {error}"
            ) from error
        if when.tzinfo is None:
            # The publisher writes an explicit offset on every row. One without
            # is a schema change, and assuming a zone would put the event out
            # by whatever the real offset was, in one direction, silently.
            raise SourceError(
                f"{self.name} published {title!r} with no UTC offset, so its "
                "instant cannot be determined"
            )

        return CalendarEvent(
            title=title,
            currency=currency,
            scheduled_for=when.astimezone(UTC),
            impact=impact,
            source=self.name,
            forecast=_display(entry.get("forecast")),
            previous=_display(entry.get("previous")),
            actual=_display(entry.get("actual")),
        )

    def _decode(self, body: bytes) -> object:
        """Decode the feed, refusing the publisher's rate-limit page.

        Args:
            body: Raw response bytes.

        Returns:
            The decoded JSON, as the base does.

        Raises:
            SourceError: When the body is the "Request Denied" page, or when it
                is not JSON at all.

        The base already refuses a non-JSON body, so the denied page would raise
        either way. This override exists for the message: "not JSON" sends a
        reader looking for a parser bug, and the actual cause is the export
        limit, whose fix is to wait and use the cached week rather than to retry.

        Refusing here rather than one level up is what keeps the page off disk.
        `BaseDataSource._request` writes the cache only after decoding succeeds,
        and an offline run expires nothing, so a denied body cached once would
        serve itself back until somebody deleted the file by hand.

        """
        if _DENIED_MARKER in body[:2048].decode("utf-8", "replace").lower():
            raise SourceError(
                f"{self.name} returned the publisher's rate limit page rather "
                "than the calendar. Their guidance is to download once a week "
                "and work from the copy, so wait rather than retrying."
            )
        return super()._decode(body)

    def blackout_windows(
        self,
        events: Sequence[CalendarEvent],
    ) -> Mapping[str, Sequence[tuple[datetime, datetime]]]:
        """Turn events into per-currency intervals during which not to trade.

        Overlapping windows are merged, so a currency with three releases
        thirty minutes apart yields one long window rather than three that a
        caller has to reconcile.

        Args:
            events: Events from `events`. Whatever it returned, which is only
                the events inside the window that was asked for. If that window
                began at the current moment, a release already past is not in
                it, and no window is produced for it even though its own is
                still open. `events` documents the widening a caller owes; this
                method receives what it is given and cannot tell that anything
                is missing.

        Returns:
            Currency to ``(start, end)`` intervals in UTC, ordered and merged.
            The widths come from `DataConfig.calendar_blackout_before_min` and
            ``calendar_blackout_after_min``, 30 and 60 minutes by default, so a
            run that widens them in config widens these without a change here. A
            currency with no events is absent from the mapping rather than
            present with an empty sequence: nothing scheduled is not a window of
            zero length.

        Windows merge within a currency and never across. Whether a *pair* is
        blacked out by one of its legs is `fbe.calendar_guard`'s question, and it
        reads this mapping to answer it, so merging across currencies here would
        take that decision away from the guard and make every release global.

        The live feed produces the overlapping case on any FOMC day: the
        statement, the rate and the projections land on one instant with the
        press conference thirty minutes later, which is four events and one
        window.

        """
        before = timedelta(minutes=self.config.calendar_blackout_before_min)
        after = timedelta(minutes=self.config.calendar_blackout_after_min)

        spans: dict[str, list[tuple[datetime, datetime]]] = {}
        for event in events:
            spans.setdefault(event.currency, []).append(
                (event.scheduled_for - before, event.scheduled_for + after)
            )

        return {currency: _merge(windows) for currency, windows in spans.items()}

    def classify(self, title: str) -> tuple[str, ...]:
        """Match an event title against the plan's ten classes to avoid.

        Args:
            title: The feed's ``title`` field, verbatim.

        Returns:
            The `AVOID_PATTERNS` keys this title matched, empty when none. A
            title can match more than one, and "Unemployment Rate" on an NFP
            Friday legitimately matches two.

            Matching folds the case, because the feed capitalises titles and
            `AVOID_PATTERNS` is written lower case. Without the fold this would
            return nothing for every real title, which is a classifier that
            never fires and a report showing no class against any event.

        The keys come back in `AVOID_PATTERNS` order rather than in the order
        they happened to match, so two runs over one title produce the same
        tuple and a report does not reorder itself between runs.

        """
        folded = title.lower()
        return tuple(
            key
            for key, patterns in AVOID_PATTERNS.items()
            if any(pattern in folded for pattern in patterns)
        )

    # Whether a pair is inside a blackout window at a given moment is answered
    # by fbe.calendar_guard.is_blacked_out, which returns three outcomes
    # (blocked, clear, unknown) rather than the bare bool this class used to
    # return here. A bool cannot represent "the fetch that produced these
    # events failed" or "this cache does not reach that far", both of which
    # this class can now report through CalendarCoverage, so the check itself
    # does not belong on this class a second time. See issue #43.


def _display(value: object) -> str | None:
    """Carry a feed display string through, or mark it absent.

    Args:
        value: The raw ``forecast``, ``previous`` or ``actual`` field, or
            ``None`` when the key was absent.

    Returns:
        The string exactly as published, or ``None`` when the key was missing or
        held the empty string.

    These are for a person to read beside the event and nothing computes with
    them, which is why they stay strings. Parsing would mean deciding what ``K``
    means in ``"-1.2K"``, what a bare ``"3.75%"`` is a percentage of, and what to
    do with ``"<1.25%"``, which the captured payload carries on the BOJ policy
    rate. None of those has an answer this module needs.

    The empty string becomes ``None`` because it is the feed's way of writing a
    figure it has not got, and `fbe.types.CalendarEvent` types all three as
    ``str | None`` so that absence has a marker. Carrying ``""`` through would
    put a blank cell in the report that a reader cannot tell from a figure the
    publisher declined to forecast.

    """
    if value is None:
        return None
    text = str(value)
    return text or None


def _merge(
    windows: Sequence[tuple[datetime, datetime]],
) -> tuple[tuple[datetime, datetime], ...]:
    """Collapse overlapping or touching intervals into the fewest that cover them.

    Args:
        windows: ``(start, end)`` pairs for one currency, in any order.

    Returns:
        The same coverage as the fewest ordered, disjoint intervals. Touching
        intervals merge as well as overlapping ones: a window ending exactly
        when the next begins leaves no tradeable instant between them, so
        reporting two would invite a caller to find a gap that is not there.

    Empty in, empty out, though a currency with no events never reaches this.

    """
    if not windows:
        return ()

    merged: list[tuple[datetime, datetime]] = []
    for opened, closed in sorted(windows):
        if merged and opened <= merged[-1][1]:
            previous_open, previous_close = merged[-1]
            merged[-1] = (previous_open, max(previous_close, closed))
        else:
            merged.append((opened, closed))
    return tuple(merged)
