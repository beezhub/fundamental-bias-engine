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

import csv
import io
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from types import MappingProxyType

from fbe.config import DataConfig
from fbe.datasources.base import (
    BaseDataSource,
    FailureScope,
    RateLimit,
    RetryPolicy,
    SourceError,
)
from fbe.datasources.registry import INDICATORS, SeriesRef
from fbe.types import Observation

__all__ = [
    "BASE_URL",
    "BTS_ACTIVITY_MANUFACTURING",
    "BTS_ADJUSTMENT",
    "BTS_FLOW",
    "BTS_FREQUENCY",
    "BTS_MEASURE",
    "BTS_UNIT_BALANCE",
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
    "DSD_STES@DF_BTS": "4.0",
    "DSD_KEI@DF_KEI": "4.0",
}
"""Flow to version, all verified live. Versions are part of the URL and are not
interchangeable: ``DF_FINMARK`` answers at 4.0 and 404s at 4.1 and 1.0."""

FLOW_AGENCIES: Mapping[str, str] = {
    "DSD_PRICES@DF_PRICES_ALL": "OECD.SDD.TPS",
    "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL": "OECD.SDD.TPS",
    "DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG": "OECD.SDD.TPS",
    "DSD_STES@DF_FINMARK": "OECD.SDD.STES",
    "DSD_STES@DF_BTS": "OECD.SDD.STES",
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

CPI_ADJUSTMENT = "N"
"""Neither seasonally adjusted nor calendar adjusted, matching every CPI ref in
the registry. Distinct from `CPI_METHODOLOGY`, which happens to share the
letter and means national rather than harmonised."""

FINMARK_UNITS: Mapping[str, str] = MappingProxyType(
    {
        "IRSTCI": "PA",
        "IRLT": "PA",
        "IR3TIB": "PA",
        "SHARE": "IX",
    }
)
"""``UNIT_MEASURE`` for each measure whose unit was verified against a live
key. Interest rates are percent per annum, share prices an index.

``CC`` and ``CCRE`` are deliberately absent. Both are in `MEASURES`, neither is
routed to by any registry entry, and neither unit was confirmed. Guessing one
produces a key that either returns nothing or returns a different series, and
the second is the failure this module exists to prevent, so `finmark_key`
refuses them instead."""

BTS_FLOW = "DSD_STES@DF_BTS"
"""Business Tendency Surveys: the national confidence surveys the OECD
harmonises and republishes. Shares the `DIMENSIONS` shape of `FINMARK_FLOW`,
being the same ``DSD_STES`` structure, and nothing else.

This is the free route to a leading growth indicator. The licensed alternative,
``pmi_composite``, is 0/8 on free coverage and exists in the registry only as a
manual entry, so every currency loses its leading component in any month nobody
keys eight numbers in by hand.
"""

BTS_MEASURE = "BCICP"
"""Composite business confidence. Verified live for all eight reference areas."""

BTS_UNIT_BALANCE = "PB"
"""Percentage balance: respondents answering positively minus those answering
negatively, as a net percentage.

**Neutral is zero, not 50.** A purchasing managers' index is a diffusion index
with an expansion line at 50 and this is not one, which is why the registry
carries this series under its own key with unit ``percentage_balance`` rather
than as a value written under the PMI key. Reading one as the other shifts every
currency in the universe by the same amount, the cross-sectional z-score absorbs
the offset, and the ranking still comes out looking orderly. Nothing downstream
would raise. See ``docs/answers/data.md`` question 5.

Pinned in the key for the same reason `FINMARK_UNITS` exists: a guessed
``UNIT_MEASURE`` returns either nothing or a different series, and the second is
indistinguishable from success.
"""

BTS_ACTIVITY_MANUFACTURING = "C"
"""ISIC section C, manufacturing. Pinned rather than wildcarded: the survey
covers several activities and a wildcard returns the composite as one row among
many, which the CSV reader would emit as duplicate periods."""

BTS_ADJUSTMENT = "Y"
"""Calendar and seasonally adjusted, which is what this flow serves.

Pinned for the same reason as the activity, and `cpi_key` pins `CPI_ADJUSTMENT`
for the same reason again. Checked against the live flow one currency at a time:
all eight return exactly one row per period and every row carries ``Y``. A
wildcard is safe only for as long as that stays true, and `fetch` has no
duplicate-period guard anywhere in its path, so a second adjustment appearing
later would emit two `Observation`s for one period and nothing would raise.
"""

BTS_FREQUENCY: Mapping[str, str] = MappingProxyType(
    {
        "USD": "M",
        "EUR": "M",
        "GBP": "M",
        "CHF": "M",
        "JPY": "Q",
        "CAD": "Q",
        "AUD": "Q",
        "NZD": "Q",
    }
)
"""Each currency's survey cadence, verified one at a time against the live flow.

Four monthly and four quarterly, because the OECD republishes each country's own
survey at the cadence that country runs it: Japan's is the quarterly Tankan, and
the Australian and New Zealand series are the equally quarterly national business
outlooks. This is not a property of the dataflow that could be read once and
applied to all.

`bts_key` pins the frequency from this table rather than wildcarding it, which is
the one way these keys differ from `finmark_key`. A wildcarded ``FREQ`` returns
every period shape a country publishes, and `_key_frequency` would then have no
single answer to read.
"""

TIME_PERIOD_COLUMN = "TIME_PERIOD"
OBS_VALUE_COLUMN = "OBS_VALUE"
"""The two columns read out of the SDMX CSV. Found by name rather than by
position: the captured body carries sixteen columns and their order is the
API's to change."""

QUARTER_FIRST_MONTH: Mapping[str, int] = MappingProxyType(
    {"1": 1, "2": 4, "3": 7, "4": 10}
)
"""Quarter number to the month it begins in, under the first-day stamping
convention ruled on issue #27."""

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
    base_url = BASE_URL
    default_headers = MappingProxyType({"Accept": CSV_ACCEPT})
    failure_scope = FailureScope.SERIES
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=3, backoff_seconds=5.0)

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: Effective `DataConfig`. No credential is needed.

        """
        super().__init__(config)

    def _decode(self, body: bytes) -> object:
        """Parse an SDMX CSV body into ``(time_period, value)`` pairs.

        Overridden because this API serves CSV where the base expects JSON.
        Doing it here rather than in `fetch_key` is deliberate: `_request`
        writes the cache only after decoding succeeds, so a body this source
        refuses never reaches disk. Caching a throttle would serve it back for
        the whole TTL, and an offline run expires nothing.

        Args:
            body: Raw response bytes, exactly as cached.

        Returns:
            A tuple of ``(time_period, value)`` pairs, periods left as the
            API's own strings. Values carry the unit of the series requested;
            this method does not know which that is. An empty tuple means the
            body was a well-formed CSV with no observation rows, which is a
            window that held nothing and is data.

        Raises:
            SourceError: When the body carries `THROTTLE_MARKER`, when it is
                not CSV with the two columns this source reads, or when a value
                is present and unreadable. A throttle is prose rather than
                SDMX, and parsing it as zero rows would report a live currency
                as uncovered; that is the specific failure this module was
                written for.

        """
        text = body.decode("utf-8-sig", errors="replace")
        if THROTTLE_MARKER in text:
            raise SourceError(
                f"{self.name} was throttled: the response carries the API's "
                "rate limit notice rather than data, so it is an error and not "
                "an empty series"
            )
        reader = csv.DictReader(io.StringIO(text))
        columns = reader.fieldnames or []
        missing = [
            name
            for name in (TIME_PERIOD_COLUMN, OBS_VALUE_COLUMN)
            if name not in columns
        ]
        if missing:
            raise SourceError(
                f"{self.name} returned a body with no {', '.join(missing)} "
                f"column, so it is not the SDMX CSV this source asked for"
            )
        parsed: list[tuple[str, float]] = []
        for row in reader:
            period = (row.get(TIME_PERIOD_COLUMN) or "").strip()
            raw = (row.get(OBS_VALUE_COLUMN) or "").strip()
            if not period:
                raise SourceError(f"{self.name} returned a row with no period")
            if not raw:
                # A blank observation is a hole in the series. Reading it as a
                # zero would put a zero inflation print in the middle of a CPI
                # series, which scores as a real collapse.
                continue
            try:
                value = float(raw)
            except ValueError as error:
                raise SourceError(
                    f"{self.name} returned an unreadable value for {period}: {raw!r}"
                ) from error
            if not math.isfinite(value):
                # float() accepts "nan" and "inf". A nan reaching a
                # cross-sectional pillar makes the mean and the standard
                # deviation nan for the whole universe, so all eight currencies
                # score nan and every threshold comparison quietly reads False.
                raise SourceError(
                    f"{self.name} returned a non-finite value for {period}: {raw!r}"
                )
            parsed.append((period, value))
        return tuple(parsed)

    def available(self) -> bool:
        """Report availability, which is always true: the API needs no key.

        Returns:
            Whether this source can be used on this run. Always ``True``: the
            public SDMX endpoint is open, so there is nothing about the
            configuration that could rule this source out, and there is no
            directory or credential to check.

        This answers about configuration rather than connectivity, which is the
        contract `BaseDataSource.available` sets and
        ``tests/test_datasource_base.py`` enforces across every source: no
        implementation here may make a request. So an unreachable or throttled
        API is not reported here, it is reported by `fetch` raising, and an
        offline run with nothing cached is reported the same way, which is the
        only place that distinction can be made without a network call.

        """
        return True

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
            Observations keyed by canonical indicator. Never an empty result
            for a series that was asked for: see below.

        Raises:
            SourceError: On repeated request failure, on an unparseable body,
                when the body contains `THROTTLE_MARKER`, or when a series this
                was asked for held no rows in the window.

                The last is rule 3 of ADR 0013 at series scope, and it lives
                here rather than in the collector because only the source knows
                what empty means for it. For this API it means a dead or
                misrouted series: a key aimed at the wrong price dataflow
                answers ``NoRecordsFound`` or an empty body, which is
                indistinguishable from a live series on the wire and is how an
                unrouted currency came to read as uncovered. A monthly series
                that genuinely holds nothing across a multi-year window is not
                a case this registry has. Contrast `fbe.datasources.cot`, which
                documents empty as a reading.

        """
        wanted_indicators = set(indicators)
        wanted_currencies = set(currencies)
        emitted: list[Observation] = []
        for (indicator, currency), ref in self.refs().items():
            if indicator not in wanted_indicators:
                continue
            if currency not in wanted_currencies:
                continue
            flow, key = self.split_series_id(ref.series_id)
            frequency = self._key_frequency(key, ref.series_id)
            rows = self.fetch_key(flow, key, start, end)
            if not rows:
                raise SourceError(
                    f"{self.name} served no rows for {indicator} {currency} "
                    f"({ref.series_id}) between {start} and {end}, so the "
                    "series is dead or misrouted rather than empty"
                )
            for period, value in rows:
                emitted.append(
                    self._observation(
                        indicator,
                        currency,
                        ref,
                        self.parse_period(period, frequency),
                        value,
                    )
                )
        return emitted

    def _key_frequency(self, key: str, series_id: str) -> str:
        """Read the ``FREQ`` segment out of a dimension key.

        The key is what was actually asked of the API, so it is what decides
        how to read the periods that come back, rather than the ref's own
        ``frequency`` field.

        Args:
            key: Dot-separated dimension key.
            series_id: The whole registry identifier, named in any error.

        Returns:
            The SDMX frequency letter, ``"M"``, ``"Q"`` or ``"A"``.

        Raises:
            SourceError: When the segment is empty. A wildcarded frequency
                returns periods of more than one shape, and guessing which is
                how a quarter gets filed under a month.

        """
        segments = key.split(".")
        frequency = segments[1] if len(segments) > 1 else ""
        if not frequency:
            raise SourceError(
                f"{self.name} cannot read periods for {series_id} because its "
                "key names no frequency"
            )
        return frequency

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"oecd"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        return {
            (spec.key, currency): ref
            for spec in INDICATORS.values()
            for currency, ref in spec.series.items()
            if ref.source == self.name
        }

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
        flow, separator, key = series_id.partition("/")
        if not separator or not key:
            raise ValueError(f"{series_id!r} is not a {{flow}}/{{key}} identifier")
        if flow not in FLOW_VERSIONS:
            raise ValueError(
                f"{flow!r} is not a known dataflow, so it has no version and "
                f"cannot be turned into a URL; known flows are "
                f"{', '.join(sorted(FLOW_VERSIONS))}"
            )
        return flow, key

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
        agency = FLOW_AGENCIES[flow]
        version = FLOW_VERSIONS[flow]
        payload = self._request(
            f"data/{agency},{flow},{version}/{key}",
            {"startPeriod": start.isoformat(), "endPeriod": end.isoformat()},
        )
        if not isinstance(payload, tuple):
            raise SourceError(
                f"{self.name} decoded {key} into something other than observation pairs"
            )
        return list(payload)

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
        year, _, remainder = period.partition("-")
        if frequency == "A":
            if remainder:
                raise ValueError(f"{period!r} is not an annual period")
            return date(int(year), 1, 1)
        if frequency == "M":
            month = int(remainder)
            if not 1 <= month <= 12:
                raise ValueError(f"{period!r} names no month")
            return date(int(year), month, 1)
        if frequency == "Q":
            if not remainder.startswith("Q"):
                raise ValueError(f"{period!r} is not a quarterly period")
            quarter = remainder[1:]
            if quarter not in QUARTER_FIRST_MONTH:
                raise ValueError(f"{period!r} names no quarter")
            return date(int(year), QUARTER_FIRST_MONTH[quarter], 1)
        raise ValueError(
            f"{frequency!r} is not a frequency this source reads; expected M, Q or A"
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
        flow = (CORE_CPI_FLOW if core else CPI_FLOW)[currency]
        expenditure = EXPENDITURE_CORE if core else EXPENDITURE_ALL
        key = ".".join(
            (
                REF_AREA[currency],
                "",
                CPI_METHODOLOGY,
                CPI_MEASURE,
                CPI_UNIT_PERCENT,
                expenditure,
                CPI_ADJUSTMENT,
                CPI_TRANSFORM_YOY,
            )
        )
        return flow, key

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
        area = REF_AREA[currency]
        if measure not in MEASURES.values():
            raise KeyError(
                f"{measure!r} is not a measure of {FINMARK_FLOW}; expected one "
                f"of {', '.join(sorted(set(MEASURES.values())))}"
            )
        if measure not in FINMARK_UNITS:
            raise KeyError(
                f"{measure!r} has no verified unit, so a key for it would be a "
                "guess; see FINMARK_UNITS"
            )
        key = ".".join((area, "", measure, FINMARK_UNITS[measure], "", "", "", "", ""))
        return FINMARK_FLOW, key

    def bts_key(self, currency: str) -> tuple[str, str]:
        """Build the flow and key for one currency's business confidence series.

        Args:
            currency: ISO 4217 code. The euro resolves to Germany, per
                `REF_AREA`.

        Returns:
            ``(flow, key)`` ready for `fetch_key`, with the frequency taken from
            `BTS_FREQUENCY` rather than left as a wildcard.

        Raises:
            KeyError: If the currency has no entry in `BTS_FREQUENCY`, or none
                in `REF_AREA`. A currency with no verified cadence cannot get a
                key here, because the alternative is to wildcard the frequency
                and let `parse_period` decide what a period was, which is how a
                quarter gets filed under a month.

                The cadence is checked first and deliberately. The two tables
                hold the same eight currencies today, so reading `REF_AREA`
                first made this message unreachable and left the bare lookup
                error standing in for it.

        """
        if currency not in BTS_FREQUENCY:
            raise KeyError(
                f"{currency!r} has no verified survey cadence in BTS_FREQUENCY, "
                "so a key for it would have to wildcard FREQ"
            )
        key = ".".join(
            (
                REF_AREA[currency],
                BTS_FREQUENCY[currency],
                BTS_MEASURE,
                BTS_UNIT_BALANCE,
                BTS_ACTIVITY_MANUFACTURING,
                BTS_ADJUSTMENT,
                "",
                "",
                "",
            )
        )
        return BTS_FLOW, key
