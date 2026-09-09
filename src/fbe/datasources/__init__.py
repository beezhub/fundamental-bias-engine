"""Data sources: everything that turns the outside world into `Observation`s.

Five sources, mapped onto the seven pillars:

===============  ==========================================  ==================
Source           Backs                                       Credential
===============  ==========================================  ==================
`FredSource`     MONETARY, INFLATION, GROWTH, EMPLOYMENT,    free API key
                 EXTERNAL
`CotSource`      POSITIONING                                 none
`PricesSource`   RISK, EXTERNAL commodity proxies            none
`CalendarSource` no pillar; feeds the news blackout          none
`ManualSource`   whatever the others cannot supply           none
===============  ==========================================  ==================

`registry` is the module to read first. It maps every canonical indicator key to
a real, verified series identifier per currency, and it is the only place that
knows a vendor's vocabulary. Everything else in this package speaks the
registry's keys.

Only FRED needs a credential. `DataConfig.fred_api_key` carries it, read from
``FRED_API_KEY`` in the environment.

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
from fbe.datasources.fred import FredSource
from fbe.datasources.manual import ManualSource
from fbe.datasources.prices import PricesSource
from fbe.datasources.registry import (
    GLOBAL,
    INDICATORS,
    IndicatorSpec,
    SeriesRef,
    coverage_report,
    indicators_for_pillar,
    series_for,
)

__all__ = [
    "ALL_SOURCES",
    "BaseDataSource",
    "CacheEntry",
    "CacheMiss",
    "CalendarSource",
    "CotSource",
    "DiskCache",
    "FredSource",
    "GLOBAL",
    "INDICATORS",
    "IndicatorSpec",
    "ManualSource",
    "PricesSource",
    "RateLimit",
    "RetryPolicy",
    "SeriesRef",
    "SourceError",
    "coverage_report",
    "indicators_for_pillar",
    "series_for",
]

ALL_SOURCES: tuple[type[BaseDataSource], ...] = (
    FredSource,
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
