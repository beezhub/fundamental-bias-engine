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

from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from fbe.config import DataConfig
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy
from fbe.datasources.registry import SeriesRef
from fbe.types import Observation

__all__ = [
    "FRED_COMMODITY_SERIES",
    "FRED_RISK_SERIES",
    "FRED_SPOT_SERIES",
    "RATE_LIMIT",
    "STOOQ_CSV_URL",
    "STOOQ_SYMBOLS",
    "YAHOO_CHART_URL",
    "PricesSource",
]


STOOQ_CSV_URL = "https://stooq.com/q/d/l/"
"""Daily history download. Query: ``s`` symbol, ``d1``/``d2`` dates as
``YYYYMMDD``, ``i`` interval. Returns ``Date,Open,High,Low,Close,Volume``."""

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

RATE_LIMIT = RateLimit(requests=20, per_seconds=60.0, min_interval_seconds=1.0)
"""Stooq throttles by IP and publishes no limit. One second between requests,
and the whole daily refresh is a handful of symbols."""


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
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=2, backoff_seconds=2.0)

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: Effective `DataConfig`. No credential is needed.

        """
        super().__init__(config)

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
                ``vix`` and ``commodity_index``.
            currencies: ISO 4217 codes, plus ``"GLOBAL"`` for VIX and the
                broad commodity index.
            start: Earliest session wanted.
            end: Latest session wanted.

        Returns:
            Observations carrying canonical indicator keys.

        Raises:
            SourceError: On repeated request failure, or when the response is
                the anti-bot challenge rather than CSV.

        """
        raise NotImplementedError(
            "fbe.datasources.prices.PricesSource.fetch is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"stooq"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        raise NotImplementedError(
            "fbe.datasources.prices.PricesSource.refs is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

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
            SourceError: If the body is not the expected CSV. Stooq answers a
                blocked request with HTTP 200 and an HTML challenge, so the
                parser must reject anything whose first line is not the
                ``Date,Open,High,Low,Close,Volume`` header.

        """
        raise NotImplementedError(
            "fbe.datasources.prices.PricesSource.fetch_stooq is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

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

        """
        raise NotImplementedError(
            "fbe.datasources.prices.PricesSource.spot is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )
