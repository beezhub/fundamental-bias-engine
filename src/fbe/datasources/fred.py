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
from fbe.datasources.base import (
    BaseDataSource,
    RateLimit,
    RetryPolicy,
    SourceError,
)
from fbe.datasources.registry import INDICATORS, SeriesRef
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
    base_url = BASE_URL
    api_key_param = "api_key"
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=3, backoff_seconds=1.0)

    def __init__(self, config: DataConfig) -> None:
        """Store config and the API key.

        Args:
            config: Effective `DataConfig`. ``fred_api_key`` must be set for
                the source to report itself available.

        """
        super().__init__(config)

    def _api_key(self) -> str | None:
        """Return the configured FRED key, or ``None`` when there is none.

        ``None`` means the ``api_key`` parameter is not sent at all rather than
        sent empty, which is what `BaseDataSource._fetch_with_retries` expects.
        FRED answers such a request with a 400 naming the missing key, which is
        a clearer failure than an empty credential would produce.

        Returns:
            The key from `fbe.config.DataConfig`, or ``None``.

        """
        return self.config.fred_api_key

    def available(self) -> bool:
        """Report whether an API key is configured, or the run is offline.

        Offline runs read the cache, which needs no key. That is what makes a
        cached run reproducible on a machine that has never had one.

        Returns:
            Whether this source can be used on this run.

        """
        return self.config.offline or self.config.fred_api_key is not None

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
        wanted_indicators = set(indicators)
        wanted_currencies = set(currencies)
        emitted: list[Observation] = []
        for (indicator, currency), ref in self.refs().items():
            if indicator not in wanted_indicators:
                continue
            if currency not in wanted_currencies:
                continue
            units = self._units_for(indicator, currency, ref)
            for period, value in self.fetch_series(
                ref.series_id, start, end, units=units
            ):
                emitted.append(
                    self._observation(indicator, currency, ref, period, value)
                )
        return emitted

    def _units_for(self, indicator: str, currency: str, ref: SeriesRef) -> str:
        """Map a registry transform onto FRED's ``units`` parameter.

        Args:
            indicator: Canonical indicator key, named in the error.
            currency: ISO 4217 code, named in the error.
            ref: The registry entry whose ``transform`` is being mapped.

        Returns:
            The `UNITS` value FRED should compute, so the arithmetic happens
            server-side rather than here.

        Raises:
            SourceError: When `UNITS` has no entry for the transform. The two
                that reach this today are ``chg_1m`` and ``chg_3m``, which the
                registry defines as a month-end or quarter-end resample, then a
                difference, then a rescale into basis points. FRED's ``chg`` is
                the change from the previous observation, which on a daily
                series is a one-day change, so mapping the two would emit a
                number roughly thirty times too small under a label saying
                otherwise. Refusing is the only safe answer until the
                derivation is settled; see the note on issue #56.

        """
        try:
            return UNITS[ref.transform]
        except KeyError as error:
            raise SourceError(
                f"{self.name} cannot express the {ref.transform!r} transform "
                f"that {indicator} / {currency} asks for as a FRED units "
                "parameter, and will not approximate it with a different one"
            ) from error

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"fred"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        return {
            (spec.key, currency): ref
            for spec in INDICATORS.values()
            for currency, ref in spec.series.items()
            if ref.source == self.name
        }

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
        params: dict[str, str | int | float] = {
            "series_id": series_id,
            "observation_start": start.isoformat(),
            "observation_end": end.isoformat(),
            "units": units,
            "file_type": "json",
        }
        if vintage is not None:
            # Both bounds, not just the start: a window sets which vintages are
            # returned, and leaving the end open returns every revision since
            # rather than the one that stood on the day.
            params["realtime_start"] = vintage.isoformat()
            params["realtime_end"] = vintage.isoformat()
        payload = self._request(ENDPOINTS["observations"], params)
        return self._parse_observations(payload, series_id)

    def _parse_observations(
        self, payload: object, series_id: str
    ) -> Sequence[tuple[date, float]]:
        """Read ``(period, value)`` pairs out of an observations body.

        Args:
            payload: The decoded response body.
            series_id: Named in any error, since the body does not carry it.

        Returns:
            Pairs in the order FRED returned them, with `MISSING_VALUE` rows
            dropped. Values are in the series' published unit, after whatever
            ``units`` was requested. An empty list means the series genuinely
            holds nothing in the window, which is data.

        Raises:
            SourceError: When the body carries no ``observations`` list, when a
                row is missing its date or value, or when a value is neither
                `MISSING_VALUE` nor a number. Only ``"."`` is a documented
                hole; anything else unreadable is a response shape that has
                changed, and skipping it would turn that into a quiet coverage
                gap.

        """
        if not isinstance(payload, dict) or not isinstance(
            payload.get("observations"), list
        ):
            raise SourceError(
                f"{self.name} returned no observations list for {series_id}"
            )
        parsed: list[tuple[date, float]] = []
        for row in payload["observations"]:
            if not isinstance(row, dict) or "date" not in row or "value" not in row:
                raise SourceError(
                    f"{self.name} returned a row without a date and a value "
                    f"for {series_id}"
                )
            raw = str(row["value"])
            if raw == MISSING_VALUE:
                # FRED's null. Reading it as a number would put a zero in the
                # middle of a yield series, which scores as a real collapse.
                continue
            try:
                period = date.fromisoformat(str(row["date"]))
                value = float(raw)
            except ValueError as error:
                raise SourceError(
                    f"{self.name} returned an unreadable row for {series_id}: {error}"
                ) from error
            parsed.append((period, value))
        return parsed

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
        payload = self._request(
            ENDPOINTS["vintage_dates"],
            {"series_id": series_id, "file_type": "json"},
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("vintage_dates"), list
        ):
            raise SourceError(
                f"{self.name} returned no vintage_dates list for {series_id}"
            )
        try:
            return sorted(
                date.fromisoformat(str(day)) for day in payload["vintage_dates"]
            )
        except ValueError as error:
            raise SourceError(
                f"{self.name} returned an unreadable vintage date for "
                f"{series_id}: {error}"
            ) from error

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
        payload = self._request(
            ENDPOINTS["series"],
            {"series_id": series_id, "file_type": "json"},
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("seriess"), list
        ):
            raise SourceError(
                f"{self.name} returned no series metadata for {series_id}"
            )
        entries = payload["seriess"]
        if not entries or not isinstance(entries[0], dict):
            # An unknown series ID answers with an empty list. That is a broken
            # registry entry, not a series that happens to hold nothing, and
            # the two must not both read as None.
            raise SourceError(f"{self.name} knows no series called {series_id}")
        observation_end = entries[0].get("observation_end")
        if not observation_end or observation_end == MISSING_VALUE:
            return None
        try:
            return date.fromisoformat(str(observation_end))
        except ValueError as error:
            raise SourceError(
                f"{self.name} returned an unreadable observation_end for "
                f"{series_id}: {error}"
            ) from error
