"""Data sources: everything that turns the outside world into `Observation`s.

Seven sources, mapped onto the seven pillars:

===============  ==========================================  ==================
Source           Backs                                       Credential
===============  ==========================================  ==================
`FredSource`     MONETARY, GROWTH, EMPLOYMENT, EXTERNAL,     free API key
                 US and euro-area INFLATION
`OecdSource`     INFLATION for six currencies, and the       none
                 parts of MONETARY and RISK where FRED's
                 mirror runs months behind
`CurvesSource`   the 2-year yields at the heart of           none
                 MONETARY, from central banks direct
`CotSource`      POSITIONING                                 none
`PricesSource`   RISK, EXTERNAL commodity proxies            none
`CalendarSource` no pillar; feeds the news blackout          none
`ManualSource`   whatever the others cannot supply           none
===============  ==========================================  ==================

Three sources rather than one because FRED, which mirrors OECD statistics,
stopped updating several of them: its CPI complex ends in early 2025 and its
Japanese CPI in 2021, while every one of those series still resolves and
returns data. `OecdSource` reads the same material from the OECD directly and
is current. `CurvesSource` exists because FRED carries no 2-year government
yield for any non-US G10 issuer, and the 2-year is what the monetary pillar is
mostly made of.

`registry` is the module to read first. It maps every canonical indicator key to
a real, verified series identifier per currency, and it is the only place that
knows a vendor's vocabulary. Everything else in this package speaks the
registry's keys.

Only FRED needs a credential. `DataConfig.fred_api_key` carries it, read from
``FRED_API_KEY`` in the environment. Every other source is open, which also
means every other source can be checked by anyone reading this without asking
for access first.

Operator documentation, including how to get the key, what each source's terms
allow, the full indicator table with a verified column, and the known gaps,
lives in ``docs/data-sources.md``.
"""

from __future__ import annotations

from fbe.datasources.base import (
    BaseDataSource,
    RateLimit,
    RetryPolicy,
    SourceError,
)
from fbe.datasources.cache import CacheEntry, CacheMiss, DiskCache
from fbe.datasources.calendar import CalendarSource
from fbe.datasources.cot import CotSource
from fbe.datasources.curves import CurvesSource
from fbe.datasources.fred import FredSource
from fbe.datasources.manual import ManualSource
from fbe.datasources.oecd import OecdSource
from fbe.datasources.prices import PricesSource
from fbe.datasources.registry import (
    GLOBAL,
    INDICATORS,
    VERIFIED_ON,
    IndicatorSpec,
    SeriesRef,
    coverage_report,
    identifier_coverage,
    indicators_for_pillar,
    series_for,
    stale_refs,
)

__all__ = [
    "ALL_SOURCES",
    "BaseDataSource",
    "CacheEntry",
    "CacheMiss",
    "CalendarSource",
    "CotSource",
    "CurvesSource",
    "DiskCache",
    "FredSource",
    "GLOBAL",
    "INDICATORS",
    "IndicatorSpec",
    "ManualSource",
    "OecdSource",
    "PricesSource",
    "RateLimit",
    "RetryPolicy",
    "SeriesRef",
    "SourceError",
    "VERIFIED_ON",
    "coverage_report",
    "identifier_coverage",
    "indicators_for_pillar",
    "series_for",
    "stale_refs",
]

ALL_SOURCES: tuple[type[BaseDataSource], ...] = (
    FredSource,
    OecdSource,
    CurvesSource,
    CotSource,
    PricesSource,
    CalendarSource,
    ManualSource,
)
"""Every source class, in the order a collector should try them.

Order matters in one place: `ManualSource` is last so that an operator's entry
overrides a fetched value for the same ``(indicator, currency, period)``. That
is the point of an override, and it is also how a known-bad vendor print gets
corrected without patching the registry."""
