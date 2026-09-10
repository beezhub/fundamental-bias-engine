"""FRED client: the engine's primary macro source.

FRED is the Federal Reserve Bank of St. Louis's economic data service. It backs
MONETARY, GROWTH, EMPLOYMENT and EXTERNAL, plus US and euro-area INFLATION, and
it is the widest single free source in the stack. Its reach is uneven, and
`fbe.datasources.registry` documents exactly where.

One caveat governs how much of this registry FRED is allowed to carry. Much of
what FRED serves for non-US countries is republished OECD material, and several
of those mirrors have stopped updating while continuing to answer requests
normally: the whole OECD CPI complex ends in March or April 2025, Japan's ends
in 2021, and industrial production and current account families end in 2023 and
2024. Nothing in the API signals this. Wherever the OECD publishes the same
series itself, the registry now reads it from `fbe.datasources.oecd` instead,
and `last_updated` below exists so that any remaining FRED ref can be checked
rather than trusted.

The API
-------
Base URL: ``https://api.stlouisfed.org/fred/``

Endpoints this client uses:

* ``series/observations`` for the data itself.
* ``series`` for metadata, used to confirm a series still updates before the
  engine trusts it.
* ``series/vintagedates`` for ALFRED, see below.

Required parameters on every call: ``series_id`` and ``api_key``. The key is a
32-character lowercase alphanumeric string, free, issued instantly from
https://fredaccount.stlouisfed.org/apikeys after registering an account. Set it
as ``FRED_API_KEY`` in the environment; `fbe.config.default_config` reads it
from there so it never has to appear in a config file.

Parameters worth knowing, all optional:

* ``file_type=json``. The default is XML, which nobody wants.
* ``observation_start`` and ``observation_end``, ``YYYY-MM-DD``.
* ``units``: ``lin`` (the default, as published), ``pch``, ``pc1`` (percent
  change from a year ago), ``chg``, ``ch1``, ``pca``, ``cch``, ``cca``, ``log``.
  ``pc1`` computes a year-on-year rate server-side, which is exactly the ``yoy``
  transform the registry asks for on index series. Using it costs nothing and
  removes a whole class of arithmetic bugs.
* ``frequency`` with ``aggregation_method`` (``avg``, ``sum``, ``eop``) to
  downsample. Aggregating a daily yield to monthly with ``eop`` is not the same
  as with ``avg``; pick deliberately.
* ``realtime_start`` and ``realtime_end``, both defaulting to today.
* ``vintage_dates``, a comma-separated list.
* ``limit`` (default and maximum 100000), ``offset``, ``sort_order``.

Missing observations come back as the string ``"."``, not as null and not
omitted. Anything parsing this must handle that or it will read a hole in the
series as a zero.

ALFRED and look-ahead bias
--------------------------
This is the part that matters for anyone who later backtests this engine.

Macro data is revised. US GDP for a quarter is published three times over three
months and revised again for years afterwards. FRED serves the *latest* vintage
by default, so a naive backtest scoring January 2024 gets numbers nobody could
have seen until 2025. The model looks prescient and is not.

ALFRED is FRED's vintage archive, reached through the same endpoints via
``realtime_start`` and ``realtime_end``. Setting both to a past date returns the
series as it stood on that date, revisions and all. ``series/vintagedates``
lists every date a given series was revised, which is what a backtest walks.

Live scoring does not need any of this, so the client defaults to the latest
vintage. But a backtest that skips it is measuring nothing. `Observation` keeps
``released_at`` and ``revision`` for precisely this reason.

Rate limits
-----------
The published terms of use do not state a number. They reserve the right to
limit bandwidth and transaction volume, and 429 responses do occur. The
commonly cited operational ceiling is 120 requests per minute per key; that
figure is not in the documentation and is treated here as an unverified
assumption, so `RATE_LIMIT` sits well under it. It costs nothing to be polite:
one full G10 refresh is under 150 requests and runs once a day.

Terms of use
------------
https://fred.stlouisfed.org/docs/api/terms_of_use.html. Two obligations bind
this project. First, attribution: any interface built on this must display
"This product uses the FRED(R) API but is not endorsed or certified by the
Federal Reserve Bank of St. Louis." Second, third-party data: many series here
are sourced from the OECD, Eurostat and the IMF and carry their own copyright.
Personal use is fine. Redistributing the numbers is not, without asking the
data owner.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from fbe.config import DataConfig
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy
from fbe.datasources.registry import SeriesRef
from fbe.types import Observation

__all__ = [
    "API_KEY_URL",
    "BASE_URL",
    "ENDPOINTS",
    "MISSING_VALUE",
    "RATE_LIMIT",
    "UNITS",
    "FredSource",
]


BASE_URL = "https://api.stlouisfed.org/fred/"
"""Verified live. Every endpoint below hangs off this."""

API_KEY_URL = "https://fredaccount.stlouisfed.org/apikeys"
"""Where an operator gets a free key. Registration, then instant issue."""

TERMS_URL = "https://fred.stlouisfed.org/docs/api/terms_of_use.html"

ATTRIBUTION = (
    "This product uses the FRED(R) API but is not endorsed or certified by "
    "the Federal Reserve Bank of St. Louis."
)
"""Required on any user-facing surface. The report template must carry it."""

ENDPOINTS: Mapping[str, str] = {
    "observations": "series/observations",
    "series": "series",
    "vintage_dates": "series/vintagedates",
    "search": "series/search",
}
"""Verified live: each returns a 400 naming the missing ``api_key`` when called
without one, which is how a real endpoint answers an unauthenticated request."""

MISSING_VALUE = "."
"""FRED's null. A parser that misses this reads gaps as zeros."""

UNITS: Mapping[str, str] = {
    "level": "lin",
    "yoy": "pc1",
    "pct_change": "pch",
    "diff": "chg",
    "yoy_diff": "ch1",
}
"""Registry transform hints mapped onto FRED's ``units`` parameter, so the
server does the arithmetic. ``pc1`` is percent change from a year ago and is
what turns an index like ``CPIAUCSL`` into ``cpi_yoy`` without local code."""

RATE_LIMIT = RateLimit(requests=60, per_seconds=60.0, min_interval_seconds=0.2)
"""Half the commonly cited 120 per minute, with a 200ms floor between calls.
Deliberately conservative: the daily refresh is small and a revoked key costs
far more than the seconds saved."""


class FredSource(BaseDataSource):
    """Fetches macro series from FRED and ALFRED.

    Backs MONETARY, INFLATION, GROWTH, EMPLOYMENT and EXTERNAL. One request per
    ``(indicator, currency)`` pair, since FRED's observations endpoint takes a
    single ``series_id``.

    Attributes:
        name: ``"fred"``, matching ``SeriesRef.source``.

    """

    name = "fred"
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=3, backoff_seconds=1.0)

    def __init__(self, config: DataConfig) -> None:
        """Store config and the API key.

        Args:
            config: Effective `DataConfig`. ``fred_api_key`` must be set for
                the source to report itself available.

        """
        super().__init__(config)

    def available(self) -> bool:
        """Report whether an API key is configured, or the run is offline.

        Offline runs read the cache, which needs no key. That is what makes a
        cached run reproducible on a machine that has never had one.

        Returns:
            Whether this source can be used on this run.

        """
        raise NotImplementedError

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Fetch every FRED-backed series in the request.

        Args:
            indicators: Canonical indicator keys. Pairs whose `SeriesRef` names
                another source are skipped.
            currencies: ISO 4217 codes, plus ``"GLOBAL"``.
            start: Earliest period wanted, passed as ``observation_start``.
            end: Latest period wanted, passed as ``observation_end``.

        Returns:
            Observations keyed by canonical indicator, never by FRED series ID.

        Raises:
            SourceError: On repeated request failure or an unparseable body.

        """
        raise NotImplementedError

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"fred"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        raise NotImplementedError

    def fetch_series(
        self,
        series_id: str,
        start: date,
        end: date,
        units: str = "lin",
        vintage: date | None = None,
    ) -> Sequence[tuple[date, float]]:
        """Fetch one raw series, optionally as it stood on a past date.

        Args:
            series_id: FRED series ID, e.g. ``"DGS2"``.
            start: ``observation_start``.
            end: ``observation_end``.
            units: A value from `UNITS`, defaulting to as-published.
            vintage: When given, sets ``realtime_start`` and ``realtime_end`` to
                this date, returning the ALFRED vintage rather than the current
                one. This is the switch a backtest must set on every call; see
                the module docstring on look-ahead bias.

        Returns:
            ``(period, value)`` pairs with `MISSING_VALUE` rows dropped.

        Raises:
            SourceError: On repeated request failure or an unparseable body.

        """
        raise NotImplementedError

    def vintage_dates(self, series_id: str) -> Sequence[date]:
        """List every date on which a series was revised.

        A backtest walks these to reconstruct what was knowable at each point
        in time. Series that are never revised, most daily market rates among
        them, return a short list or one entry.

        Args:
            series_id: FRED series ID.

        Returns:
            Revision dates in ascending order.

        Raises:
            SourceError: On repeated request failure.

        """
        raise NotImplementedError

    def last_updated(self, series_id: str) -> date | None:
        """Return the date of a series' most recent observation.

        Worth calling before trusting any of the registry's OECD-sourced refs.
        Several of those resolve happily and return data that stopped in 2024
        or 2025, and a silent stale series is the failure mode this whole
        registry is built to avoid.

        Args:
            series_id: FRED series ID.

        Returns:
            The last observation date, or ``None`` when the series holds none.

        Raises:
            SourceError: On repeated request failure.

        """
        raise NotImplementedError
