"""OECD SDMX API: current data where FRED's mirror of the same material is not.

The discovery that makes this module worth having: FRED republishes OECD
statistics, and for prices it stopped. FRED's entire OECD CPI complex, index
levels and year-on-year rates alike, ends in March or April 2025, and Japan's
ends in June 2021. The OECD's own API serves the same countries through July or
August 2026.

That is a five-year hole for Japan and a seventeen-month hole for five other
currencies, and it was invisible: every one of those FRED series resolves,
returns data, and looks healthy. The inflation pillar would have scored six of
eight G10 currencies on numbers from another cycle.

The same is true, less dramatically, of policy rates, 10-year yields and share
price indices, where the OECD API runs about two months ahead of FRED. Anywhere
this project used to read OECD material through FRED, it now reads it here.

The API
-------
Base: ``https://sdmx.oecd.org/public/rest/``. No key, no registration.

Data requests are ``data/{agency},{dataflow},{version}/{key}`` with the key
being dot-separated dimension values, empty for a wildcard. Ask for flat CSV
with an ``Accept: application/vnd.sdmx.data+csv`` header, which is more
reliable here than the ``format=`` query parameter.

The key must have exactly as many segments as the dataflow has dimensions or
the request fails, and the two failure modes read very differently: too few
segments gives HTTP 422 with a helpful "expecting 8 got 7", while too many
gives HTTP 404 with the body ``NoRecordsFound``, indistinguishable from a valid
query that matched nothing. Count the dimensions from the datastructure rather
than guessing; `DIMENSIONS` below records the two that matter.

Rate limits
-----------
Real, undocumented, and enforced. A broad wildcard query triggered HTTP 429
with a plain-text body beginning "You have exceeded the number of requests for
data downloads or very large data ranges permitted in the OECD Data API".

Two consequences. Narrow every request to the exact key and period wanted
rather than pulling a country's whole dataflow and filtering locally, which is
the natural thing to write and the thing that gets a client blocked. And treat
a 429 as an error: the body is prose, not SDMX, so a parser that does not check
will read it as zero observations and report a currency as uncovered when it is
merely throttled.

The two price dataflows, and why both are needed
------------------------------------------------
Countries are split across two dataflows by classification vintage, and the
split does not follow anything you would guess:

* ``DSD_PRICES@DF_PRICES_ALL`` (COICOP 1999) has the United States, the United
  Kingdom, Canada, Germany, Australia and New Zealand.
* ``DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL`` (COICOP 2018) has Japan and
  Switzerland, and also Canada and Australia.

Neither covers all eight. A currency queried against the wrong one returns
``NoRecordsFound``, which looks exactly like a dead series. `CPI_FLOW` records
which flow each currency lives in, verified one currency at a time.

Switzerland is stranger still: its core CPI is in neither general flow and
lives only in the dedicated core dataflow
``DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG``. That is the only free
Swiss core inflation series found anywhere.

Terms
-----
OECD data is published under its own terms at https://www.oecd.org/termsandconditions/.
Free for personal and non-commercial use with attribution to the OECD. As with
FRED, redistributing the numbers is a different question from using them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from fbe.config import DataConfig
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy
from fbe.datasources.registry import SeriesRef
from fbe.types import Observation

__all__ = [
    "BASE_URL",
    "CPI_FLOW",
    "CSV_ACCEPT",
    "DIMENSIONS",
    "FINMARK_FLOW",
    "FLOW_VERSIONS",
    "MEASURES",
    "RATE_LIMIT",
    "REF_AREA",
    "THROTTLE_MARKER",
    "OecdSource",
]


BASE_URL = "https://sdmx.oecd.org/public/rest/"
"""Verified live. ``data/...`` for observations, ``dataflow/{agency}`` to list
flows, ``datastructure/{agency}/{dsd}`` for dimension order."""

CSV_ACCEPT = "application/vnd.sdmx.data+csv"
"""Ask for CSV by header. More dependable here than ``?format=csvdata``, which
is accepted on some flows and ignored on others."""

TERMS_URL = "https://www.oecd.org/termsandconditions/"

THROTTLE_MARKER = "You have exceeded the number of requests"
"""First words of the HTTP 429 body. Plain prose, not SDMX. A client that does
not check for it will parse a throttle as an empty result and report a live
currency as uncovered."""

FLOW_VERSIONS: Mapping[str, str] = {
    "DSD_PRICES@DF_PRICES_ALL": "1.0",
    "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL": "1.0",
    "DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG": "1.0",
    "DSD_STES@DF_FINMARK": "4.0",
    "DSD_KEI@DF_KEI": "4.0",
}
"""Flow to version, all verified live. Versions are part of the URL and are not
interchangeable: ``DF_FINMARK`` answers at 4.0 and 404s at 4.1 and 1.0."""

FLOW_AGENCIES: Mapping[str, str] = {
    "DSD_PRICES@DF_PRICES_ALL": "OECD.SDD.TPS",
    "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL": "OECD.SDD.TPS",
    "DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG": "OECD.SDD.TPS",
    "DSD_STES@DF_FINMARK": "OECD.SDD.STES",
    "DSD_KEI@DF_KEI": "OECD.SDD.STES",
}

DIMENSIONS: Mapping[str, tuple[str, ...]] = {
    "DSD_PRICES": (
        "REF_AREA",
        "FREQ",
        "METHODOLOGY",
        "MEASURE",
        "UNIT_MEASURE",
        "EXPENDITURE",
        "ADJUSTMENT",
        "TRANSFORMATION",
    ),
    "DSD_STES": (
        "REF_AREA",
        "FREQ",
        "MEASURE",
        "UNIT_MEASURE",
        "ACTIVITY",
        "ADJUSTMENT",
        "TRANSFORMATION",
        "TIME_HORIZ",
        "METHODOLOGY",
    ),
}
"""Dimension order for the two structures used, read from their datastructure
definitions. The key in a data request must supply exactly this many segments.
Eight for prices, nine for short-term statistics."""

REF_AREA: Mapping[str, str] = {
    "USD": "USA",
    "EUR": "DEU",
    "GBP": "GBR",
    "JPY": "JPN",
    "CHF": "CHE",
    "CAD": "CAN",
    "AUD": "AUS",
    "NZD": "NZL",
}
"""Currency to OECD reference area.

``EUR`` maps to Germany, not to the euro area. ``EA20`` exists and answers for
the financial market flow, but not for national CPI, where the OECD serves
member states rather than the bloc. Where a true euro-area aggregate exists,
the registry uses Eurostat through FRED instead, which is why euro CPI does not
come from this module at all."""

EA_REF_AREA = "EA20"
"""The euro-area aggregate, valid in ``DSD_STES@DF_FINMARK`` and not in the
price flows. Kept for the rates and share price series, where it is a genuine
bloc number rather than a German proxy."""

CPI_FLOW: Mapping[str, str] = {
    "GBP": "DSD_PRICES@DF_PRICES_ALL",
    "CAD": "DSD_PRICES@DF_PRICES_ALL",
    "AUD": "DSD_PRICES@DF_PRICES_ALL",
    "NZD": "DSD_PRICES@DF_PRICES_ALL",
    "USD": "DSD_PRICES@DF_PRICES_ALL",
    "JPY": "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL",
    "CHF": "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL",
}
"""Which price dataflow holds each currency's headline CPI, verified one at a
time. A currency asked of the wrong flow returns ``NoRecordsFound``, which is
indistinguishable from a dead series, so this mapping is load-bearing rather
than a convenience."""

CORE_CPI_FLOW: Mapping[str, str] = {
    "GBP": "DSD_PRICES@DF_PRICES_ALL",
    "AUD": "DSD_PRICES@DF_PRICES_ALL",
    "NZD": "DSD_PRICES@DF_PRICES_ALL",
    "USD": "DSD_PRICES@DF_PRICES_ALL",
    "JPY": "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL",
    "CAD": "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL",
    "CHF": "DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG",
}
"""Core CPI splits differently again. Canada's core is in the 2018 flow while
its headline is in both. Switzerland's core is in neither general flow and only
in the dedicated core flow, which is the only free Swiss core series found."""

FINMARK_FLOW = "DSD_STES@DF_FINMARK"
"""Policy rates, bond yields, share price indices and exchange rates. All
current to roughly one month behind, against FRED's three."""

MEASURES: Mapping[str, str] = {
    "policy_rate": "IRSTCI",
    "yield_10y": "IRLT",
    "rate_3m": "IR3TIB",
    "equity_index": "SHARE",
    "exchange_rate": "CC",
    "real_effective_rate": "CCRE",
}
"""``MEASURE`` codes in `FINMARK_FLOW`, all verified live for all eight
currencies. ``IRSTCI`` is the overnight or call money rate, which tracks the
policy rate rather than being it, and is the only current free short rate for
the Swiss franc and the New Zealand dollar."""

CPI_MEASURE = "CPI"
CPI_METHODOLOGY = "N"
"""National CPI, as opposed to the harmonised HICP construction."""

CPI_UNIT_PERCENT = "PA"
"""Percent per annum. Paired with ``TRANSFORMATION=GY`` this is the
year-on-year growth rate, already computed, so the registry's transform hint is
``level`` rather than ``yoy``."""

CPI_TRANSFORM_YOY = "GY"
EXPENDITURE_ALL = "_T"
EXPENDITURE_CORE = "_TXCP01_NRG"
"""All items, and all items less food and energy."""

RATE_LIMIT = RateLimit(requests=10, per_seconds=60.0, min_interval_seconds=3.0)
"""Set from observed behaviour, not from documentation, because there is none.
A burst of broad queries earned a 429 within a minute. Three seconds between
narrow requests covers a full G10 refresh in under a minute and has not been
throttled."""


class OecdSource(BaseDataSource):
    """Fetches macro series from the OECD's own SDMX API.

    Backs INFLATION for six currencies, and the parts of MONETARY and RISK
    where FRED's mirror of the same OECD material runs months behind.

    Series IDs in the registry are written as ``{flow}/{key}``, so one string
    carries both the dataflow and the dimension key; `split_series_id` is the
    only thing that needs to know that.

    Attributes:
        name: ``"oecd"``, matching ``SeriesRef.source``.

    """

    name = "oecd"
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=3, backoff_seconds=5.0)

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: Effective `DataConfig`. No credential is needed.

        """
        super().__init__(config)

    def available(self) -> bool:
        """Report whether the OECD API answers, or a warm cache exists.

        Returns:
            Whether this source can be used on this run.

        """
        raise NotImplementedError(
            "fbe.datasources.oecd.OecdSource.available is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Fetch every OECD-backed series in the request.

        Issues one narrow request per ``(indicator, currency)``. Deliberately
        not one broad request per country: broad queries are what trigger the
        API's throttle, and the saving is not worth losing the inflation pillar
        for a day.

        Args:
            indicators: Canonical indicator keys. Pairs whose `SeriesRef` names
                another source are skipped.
            currencies: ISO 4217 codes.
            start: Earliest period wanted, passed as ``startPeriod``.
            end: Latest period wanted, passed as ``endPeriod``.

        Returns:
            Observations keyed by canonical indicator.

        Raises:
            SourceError: On repeated request failure, on an unparseable body,
                or when the body contains `THROTTLE_MARKER`.

        """
        raise NotImplementedError(
            "fbe.datasources.oecd.OecdSource.fetch is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"oecd"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        raise NotImplementedError(
            "fbe.datasources.oecd.OecdSource.refs is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def split_series_id(self, series_id: str) -> tuple[str, str]:
        """Split a registry series ID into its dataflow and dimension key.

        Args:
            series_id: e.g.
                ``"DSD_PRICES@DF_PRICES_ALL/GBR.M.N.CPI.PA._T.N.GY"``.

        Returns:
            ``(flow, key)``.

        Raises:
            ValueError: If the ID has no separator, or names a flow absent from
                `FLOW_VERSIONS`. An unknown flow has no version and cannot be
                turned into a URL, so it fails here rather than as a 404 later.

        """
        raise NotImplementedError(
            "fbe.datasources.oecd.OecdSource.split_series_id is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def fetch_key(
        self,
        flow: str,
        key: str,
        start: date,
        end: date,
    ) -> Sequence[tuple[str, float]]:
        """Fetch one SDMX key as ``(time_period, value)`` pairs.

        Args:
            flow: Dataflow id, a key of `FLOW_VERSIONS`.
            key: Dot-separated dimension key. Must carry exactly as many
                segments as the flow has dimensions; see `DIMENSIONS`. Too few
                returns HTTP 422 naming the shortfall, too many returns HTTP
                404 ``NoRecordsFound``, which is silently wrong.
            start: Passed as ``startPeriod``.
            end: Passed as ``endPeriod``.

        Returns:
            ``(time_period, value)`` pairs with the period left as the API's
            own string, since monthly is ``2026-07`` and quarterly is
            ``2026-Q2`` and only the caller knows which it asked for.

        Raises:
            SourceError: On repeated failure, on a throttle, or on a body that
                is neither CSV nor a recognised error.

        """
        raise NotImplementedError(
            "fbe.datasources.oecd.OecdSource.fetch_key is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def parse_period(self, period: str, frequency: str) -> date:
        """Convert an SDMX time period to the first day of the period it names.

        Args:
            period: e.g. ``"2026-07"`` or ``"2026-Q2"``.
            frequency: ``"M"`` or ``"Q"``.

        Returns:
            The first day of the period, matching the convention the rest of
            the registry uses so that OECD and FRED observations for the same
            quarter sort together.

        Raises:
            ValueError: On a period string that does not match the frequency.

        """
        raise NotImplementedError(
            "fbe.datasources.oecd.OecdSource.parse_period is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def cpi_key(self, currency: str, core: bool = False) -> tuple[str, str]:
        """Build the flow and key for one currency's CPI series.

        Args:
            currency: ISO 4217 code.
            core: True for all items less food and energy.

        Returns:
            ``(flow, key)`` ready for `fetch_key`.

        Raises:
            KeyError: If the currency is in neither `CPI_FLOW` nor
                `CORE_CPI_FLOW`. The euro is the expected case: its CPI comes
                from Eurostat through FRED, because the OECD serves member
                states rather than the bloc.

        """
        raise NotImplementedError(
            "fbe.datasources.oecd.OecdSource.cpi_key is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def finmark_key(self, currency: str, measure: str) -> tuple[str, str]:
        """Build the flow and key for one currency's financial market series.

        Args:
            currency: ISO 4217 code.
            measure: A value of `MEASURES`.

        Returns:
            ``(flow, key)`` ready for `fetch_key`.

        Raises:
            KeyError: If the currency or measure is unknown.

        """
        raise NotImplementedError(
            "fbe.datasources.oecd.OecdSource.finmark_key is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )
