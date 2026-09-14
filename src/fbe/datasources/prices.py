"""Market prices: spot FX, equity indices, volatility and commodity proxies.

Feeds RISK, which needs to know whether the market is buying risk or selling
it, and EXTERNAL, which needs the terms-of-trade proxies for the three
commodity currencies. Spot FX itself is not scored, since the engine produces a
bias rather than a price forecast, but it is needed to convert a trade balance,
to size a position in the account currency, and to check that a pair's recent
range is worth the spread.

Source order
------------
1. FRED, wherever it carries the series. VIXCLS, SP500, NIKKEI225, DCOILWTICO,
   DCOILBRENTEU, PALLFNFINDEXM, PIORECRUSDM and the DEX* spot rates are all
   verified live and are already covered by `fbe.datasources.fred`. Preferring
   FRED keeps one client, one key and one cache for most of this.
2. The OECD API, for the non-US equity indices, which the registry now takes
   from `fbe.datasources.oecd` rather than FRED. Both publish the same OECD
   share price index; the OECD's own copy runs two months ahead. Still monthly.
3. Stooq, for a daily read on those same indices, when it can be made to work.
4. Yahoo Finance, as a last resort, with the caveat below.

Stooq
-----
Endpoint: ``https://stooq.com/q/d/l/?s={symbol}&d1={YYYYMMDD}&d2={YYYYMMDD}&i=d``

It returns a CSV with the header ``Date,Open,High,Low,Close,Volume`` and one row
per session, oldest first. Symbol conventions: FX pairs are six lower-case
letters (``eurusd``), indices carry a leading caret (``^spx``), US equities take
a ``.us`` suffix, and futures take ``.f``. ``i`` sets the interval, ``d`` for
daily, ``w`` weekly, ``m`` monthly. There is no key and no documented rate
limit, but the export is throttled per IP and a caller that polls it hard gets
blocked.

Verification status, stated plainly: the URL shape and symbol conventions above
are confirmed from Stooq's own download pages and from several independent
client implementations, but they could not be confirmed against a live response
from this environment. Stooq now sits behind a JavaScript proof-of-work
challenge that a plain HTTP client does not solve, and every request from here
returned the challenge page or a reset connection rather than CSV. Every symbol
in `STOOQ_SYMBOLS` is therefore marked unverified. Before relying on any of
them, fetch one by hand in a browser and confirm the CSV comes back.

This is also a reason to keep the FRED and OECD paths primary. A source that
can start serving an anti-bot page instead of data is a source that will fail
on a Monday morning, and the engine should degrade to a monthly OECD share
price index rather than to nothing.

Yahoo Finance
-------------
``https://query1.finance.yahoo.com/v8/finance/chart/{symbol}`` returns JSON
quotes and is widely used, but there is no public API and no licence to use it.
Yahoo's terms permit personal use through their own site and prohibit
redistribution and automated scraping; the endpoint is undocumented, unversioned
and has broken without notice more than once. Treat it as an emergency fallback
for a single missing symbol, on a personal account, never as a dependency, and
never in anything published.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta

from fbe.config import DataConfig
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy, SourceError
from fbe.datasources.fred import FredSource
from fbe.datasources.registry import INDICATORS, SeriesRef
from fbe.types import Observation

__all__ = [
    "FRED_COMMODITY_SERIES",
    "FRED_RISK_SERIES",
    "FRED_SPOT_SERIES",
    "MAX_SPOT_STALENESS_DAYS",
    "RATE_LIMIT",
    "SPOT_LOOKBACK_DAYS",
    "STOOQ_CLOSE_COLUMN",
    "STOOQ_CSV_HEADER",
    "STOOQ_CSV_URL",
    "STOOQ_DATE_FORMAT",
    "STOOQ_SYMBOLS",
    "YAHOO_CHART_URL",
    "PricesSource",
]


STOOQ_CSV_URL = "https://stooq.com/q/d/l/"
"""Daily history download. Query: ``s`` symbol, ``d1``/``d2`` dates as
``YYYYMMDD``, ``i`` interval. Returns ``Date,Open,High,Low,Close,Volume``."""

STOOQ_CSV_HEADER = "Date,Open,High,Low,Close,Volume"
"""The first line every genuine response carries.

This is the whole of the anti-bot defence. The challenge page arrives with HTTP
200, so status alone says the request succeeded, and anything that is not this
header is refused rather than parsed. Held as a constant because it is the one
string that decides whether a body is data."""

STOOQ_DATE_FORMAT = "%Y%m%d"
"""How ``d1`` and ``d2`` are written. Not the format of the ``Date`` column in
the response, which is ISO."""

STOOQ_CLOSE_COLUMN = 4
"""Zero-based index of ``Close`` in the response row. Open, high and low are all
present and all plausible, so reading the wrong one produces a number nobody
would question."""

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
"""Undocumented and unlicensed. See the module docstring before using it."""

STOOQ_SYMBOLS: Mapping[str, str] = {
    "USD": "^spx",
    "EUR": "^dax",
    "GBP": "^ukx",
    "JPY": "^nkx",
    "CHF": "^smi",
    "CAD": "^tsx",
    "AUD": "^aor",
    "NZD": "^nz50",
}
"""Daily equity index symbols per currency. UNVERIFIED, every one of them:
Stooq's anti-bot challenge blocked live confirmation from this environment.
Check each in a browser before trusting it, and prefer the FRED equity refs in
the registry until you have."""

FRED_RISK_SERIES: Mapping[str, str] = {
    "vix": "VIXCLS",
    "vix_emerging": "VXEEMCLS",
    "high_yield_spread": "BAMLH0A0HYM2",
    "yield_curve_10y_2y": "T10Y2Y",
    "breakeven_10y": "T10YIE",
    "financial_stress": "STLFSI4",
    "dollar_index_broad": "DTWEXBGS",
}
"""Risk-regime series, all verified live on FRED and all current. VIXCLS is the
headline; the rest are corroboration, and a risk pillar that reads only VIX
will call every equity wobble a crisis."""

FRED_SPOT_SERIES: Mapping[str, str] = {
    "EURUSD": "DEXUSEU",
    "GBPUSD": "DEXUSUK",
    "AUDUSD": "DEXUSAL",
    "NZDUSD": "DEXUSNZ",
    "USDJPY": "DEXJPUS",
    "USDCHF": "DEXSZUS",
    "USDCAD": "DEXCAUS",
}
"""Daily noon spot rates, all verified live. Note the quoting direction: FRED
names each series by its own convention, so ``DEXUSEU`` is dollars per euro
while ``DEXJPUS`` is yen per dollar. The keys above are already in market
convention, which is what `fbe.universe.MAJORS` uses; inverting one of these by
accident silently inverts a bias, so map by key and never by series name.

These are daily fixings, not live quotes. They are fine for sizing and for
converting a balance. They are not an execution price."""

FRED_COMMODITY_SERIES: Mapping[str, str] = {
    "all_commodities": "PALLFNFINDEXM",
    "energy": "PNRGINDEXM",
    "metals": "PMETAINDEXM",
    "food": "PFOODINDEXM",
    "crude_wti": "DCOILWTICO",
    "crude_brent": "DCOILBRENTEU",
    "iron_ore": "PIORECRUSDM",
    "copper": "PCOPPUSDM",
    "coal_australia": "PCOALAUUSDM",
}
"""Commodity proxies, all verified live. ``crude_wti`` is daily; the IMF index
family is monthly and runs about two months behind.

The gap: no dairy price index exists on FRED, so the New Zealand dollar has no
free terms-of-trade proxy. The GlobalDairyTrade auction index is the right
series and is published fortnightly on globaldairytrade.info. ``food`` is a poor
substitute, since the dairy component is a small share of it."""

SPOT_LOOKBACK_DAYS = 30
"""How far back `PricesSource.spot` asks FRED for, in calendar days.

Width matters only to the ``on=None`` path, which has to find the newest fixing
and so needs more than one session in the window. Asking for a named session
reads one row and ignores its neighbours, so the width is irrelevant there.

These are Federal Reserve H.10 noon rates, published on US banking days, so the
gaps are US holidays and weekends rather than the holidays of the other leg:
Golden Week does not suspend ``DEXJPUS``. The longest US run is about four days.
Thirty is therefore generous rather than tight, which is deliberate: the cost of
too narrow is a window with no rows at all, and the cost of too wide is bounded
by `MAX_SPOT_STALENESS_DAYS`, which refuses an answer that is too old whatever
the window held."""

MAX_SPOT_STALENESS_DAYS = 7
"""How old the newest fixing may be before ``spot(pair)`` refuses to answer.

A FRED series can stop updating while continuing to answer requests normally,
which the `fbe.datasources.fred` module docstring records happening to several
series already. Without this bound, ``on=None`` would hand back the last fixing
it could find, up to `SPOT_LOOKBACK_DAYS` old, as though it were current. A rate
weeks stale is the same defect as a conversion rate defaulting to 1.0: it sizes
a position against a number nobody looked at, and nothing downstream can tell.

Seven days rather than four, so an ordinary US holiday week plus its weekends
cannot trip it. Past that, the series has stopped rather than the market having
been shut, and those need different answers from the operator."""

RATE_LIMIT = RateLimit(requests=20, per_seconds=60.0, min_interval_seconds=1.0)
"""Stooq throttles by IP and publishes no limit. One second between requests,
and the whole daily refresh is a handful of symbols."""


class _BodyNotCsv(SourceError):
    """The response body is not a Stooq session history.

    Private, and raised only by `PricesSource._decode`, so `fetch_stooq` can
    tell a body it refused apart from the failures the shared request path
    raises for its own reasons: an exhausted retry, a status that is not worth
    retrying, or an offline run with a cold cache. Those need a different
    answer from the operator, and telling all of them to open a browser and
    check the symbol sends them after the wrong thing.
    """


def _today() -> date:
    """Return the current date.

    A seam, and the only reason it exists: ``spot(pair)`` with no session has to
    ask "as of when", and a test that let that be the real today would pass in
    the week it was written and fail the week after, as the fixture aged past
    `MAX_SPOT_STALENESS_DAYS`. Pinning it here is cheaper and more honest than
    patching the `datetime` module, and keeps the fixed-date rule the rest of
    the suite already follows.

    Returns:
        Today's date in the system's local timezone.

    """
    return date.today()


class PricesSource(BaseDataSource):
    """Fetches market prices for the risk and external pillars.

    Reads FRED first for everything FRED carries, then Stooq for the daily
    non-US equity indices. Yahoo is not wired in: it is documented in the
    module docstring as a manual escape hatch, not as a code path, because a
    dependency with no licence does not belong in a scheduled job.

    Attributes:
        name: ``"stooq"``, matching ``SeriesRef.source`` for the refs it owns.
            The FRED-backed prices are owned by `FredSource` and keep the
            ``"fred"`` key, so no observation ever misreports its origin.

    """

    name = "stooq"
    base_url = STOOQ_CSV_URL
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=2, backoff_seconds=2.0)

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: Effective `DataConfig`. No credential is needed by the
                Stooq endpoint. `spot` reads through FRED, which does take one,
                and carries this same config so there is no second place for a
                key to come from.

        """
        super().__init__(config)
        self._fred_source: FredSource | None = None

    def _fred(self) -> FredSource:
        """Return the FRED source this one reads its spot fixings through.

        Built once and reused, and deliberately not a second HTTP client. The
        run keeps one key, one throttle and one cache for FRED that way, which
        is what the module docstring asks for.

        Returns:
            The source, carrying this run's `DataConfig` and therefore its
            cache directory, TTL, offline flag and credential. The throttle is
            per instance, so a collector holding its own `FredSource` and this
            one would count separately against the same endpoint. That is a
            known limit of building one here rather than being handed one, and
            it is the reason the cache is shared even though the throttle is
            not.

        """
        if self._fred_source is None:
            self._fred_source = FredSource(self.config)
        return self._fred_source

    def close(self) -> None:
        """Release this source's HTTP client and the FRED one it reads through.

        The base only knows about its own client. `spot` goes through a second
        `FredSource`, which opens a client of its own, so closing only the base
        leaks it. Safe to call more than once and safe on a source that never
        made a request.
        """
        super().close()
        if self._fred_source is not None:
            self._fred_source.close()

    def _decode(self, body: bytes) -> object:
        """Parse a Stooq CSV body into session and close pairs.

        The base decodes JSON. This endpoint serves CSV, and a blocked request
        serves the HTML challenge page at HTTP 200.

        The whole parse lives here rather than in `fetch_stooq`, and that
        placement is the point. `BaseDataSource._request` writes the cache only
        after decoding succeeds, so every refusal here keeps the body off disk.
        Checking only the header here and parsing rows one level up would let a
        truncated body pass, be cached, and then fail identically for the whole
        TTL with no network traffic; an offline run expires nothing, so that
        entry would be permanent until someone deleted the file. The cost is
        that this method does not know which symbol was requested, and
        `fetch_stooq` catches and re-raises to add it.

        Args:
            body: Raw response bytes.

        Returns:
            ``(session_date, close)`` pairs, oldest first. Closes carry the
            unit of the symbol they were fetched for, index points or a price
            in the symbol's own currency, which is `SeriesRef.unit` for a
            routed ref. Empty when the body is the header alone, which is a
            window that held no sessions and is data.

        Raises:
            _BodyNotCsv: If the first line is not `STOOQ_CSV_HEADER`, if a row
                is short or unreadable, if a close is not a finite number, or
                if a session date appears twice. Each of those is a body this
                source cannot use, and refusing keeps it out of the cache.

        """
        # utf-8-sig, not utf-8: a byte order mark is not whitespace, so a
        # plain decode leaves it on the first line and the header check
        # would refuse a genuine body as a challenge page.
        text = body.decode("utf-8-sig", errors="replace")
        lines = text.strip().splitlines()

        # Known variant, not handled: Stooq is reported to omit the Volume
        # column for some symbols, which would arrive as a five-field header
        # and be refused here. docs/data-sources.md documents the six-column
        # form and every symbol is unverified, so this follows the spec as
        # written rather than guessing at the variant.
        if not lines or lines[0].strip() != STOOQ_CSV_HEADER:
            first = lines[0].strip()[:60] if lines else "an empty body"
            raise _BodyNotCsv(
                f"expected a body beginning {STOOQ_CSV_HEADER!r} and got {first!r}"
            )

        rows: list[tuple[date, float]] = []
        seen: set[date] = set()
        for number, line in enumerate(lines[1:], start=2):
            if not line.strip():
                continue
            fields = line.split(",")
            if len(fields) <= STOOQ_CLOSE_COLUMN:
                raise _BodyNotCsv(
                    f"a short row at line {number}: {line.strip()[:60]!r}"
                )
            try:
                session = date.fromisoformat(fields[0].strip())
                close = float(fields[STOOQ_CLOSE_COLUMN])
            except ValueError as error:
                raise _BodyNotCsv(
                    f"an unreadable row at line {number}: {error}"
                ) from error

            # float() accepts "nan", "inf" and "-inf" without raising. A nan
            # reaching a cross-sectional pillar makes the mean and the standard
            # deviation nan for the whole universe, so every currency's score
            # becomes nan and every comparison against a threshold silently
            # reads False. An inf clips to the top of the band and hands a
            # currency maximum conviction from nothing. Neither shows up as a
            # coverage gap, which is what makes them worse than a missing row.
            if not math.isfinite(close):
                raise _BodyNotCsv(
                    f"a close that is not a finite number at line {number}: "
                    f"{fields[STOOQ_CLOSE_COLUMN].strip()!r}"
                )

            if session in seen:
                # Two closes for one session is evidence of a concatenated or
                # replayed body. Keeping both would give a pillar two
                # contradictory values for one period and let iteration order
                # decide; keeping one would be a guess about which.
                raise _BodyNotCsv(
                    f"the session {session.isoformat()} appears twice, at line {number}"
                )
            seen.add(session)
            rows.append((session, close))

        # The endpoint publishes oldest first and callers difference these, so
        # the order is sorted rather than trusted.
        rows.sort(key=lambda row: row[0])
        return rows

    def available(self) -> bool:
        """Report whether the Stooq endpoint answers with CSV rather than HTML.

        The check is deliberately about the response shape, not about
        reachability. Stooq's anti-bot layer returns HTTP 200 with a challenge
        page, so a status check alone would report a broken source as healthy
        and the engine would parse the challenge as an empty price series.

        Returns:
            Whether this source can be used on this run.

        """
        raise NotImplementedError(
            "fbe.datasources.prices.PricesSource.available is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Fetch price observations for the requested indicators.

        Args:
            indicators: Canonical indicator keys. Serves ``equity_index``,
                ``vol_index`` and ``commodity_price``.
            currencies: ISO 4217 codes, plus ``"GLOBAL"`` for VIX and the
                broad commodity index.
            start: Earliest session wanted.
            end: Latest session wanted.

        Returns:
            Observations carrying canonical indicator keys.

        Raises:
            SourceError: On repeated request failure, or when the response is
                the anti-bot challenge rather than CSV. Never raised for a
                request this source does not serve: a pair the registry routes
                elsewhere is skipped silently, because the collector fans the
                same request out to every source.

        """
        wanted_indicators = set(indicators)
        wanted_currencies = set(currencies)
        emitted: list[Observation] = []

        # Driven entirely by what the registry routes here. There is no
        # fallback to STOOQ_SYMBOLS: that table is eight unverified guesses,
        # and fetching one because the registry did not ask for it is how an
        # unverified number reaches a pillar.
        for (indicator, currency), ref in self.refs().items():
            if indicator not in wanted_indicators:
                continue
            if currency not in wanted_currencies:
                continue
            for period, value in self.fetch_stooq(ref.series_id, start, end):
                emitted.append(
                    self._observation(indicator, currency, ref, period, value)
                )
        return emitted

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"stooq"``.

        Derived from `fbe.datasources.registry.INDICATORS` rather than from a
        list kept here, so routing a ref to this source is a registry edit and
        nothing in this module has to be remembered alongside it.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`. Empty
            today: the registry routes the equity indices to the monthly OECD
            series, and moving one here needs the symbol confirmed in a browser
            first. Empty is the honest answer, not a gap.

        """
        return {
            (spec.key, currency): ref
            for spec in INDICATORS.values()
            for currency, ref in spec.series.items()
            if ref.source == self.name
        }

    def fetch_stooq(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "d",
    ) -> Sequence[tuple[date, float]]:
        """Fetch one Stooq symbol's daily closes.

        Args:
            symbol: A Stooq symbol, e.g. ``"^spx"`` or ``"eurusd"``.
            start: Passed as ``d1`` in ``YYYYMMDD`` form.
            end: Passed as ``d2``.
            interval: ``"d"``, ``"w"`` or ``"m"``.

        Returns:
            ``(session_date, close)`` pairs, oldest first.

        Raises:
            SourceError: If the body is not the expected CSV, naming the
                symbol. Stooq answers a blocked request with HTTP 200 and an
                HTML challenge, so anything whose first line is not the
                ``Date,Open,High,Low,Close,Volume`` header is refused. It does
                not return an empty sequence for one: empty means the window
                held no sessions, which downstream is a currency with no equity
                data, and a blocked source is not that. The two need different
                responses from the operator, so they get different answers
                here. A body that is the header alone is the empty case and
                returns no rows.

                Also raised, unchanged and without the browser advice, for the
                failures the shared request path reports: an exhausted retry, a
                status not worth retrying, or an offline run whose cache holds
                nothing. Those are not the challenge page and must not be
                described as it.

        """
        try:
            body = self._request(
                "",
                {
                    "s": symbol,
                    "d1": start.strftime(STOOQ_DATE_FORMAT),
                    "d2": end.strftime(STOOQ_DATE_FORMAT),
                    "i": interval,
                },
            )
        except _BodyNotCsv as error:
            # `_decode` refused the body so it was never cached, but it does not
            # know which symbol was asked for, and that is the first thing an
            # operator needs: the fix is per symbol and starts with fetching it
            # in a browser.
            raise SourceError(
                f"{self.name} did not return a session history for {symbol}: "
                f"{error}. A blocked request arrives as HTTP 200 with the "
                "anti-bot challenge page, so fetch this symbol in a browser "
                "to confirm it before trusting the source again."
            ) from error
        except SourceError as error:
            # Everything else the request path raises. Named with the symbol,
            # because the caller asked for one, but not diagnosed as the
            # challenge page, because it is not.
            raise SourceError(
                f"{self.name} could not fetch {symbol}: {error}"
            ) from error

        if not isinstance(body, list):
            raise SourceError(
                f"{self.name} decoded {symbol} to {type(body).__name__} rather "
                "than a list of sessions"
            )

        # The window is enforced here rather than trusted to the endpoint. This
        # module's premise is that this server cannot be relied on to return
        # what was asked for, and a row dated after `end` is the one kind of
        # surplus that matters: in Phase 6 `end` is the as-of date of a
        # backtest bar, so a later session is a price the model could not have
        # had.
        return [(session, close) for session, close in body if start <= session <= end]

    def spot(self, pair: str, on: date | None = None) -> float | None:
        """Return the spot rate for a pair in market quoting convention.

        Args:
            pair: Six-character pair, e.g. ``"EURUSD"``.
            on: Session wanted. Defaults to the most recent available.

        Returns:
            The rate quoted base-per-quote as the market writes it, or ``None``
            when no fixing exists for that session. Callers must not invert the
            result themselves: `FRED_SPOT_SERIES` already normalises FRED's
            mixed quoting directions, and a second inversion downstream is how
            a bias ends up backwards.

            ``None`` carries exactly one meaning, that the market published no
            fixing for that session. It is never returned for a pair this
            module does not serve, nor for a request that failed, nor for a
            cold cache on an offline run: each of those raises instead. A
            caller sizing a position has to be able to tell "the market was
            shut" from "we could not ask", because the first is a fact about
            the day and the second is a fact about the run.

            The value is in the quote currency per one unit of the base
            currency, which is the unit FRED publishes and which nothing here
            rescales.

            With ``on`` unset the newest fixing in the window comes back, which
            may be a session or two old over a weekend or a holiday. It is
            never more than `MAX_SPOT_STALENESS_DAYS` old: past that this
            raises rather than returning a rate that is no longer current.

        Raises:
            SourceError: When ``on`` is a ``datetime`` rather than a ``date``,
                since a fixing belongs to a session and not to a moment in one.

                When no FRED credential is configured and the run is online,
                named as a credential problem rather than left to arrive as the
                bare 400 FRED answers a keyless request with.

                When the newest fixing found is more than
                `MAX_SPOT_STALENESS_DAYS` older than the session asked about,
                which means the series stopped publishing rather than the
                market having been shut.

                When ``pair`` is not a key of `FRED_SPOT_SERIES`, and
                the message lists what is served. An unserved pair answered
                with ``None`` would make a typo and a data gap
                indistinguishable at the call site, and ``"USDEUR"`` is in this
                case: a pair written against market convention is refused
                rather than quietly answered with the rate for its inverse.

                Also raised, unchanged, for the failures the shared request
                path reports: an exhausted retry, an unreadable body, or an
                offline run whose cache holds nothing.

        """
        if isinstance(on, datetime):
            # datetime subclasses date, so this type-checks. It would then
            # never match a period and would send an ISO timestamp as
            # observation_end, which FRED rejects with an opaque 400.
            raise SourceError(
                f"spot takes a date for 'on', got a datetime ({on!r}). A "
                "fixing belongs to a session, not to a moment in one."
            )

        try:
            series_id = FRED_SPOT_SERIES[pair]
        except KeyError as error:
            served = ", ".join(sorted(FRED_SPOT_SERIES))
            # Named for FRED rather than for self.name. This class is named
            # "stooq" for the refs it owns, but the fixings come from FRED, and
            # an operator sent to look at Stooq would find its anti-bot
            # challenge and conclude that was the cause.
            raise SourceError(
                f"no FRED spot series is configured for {pair!r}. "
                f"spot serves {served}. Pairs are named the way the market "
                "writes them, so a pair is not served by asking for its "
                "inverse: USDEUR is not EURUSD and will not be answered with "
                "EURUSD's rate."
            ) from error

        # The window ends at the session asked for rather than at today, so no
        # fixing dated after `on` can come back.
        #
        # That bounds the period, not the vintage. These rates are revised, and
        # this call does not pass `vintage`, so a past session comes back as it
        # reads today rather than as it read that day. `spot` is therefore not
        # the entry point a Phase 6 backtest should use; `FredSource` takes a
        # vintage and this signature has nowhere to put one.
        end = on if on is not None else _today()
        start = end - timedelta(days=SPOT_LOOKBACK_DAYS)

        # Through FredSource rather than a second client, so the run keeps one
        # key and one cache for FRED. Nothing here touches the value:
        # FRED_SPOT_SERIES has already resolved the quoting direction by key,
        # and a second normalisation is how a rate ends up inverted.
        fred = self._fred()
        if not fred.available():
            # Asked before the request rather than after, because FRED answers
            # a keyless call with a bare 400 that names no cause, and an
            # operator reading that goes looking at the network.
            raise SourceError(
                f"no FRED credential is configured, so {series_id} cannot be "
                f"fetched for {pair}. Set FRED_API_KEY, or run offline to read "
                "what is already cached."
            )
        rows = fred.fetch_series(series_id, start, end)

        if on is not None:
            for period, value in rows:
                if period == on:
                    return value
            # No row for that session. Not the previous session's rate: a stale
            # fixing presented as this session's own is how a position gets
            # sized against a number nobody looked at.
            return None

        if not rows:
            return None

        # The latest date, not the last row. FRED returns them ascending, but
        # that is its choice rather than a promise worth leaning on.
        period, value = max(rows, key=lambda row: row[0])

        # A FRED series can stop publishing while still answering requests
        # normally, which the fred module docstring records happening already.
        # Without this the newest row in a thirty day window comes back as
        # though it were today's rate, and the caller sizing a position against
        # it has no way to tell: the return is a bare float with no date on it.
        age = (end - period).days
        if age > MAX_SPOT_STALENESS_DAYS:
            raise SourceError(
                f"the newest {series_id} fixing is {period.isoformat()}, "
                f"{age} days before {end.isoformat()}. That is past the "
                f"{MAX_SPOT_STALENESS_DAYS} day bound, so the series has "
                "stopped publishing rather than the market having been shut. "
                "Refusing rather than returning it as the current rate."
            )
        return value
