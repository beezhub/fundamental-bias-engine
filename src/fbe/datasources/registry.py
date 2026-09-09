"""Canonical indicator registry: the map from indicator keys to real series IDs.

This module is the single place where the engine's vocabulary meets the outside
world's. Pillars ask for ``cpi_yoy`` for ``"AUD"``; the registry says that means
FRED series ``CPALTT01AUQ659N``, in percent, quarterly, already expressed as a
year-on-year growth rate. Every data source translates into these keys before
returning `Observation`s, so no pillar ever sees a vendor identifier.

Why this file is worth reading carefully
----------------------------------------
A wrong series ID does not raise. It silently scores a currency off the wrong
number, and the error surfaces months later as an unexplained run of losses.
Every ID below was checked against the live source before being written down.
``SeriesRef.verified`` records that check, and ``SeriesRef.note`` records the
last observation the source actually held at verification time.

What ``verified`` means
-----------------------
``verified=True`` means the identifier was confirmed to exist and to return
observations from the named source. It does **not** mean the series is current.
Several families on FRED still resolve but stopped updating; those carry the
last observation date in ``note``. Read the note before trusting the ref.

The state of free G10 macro data, honestly
------------------------------------------
FRED is excellent for the United States and thin everywhere else, and the
thinness is not uniform:

* Interest rates travel well. The OECD ``IRLTLT01`` (10-year government bond)
  and ``IRSTCI01`` (overnight call money) families are current for most of the
  G10, two to three months behind.
* Prices do not. FRED's entire OECD-sourced CPI complex, both the index levels
  (``...CPIALLMINMEI``) and the year-on-year rates (``CPALTT01...``,
  ``CPGRLE01...``), stopped updating in March or April 2025. Outside the dollar
  and the euro, the engine has no free, current inflation print. This is the
  single largest gap in the registry and the main reason the manual source
  exists.
* Trade balances travel well: ``XTNTVA01...M667S`` is current for all eight.
* Current account balances are current only to late 2024 across the board.
* PMIs are absent entirely. S&P Global and ISM license those indices, so no
  free API carries them. Every PMI ref below is manual by necessity, not by
  oversight.
* The euro area is a special case. Eurostat feeds FRED with current HICP, but
  the euro-area aggregates for unemployment, employment, retail sales,
  industrial production, trade and the current account all stopped between 2022
  and 2023. Where that happens the registry falls back to the German national
  series as the euro-area proxy and says so in the note. Germany is roughly a
  third of euro-area GDP, so this is a real approximation, not a free lunch.

Two-year yields outside the United States are simply not on FRED in any form.
That matters, because the front end of the curve is where rate expectations
live and rate expectations are what move G10 FX. Treat the manual refs for
``yield_2y`` as load-bearing, not optional.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from fbe.types import Frequency, PillarName
from fbe.universe import G10

__all__ = [
    "GLOBAL",
    "INDICATORS",
    "IndicatorSpec",
    "SeriesRef",
    "SOURCE_CFTC",
    "SOURCE_FRED",
    "SOURCE_MANUAL",
    "SOURCE_STOOQ",
    "TRANSFORMS",
    "coverage_report",
    "indicators_for_pillar",
    "series_for",
]


GLOBAL = "GLOBAL"
"""Pseudo-currency for cross-market series such as VIX. Matches the convention
`Observation.currency` documents: a series that describes the whole market, not
one economy, is filed under ``"GLOBAL"`` rather than duplicated eight times."""

SOURCE_FRED = "fred"
SOURCE_CFTC = "cftc"
SOURCE_STOOQ = "stooq"
SOURCE_MANUAL = "manual"

TRANSFORMS: tuple[str, ...] = (
    "level",
    "yoy",
    "diff",
    "net_position",
)
"""Transform hints a source applies before emitting an `Observation`.

``level`` means the published number is already what the indicator asks for.
``yoy`` means the source publishes an index or a level and the caller must take
the year-on-year percentage change. ``diff`` means take the period-on-period
change, which is how a stock of employed persons becomes an employment change.
``net_position`` is the COT-specific reduction of long and short contract
counts to a single signed number.

The hint lives here rather than in the pillar because the choice is a property
of the series, not of the question being asked: ``CPIAUCSL`` is an index and
``NZLGDPRQPSMEI`` is already a growth rate, and only the registry knows which.
"""


@dataclass(frozen=True, slots=True)
class SeriesRef:
    """One source's identifier for one indicator for one currency.

    Attributes:
        source: Short source key matching a `DataSource.name`, one of
            ``"fred"``, ``"cftc"``, ``"stooq"`` or ``"manual"``.
        series_id: The source's own identifier. For FRED this is the series ID.
            For the CFTC it is the six-digit CFTC contract market code. For
            manual entries it is the key the operator writes in the YAML file.
        unit: Unit of the published value, echoed onto every `Observation`.
        frequency: How often the source publishes this particular series. This
            is authoritative and may differ from the parent `IndicatorSpec`,
            since the same indicator arrives monthly in one country and
            quarterly in another.
        transform: One of `TRANSFORMS`, describing what the source must do to
            the published number to produce the canonical indicator.
        verified: True when this identifier was confirmed against the live
            source. False marks a ref that could not be checked, which for this
            registry always means the data is not freely available and an
            operator must supply it by hand.
        note: Free text. By convention it records the last observation the
            source held at verification time, and any caveat about what the
            series actually measures.

    """

    source: str
    series_id: str
    unit: str
    frequency: Frequency
    transform: str = "level"
    verified: bool = True
    note: str = ""


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """Everything the engine knows about one canonical indicator.

    Attributes:
        key: The canonical indicator key, repeated here so a spec passed around
            on its own still knows its own name.
        pillar: Which pillar consumes this indicator. One pillar owns each
            indicator; an indicator wanted by two pillars is a sign the pillar
            boundary is drawn in the wrong place.
        unit: Canonical unit after ``transform`` is applied. This is what the
            pillar can assume, regardless of what the underlying source
            published.
        frequency: The indicator's typical release frequency across the
            universe. Per-currency reality lives on each `SeriesRef`.
        description: What the number means and why the pillar wants it.
        series: Per-currency refs. A currency absent from this mapping has no
            source at all for this indicator, which is different from having a
            manual one.

    """

    key: str
    pillar: PillarName
    unit: str
    frequency: Frequency
    description: str
    series: Mapping[str, SeriesRef] = field(default_factory=dict)


def _fred(
    series_id: str,
    unit: str,
    frequency: Frequency,
    transform: str = "level",
    note: str = "",
    verified: bool = True,
) -> SeriesRef:
    """Build a FRED `SeriesRef`. Exists only to keep the tables below readable."""
    return SeriesRef(
        source=SOURCE_FRED,
        series_id=series_id,
        unit=unit,
        frequency=frequency,
        transform=transform,
        verified=verified,
        note=note,
    )


def _cftc(series_id: str, note: str = "") -> SeriesRef:
    """Build a CFTC `SeriesRef` for a currency futures contract market code."""
    return SeriesRef(
        source=SOURCE_CFTC,
        series_id=series_id,
        unit="contracts",
        frequency=Frequency.WEEKLY,
        transform="net_position",
        verified=True,
        note=note,
    )


def _manual(
    series_id: str,
    unit: str,
    frequency: Frequency,
    note: str,
) -> SeriesRef:
    """Build a manual `SeriesRef`.

    Manual refs are always ``verified=False``. The flag is not a comment on the
    operator's typing: it records that no free machine-readable source was
    found, which is the fact a coverage report needs to surface.
    """
    return SeriesRef(
        source=SOURCE_MANUAL,
        series_id=series_id,
        unit=unit,
        frequency=frequency,
        transform="level",
        verified=False,
        note=note,
    )


# Recurring notes, written once so the tables stay scannable and so a change of
# fact is a change in one place.
_OECD_CPI_FROZEN = (
    "FRED's OECD CPI complex stopped updating in 2025-03/04; no free current "
    "series exists for this currency"
)
_EA_AGGREGATE_DEAD = (
    "euro-area aggregate on FRED stopped updating; German national series used "
    "as the euro-area proxy"
)
_PMI_LICENSED = (
    "PMIs are licensed by S&P Global and ISM and are on no free API; operator "
    "enters the headline print by hand"
)


POLICY_RATE = IndicatorSpec(
    key="policy_rate",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.DAILY,
    description=(
        "The central bank's target rate, or the overnight rate that tracks it. "
        "The level matters less than where it sits relative to the rest of the "
        "G10, which is the whole premise of a relative-value framework."
    ),
    series={
        "USD": _fred(
            "DFEDTARU",
            "percent",
            Frequency.DAILY,
            note="fed funds target range upper limit; current",
        ),
        "EUR": _fred(
            "ECBDFR",
            "percent",
            Frequency.DAILY,
            note="ECB deposit facility rate, the effective policy rate; current",
        ),
        "GBP": _fred(
            "IUDSOIA",
            "percent",
            Frequency.DAILY,
            note=(
                "SONIA, not Bank Rate itself, but it tracks Bank Rate within a "
                "few basis points; current"
            ),
        ),
        "JPY": _fred(
            "IRSTCI01JPM156N",
            "percent",
            Frequency.MONTHLY,
            note="call money rate, monthly average; last observation 2026-06",
        ),
        "CHF": _fred(
            "IRSTCI01CHM156N",
            "percent",
            Frequency.MONTHLY,
            note=(
                "DISCONTINUED, last observation 2024-03; use the manual SNB "
                "policy rate entry instead"
            ),
        ),
        "CAD": _fred(
            "IRSTCI01CAM156N",
            "percent",
            Frequency.MONTHLY,
            note="overnight money market rate; last observation 2026-06",
        ),
        "AUD": _fred(
            "IRSTCI01AUM156N",
            "percent",
            Frequency.MONTHLY,
            note="interbank overnight cash rate; last observation 2026-06",
        ),
        "NZD": _fred(
            "IRSTCI01NZM156N",
            "percent",
            Frequency.MONTHLY,
            note=(
                "DISCONTINUED, last observation 2024-12; use the manual RBNZ "
                "OCR entry instead"
            ),
        ),
    },
)


YIELD_2Y = IndicatorSpec(
    key="yield_2y",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.DAILY,
    description=(
        "Two-year government bond yield, the market's own forecast of where "
        "policy goes next. The 2y differential is the strongest single "
        "fundamental driver of a G10 pair over a multi-week horizon, which "
        "makes the seven missing legs below the registry's most expensive gap."
    ),
    series={
        "USD": _fred(
            "DGS2",
            "percent",
            Frequency.DAILY,
            note="Treasury constant maturity; current",
        ),
        "EUR": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "German 2y Schatz; no 2y series for any non-US G10 issuer exists "
            "on FRED, verified by search. Source from the Bundesbank or a "
            "broker terminal and enter by hand.",
        ),
        "GBP": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "2y gilt; see the EUR note, no free FRED series exists",
        ),
        "JPY": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "2y JGB; see the EUR note, no free FRED series exists",
        ),
        "CHF": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "2y Swiss Confederation; see the EUR note",
        ),
        "CAD": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "2y Government of Canada; see the EUR note",
        ),
        "AUD": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "2y Australian Commonwealth Government Bond; see the EUR note",
        ),
        "NZD": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "2y New Zealand Government Bond; see the EUR note",
        ),
    },
)


YIELD_10Y = IndicatorSpec(
    key="yield_10y",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Ten-year benchmark government bond yield. Slower than the 2y and less "
        "directly tied to policy, but it is the one rate with clean, current "
        "coverage across all eight currencies, so it carries the monetary "
        "pillar wherever the front end is missing."
    ),
    series={
        "USD": _fred(
            "DGS10",
            "percent",
            Frequency.DAILY,
            note="Treasury constant maturity, daily; current",
        ),
        "EUR": _fred(
            "IRLTLT01DEM156N",
            "percent",
            Frequency.MONTHLY,
            note=(
                "10y Bund, the euro-area benchmark; the EZ aggregate "
                "IRLTLT01EZM156N lags further. Last observation 2026-06."
            ),
        ),
        "GBP": _fred(
            "IRLTLT01GBM156N",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "JPY": _fred(
            "IRLTLT01JPM156N",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "CHF": _fred(
            "IRLTLT01CHM156N",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "CAD": _fred(
            "IRLTLT01CAM156N",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "AUD": _fred(
            "IRLTLT01AUM156N",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "NZD": _fred(
            "IRLTLT01NZM156N",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
    },
)


CPI_YOY = IndicatorSpec(
    key="cpi_yoy",
    pillar=PillarName.INFLATION,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Headline consumer price inflation, year on year. The inflation pillar "
        "scores the gap to each central bank's target rather than the raw "
        "print, so `CurrencyMeta.inflation_target` is the other half of this "
        "input."
    ),
    series={
        "USD": _fred(
            "CPIAUCSL",
            "index",
            Frequency.MONTHLY,
            transform="yoy",
            note="CPI-U all items, seasonally adjusted index; current",
        ),
        "EUR": _fred(
            "CP0000EZ19M086NEST",
            "index",
            Frequency.MONTHLY,
            transform="yoy",
            note="Eurostat HICP all items, euro area 19; current",
        ),
        "GBP": _fred(
            "CPALTT01GBM659N",
            "percent",
            Frequency.MONTHLY,
            note=f"DISCONTINUED, last observation 2025-03. {_OECD_CPI_FROZEN}",
        ),
        "JPY": _fred(
            "CPALTT01JPM659N",
            "percent",
            Frequency.MONTHLY,
            note=f"DISCONTINUED, last observation 2021-06. {_OECD_CPI_FROZEN}",
        ),
        "CHF": _fred(
            "CPALTT01CHM659N",
            "percent",
            Frequency.MONTHLY,
            note=f"DISCONTINUED, last observation 2025-04. {_OECD_CPI_FROZEN}",
        ),
        "CAD": _fred(
            "CPALTT01CAM659N",
            "percent",
            Frequency.MONTHLY,
            note=f"DISCONTINUED, last observation 2025-03. {_OECD_CPI_FROZEN}",
        ),
        "AUD": _fred(
            "CPALTT01AUQ659N",
            "percent",
            Frequency.QUARTERLY,
            note=f"DISCONTINUED, last observation 2025-01. {_OECD_CPI_FROZEN}",
        ),
        "NZD": _fred(
            "CPALTT01NZQ659N",
            "percent",
            Frequency.QUARTERLY,
            note=f"DISCONTINUED, last observation 2023-07. {_OECD_CPI_FROZEN}",
        ),
    },
)


CORE_CPI_YOY = IndicatorSpec(
    key="core_cpi_yoy",
    pillar=PillarName.INFLATION,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Consumer prices excluding food and energy, year on year. Central banks "
        "react to this more than to the headline, so it leads policy and "
        "therefore leads the currency."
    ),
    series={
        "USD": _fred(
            "CPILFESL",
            "index",
            Frequency.MONTHLY,
            transform="yoy",
            note="CPI-U less food and energy, SA index; current",
        ),
        "EUR": _fred(
            "00XEFDEZ19M086NEST",
            "index",
            Frequency.MONTHLY,
            transform="yoy",
            note=(
                "Eurostat HICP excluding energy, food, alcohol and tobacco; "
                "current"
            ),
        ),
        "GBP": _fred(
            "CPGRLE01GBM659N",
            "percent",
            Frequency.MONTHLY,
            note=f"DISCONTINUED, last observation 2025-03. {_OECD_CPI_FROZEN}",
        ),
        "JPY": _fred(
            "CPGRLE01JPM659N",
            "percent",
            Frequency.MONTHLY,
            note=f"DISCONTINUED, last observation 2021-06. {_OECD_CPI_FROZEN}",
        ),
        "CHF": _fred(
            "CPGRLE01CHM659N",
            "percent",
            Frequency.MONTHLY,
            note=f"DISCONTINUED, last observation 2025-04. {_OECD_CPI_FROZEN}",
        ),
        "CAD": _fred(
            "CPGRLE01CAM659N",
            "percent",
            Frequency.MONTHLY,
            note=f"DISCONTINUED, last observation 2025-03. {_OECD_CPI_FROZEN}",
        ),
        "AUD": _fred(
            "CPGRLE01AUQ659N",
            "percent",
            Frequency.QUARTERLY,
            note=f"DISCONTINUED, last observation 2025-01. {_OECD_CPI_FROZEN}",
        ),
        "NZD": _manual(
            "core_cpi_yoy",
            "percent",
            Frequency.QUARTERLY,
            "no core CPI series for New Zealand on FRED; the RBNZ sectoral "
            "factor model estimate is the usual substitute",
        ),
    },
)


GDP_YOY = IndicatorSpec(
    key="gdp_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.QUARTERLY,
    description=(
        "Real GDP growth, year on year. Slow and heavily revised, so it anchors "
        "the growth pillar rather than driving it. Full G10 coverage, which is "
        "rare enough in this registry to be worth stating."
    ),
    series={
        "USD": _fred(
            "GDPC1",
            "billions_chained_usd",
            Frequency.QUARTERLY,
            transform="yoy",
            note="real GDP, SAAR chained 2017 dollars; last observation 2026Q2",
        ),
        "EUR": _fred(
            "CLVMNACSCAB1GQEA19",
            "millions_chained_eur",
            Frequency.QUARTERLY,
            transform="yoy",
            note="Eurostat real GDP, euro area 19; last observation 2026Q2",
        ),
        "GBP": _fred(
            "NGDPRSAXDCGBQ",
            "millions_chained_gbp",
            Frequency.QUARTERLY,
            transform="yoy",
            note="last observation 2026Q2",
        ),
        "JPY": _fred(
            "JPNRGDPEXP",
            "billions_chained_jpy",
            Frequency.QUARTERLY,
            transform="yoy",
            note="real GDP by expenditure; last observation 2026Q2",
        ),
        "CHF": _fred(
            "CLVMNACSCAB1GQCH",
            "millions_chained_chf",
            Frequency.QUARTERLY,
            transform="yoy",
            note="last observation 2026Q2",
        ),
        "CAD": _fred(
            "NGDPRSAXDCCAQ",
            "millions_chained_cad",
            Frequency.QUARTERLY,
            transform="yoy",
            note="last observation 2026Q2",
        ),
        "AUD": _fred(
            "NGDPRSAXDCAUQ",
            "millions_chained_aud",
            Frequency.QUARTERLY,
            transform="yoy",
            note="last observation 2026Q2",
        ),
        "NZD": _fred(
            "NZLGDPRQPSMEI",
            "percent",
            Frequency.QUARTERLY,
            note=(
                "already published as a year-on-year growth rate, so no "
                "transform; last observation 2026Q1"
            ),
        ),
    },
)


UNEMPLOYMENT_RATE = IndicatorSpec(
    key="unemployment_rate",
    pillar=PillarName.EMPLOYMENT,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Harmonised unemployment rate. Compared cross-sectionally against the "
        "rest of the G10 and against its own recent trend, since the level that "
        "counts as full employment differs by country."
    ),
    series={
        "USD": _fred(
            "UNRATE",
            "percent",
            Frequency.MONTHLY,
            note="BLS headline U-3; current, roughly one month behind",
        ),
        "EUR": _fred(
            "LRHUTTTTDEM156S",
            "percent",
            Frequency.MONTHLY,
            note=(
                f"German harmonised rate. {_EA_AGGREGATE_DEAD} "
                "(LRHUTTTTEZM156S last observation 2023-01). "
                "Last observation 2026-06."
            ),
        ),
        "GBP": _fred(
            "LRHUTTTTGBM156S",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-04",
        ),
        "JPY": _fred(
            "LRHUTTTTJPM156S",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "CHF": _fred(
            "LRUN64TTCHQ156S",
            "percent",
            Frequency.QUARTERLY,
            note=(
                "quarterly ILO rate, aged 15-64; Switzerland publishes no "
                "monthly harmonised rate on FRED. Last observation 2026Q1."
            ),
        ),
        "CAD": _fred(
            "LRHUTTTTCAM156S",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-07",
        ),
        "AUD": _fred(
            "LRHUTTTTAUM156S",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "NZD": _fred(
            "LRHUTTTTNZQ156S",
            "percent",
            Frequency.QUARTERLY,
            note="quarterly by publication, not by choice; last observation 2026Q2",
        ),
    },
)


EMPLOYMENT_CHANGE = IndicatorSpec(
    key="employment_change",
    pillar=PillarName.EMPLOYMENT,
    unit="persons",
    frequency=Frequency.MONTHLY,
    description=(
        "Change in the number of people employed. The flow, not the stock: a "
        "falling unemployment rate driven by people leaving the labour force is "
        "a different signal from one driven by hiring, and this indicator is "
        "what separates them."
    ),
    series={
        "USD": _fred(
            "PAYEMS",
            "thousands_of_persons",
            Frequency.MONTHLY,
            transform="diff",
            note=(
                "total nonfarm payrolls; the differenced level is the NFP "
                "headline. Current."
            ),
        ),
        "EUR": _manual(
            "employment_change",
            "persons",
            Frequency.QUARTERLY,
            "no live euro-area or German employment level on FRED "
            "(LFEMTTTTEZQ647S last observation 2022-10); take the Eurostat "
            "quarterly employment release by hand",
        ),
        "GBP": _fred(
            "LFEMTTTTGBQ647S",
            "persons",
            Frequency.QUARTERLY,
            transform="diff",
            note="last observation 2026Q1",
        ),
        "JPY": _fred(
            "LFEMTTTTJPM647S",
            "persons",
            Frequency.MONTHLY,
            transform="diff",
            note="last observation 2026-06",
        ),
        "CHF": _fred(
            "LFEMTTTTCHQ647S",
            "persons",
            Frequency.QUARTERLY,
            transform="diff",
            note="last observation 2026Q1",
        ),
        "CAD": _fred(
            "LFEMTTTTCAM647S",
            "persons",
            Frequency.MONTHLY,
            transform="diff",
            note="last observation 2026-07",
        ),
        "AUD": _fred(
            "LFEMTTTTAUM647S",
            "persons",
            Frequency.MONTHLY,
            transform="diff",
            note="last observation 2026-06",
        ),
        "NZD": _fred(
            "LFEMTTTTNZQ647S",
            "persons",
            Frequency.QUARTERLY,
            transform="diff",
            note="last observation 2026Q2",
        ),
    },
)


RETAIL_SALES_YOY = IndicatorSpec(
    key="retail_sales_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Retail trade volume, year on year. The fastest read on household "
        "demand, and the growth pillar's main monthly input given that GDP "
        "arrives quarterly and late."
    ),
    series={
        "USD": _fred(
            "USASLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note=(
                "OECD retail volume growth, chosen over the fresher US-only "
                "RSAFS so the eight legs are measured the same way. Last "
                "observation 2026-05."
            ),
        ),
        "EUR": _fred(
            "DEUSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note=(
                f"{_EA_AGGREGATE_DEAD} (EA19SLRTTO01GYSAM last observation "
                "2023-10). Last observation 2026-05."
            ),
        ),
        "GBP": _fred(
            "GBRSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "JPY": _fred(
            "JPNSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-05",
        ),
        "CHF": _fred(
            "CHESLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-05",
        ),
        "CAD": _fred(
            "CANSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-04",
        ),
        "AUD": _fred(
            "SLRTTO01AUQ659S",
            "percent",
            Frequency.QUARTERLY,
            note=(
                "DISCONTINUED, last observation 2025Q2; Australia has no live "
                "retail series on FRED"
            ),
        ),
        "NZD": _fred(
            "SLRTTO01NZQ659S",
            "percent",
            Frequency.QUARTERLY,
            note="last observation 2026Q1",
        ),
    },
)


INDUSTRIAL_PRODUCTION_YOY = IndicatorSpec(
    key="industrial_production_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Industrial production, year on year. Coverage here is the worst of the "
        "growth inputs: four of eight are live. Weight it accordingly, or the "
        "growth pillar ends up scoring the countries that happen to publish "
        "rather than the countries that happen to be growing."
    ),
    series={
        "USD": _fred(
            "USAPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note=(
                "OECD basis for cross-country comparability; INDPRO is the "
                "fresher US-only alternative. Last observation 2026-06."
            ),
        ),
        "EUR": _fred(
            "DEUPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note=(
                f"DISCONTINUED, last observation 2023-12. {_EA_AGGREGATE_DEAD}, "
                "and the German proxy has now stopped too"
            ),
        ),
        "GBP": _fred(
            "GBRPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-05",
        ),
        "JPY": _fred(
            "JPNPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-05",
        ),
        "CHF": _manual(
            "industrial_production_yoy",
            "percent",
            Frequency.QUARTERLY,
            "no Swiss industrial production series on FRED in any live form",
        ),
        "CAD": _fred(
            "CANPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            note="last observation 2026-04",
        ),
        "AUD": _manual(
            "industrial_production_yoy",
            "percent",
            Frequency.QUARTERLY,
            "no Australian industrial production series on FRED",
        ),
        "NZD": _manual(
            "industrial_production_yoy",
            "percent",
            Frequency.QUARTERLY,
            "no New Zealand industrial production series on FRED",
        ),
    },
)


PMI_MANUFACTURING = IndicatorSpec(
    key="pmi_manufacturing",
    pillar=PillarName.GROWTH,
    unit="index",
    frequency=Frequency.MONTHLY,
    description=(
        "Manufacturing purchasing managers' index, 50 being the expansion line. "
        "The best leading indicator in the growth pillar and the one with zero "
        "free coverage, which is why the manual source exists at all."
    ),
    series={
        code: _manual("pmi_manufacturing", "index", Frequency.MONTHLY, _PMI_LICENSED)
        for code in G10
    },
)


TRADE_BALANCE = IndicatorSpec(
    key="trade_balance",
    pillar=PillarName.EXTERNAL,
    unit="usd",
    frequency=Frequency.MONTHLY,
    description=(
        "Merchandise trade balance in US dollars, seasonally adjusted. Already "
        "currency-converted by the source, so the eight legs are directly "
        "comparable without an FX step. Full, current G10 coverage."
    ),
    series={
        "USD": _fred(
            "XTNTVA01USM667S",
            "usd",
            Frequency.MONTHLY,
            note=(
                "OECD basis for comparability; BOPGSTB is the fresher US-only "
                "goods and services balance. Last observation 2026-06."
            ),
        ),
        "EUR": _fred(
            "XTNTVA01DEM667S",
            "usd",
            Frequency.MONTHLY,
            note=(
                f"{_EA_AGGREGATE_DEAD} (XTNTVA01EZM667S last observation "
                "2022-12). Germany runs a structural surplus larger than the "
                "bloc's, so this proxy flatters the euro. Last observation "
                "2026-05."
            ),
        ),
        "GBP": _fred(
            "XTNTVA01GBM667S",
            "usd",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "JPY": _fred(
            "XTNTVA01JPM667S",
            "usd",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "CHF": _fred(
            "XTNTVA01CHM667S",
            "usd",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "CAD": _fred(
            "XTNTVA01CAM667S",
            "usd",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "AUD": _fred(
            "XTNTVA01AUM667S",
            "usd",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
        "NZD": _fred(
            "XTNTVA01NZM667S",
            "usd",
            Frequency.MONTHLY,
            note="last observation 2026-06",
        ),
    },
)


CURRENT_ACCOUNT = IndicatorSpec(
    key="current_account",
    pillar=PillarName.EXTERNAL,
    unit="percent_of_gdp",
    frequency=Frequency.QUARTERLY,
    description=(
        "Current account balance as a share of GDP. A structural measure of "
        "whether a currency is financed by the world or financing it. It moves "
        "slowly, which is fortunate, because every leg below stopped updating "
        "in late 2024."
    ),
    series={
        "USD": _fred(
            "USAB6BLTT02STSAQ",
            "percent_of_gdp",
            Frequency.QUARTERLY,
            note="last observation 2024Q4 across the whole family",
        ),
        "EUR": _fred(
            "DEUB6BLTT02STSAQ",
            "percent_of_gdp",
            Frequency.QUARTERLY,
            note=(
                f"{_EA_AGGREGATE_DEAD} (EA19B6BLTT02STSAQ last observation "
                "2022Q4). Last observation 2024Q4."
            ),
        ),
        "GBP": _fred(
            "GBRB6BLTT02STSAQ",
            "percent_of_gdp",
            Frequency.QUARTERLY,
            note="last observation 2024Q4",
        ),
        "JPY": _fred(
            "JPNB6BLTT02STSAQ",
            "percent_of_gdp",
            Frequency.QUARTERLY,
            note="last observation 2024Q4",
        ),
        "CHF": _fred(
            "CHEB6BLTT02STSAQ",
            "percent_of_gdp",
            Frequency.QUARTERLY,
            note="last observation 2024Q4",
        ),
        "CAD": _fred(
            "CANB6BLTT02STSAQ",
            "percent_of_gdp",
            Frequency.QUARTERLY,
            note="last observation 2024Q4",
        ),
        "AUD": _fred(
            "AUSB6BLTT02STSAQ",
            "percent_of_gdp",
            Frequency.QUARTERLY,
            note="last observation 2024Q4",
        ),
        "NZD": _fred(
            "NZLB6BLTT02STSAQ",
            "percent_of_gdp",
            Frequency.QUARTERLY,
            note="last observation 2024Q4",
        ),
    },
)


COT_NET_POSITION = IndicatorSpec(
    key="cot_net_position",
    pillar=PillarName.POSITIONING,
    unit="contracts",
    frequency=Frequency.WEEKLY,
    description=(
        "Net speculative position in CME currency futures from the CFTC "
        "Commitments of Traders report. A crowded position is a reason to fade "
        "a fundamental view, not to add to it, so this pillar usually works "
        "against the others by design."
    ),
    series={
        "USD": _cftc(
            "098662",
            "USD Index on ICE, in the Legacy report (6dca-aqww), not TFF. The "
            "primary dollar read is the sign-flipped complement of the other "
            "seven; this contract is a small, thinly held cross-check.",
        ),
        "EUR": _cftc("099741", "EURO FX, CME, TFF dataset gpe5-46if"),
        "GBP": _cftc("096742", "BRITISH POUND, CME"),
        "JPY": _cftc("097741", "JAPANESE YEN, CME"),
        "CHF": _cftc("092741", "SWISS FRANC, CME"),
        "CAD": _cftc("090741", "CANADIAN DOLLAR, CME"),
        "AUD": _cftc("232741", "AUSTRALIAN DOLLAR, CME"),
        "NZD": _cftc("112741", "NZ DOLLAR, CME"),
    },
)


EQUITY_INDEX = IndicatorSpec(
    key="equity_index",
    pillar=PillarName.RISK,
    unit="index",
    frequency=Frequency.DAILY,
    description=(
        "Benchmark equity index for each economy. Feeds the risk pillar in two "
        "ways: as a proxy for the local growth and earnings picture, and, in "
        "concert with `CurrencyMeta.risk_beta`, as a read on whether the market "
        "is in risk-on or risk-off."
    ),
    series={
        "USD": _fred(
            "SP500",
            "index",
            Frequency.DAILY,
            note="daily close; FRED holds a rolling ten-year window only",
        ),
        "JPY": _fred(
            "NIKKEI225",
            "index",
            Frequency.DAILY,
            note="daily close; current",
        ),
        "EUR": _fred(
            "SPASTT01DEM661N",
            "index",
            Frequency.MONTHLY,
            note=(
                "OECD share price index, monthly average, 2015=100. Monthly is "
                "too slow for a risk pillar; prefer the Stooq daily feed and "
                "keep this as the offline fallback. Last observation 2026-06."
            ),
        ),
        "GBP": _fred(
            "SPASTT01GBM661N",
            "index",
            Frequency.MONTHLY,
            note="OECD share price index; last observation 2026-06",
        ),
        "CHF": _fred(
            "SPASTT01CHM661N",
            "index",
            Frequency.MONTHLY,
            note="OECD share price index; last observation 2026-06",
        ),
        "CAD": _fred(
            "SPASTT01CAM661N",
            "index",
            Frequency.MONTHLY,
            note="OECD share price index; last observation 2026-06",
        ),
        "AUD": _fred(
            "SPASTT01AUM661N",
            "index",
            Frequency.MONTHLY,
            note="OECD share price index; last observation 2026-06",
        ),
        "NZD": _fred(
            "SPASTT01NZM661N",
            "index",
            Frequency.MONTHLY,
            note="OECD share price index; last observation 2026-06",
        ),
    },
)


VIX = IndicatorSpec(
    key="vix",
    pillar=PillarName.RISK,
    unit="index",
    frequency=Frequency.DAILY,
    description=(
        "CBOE implied volatility on the S&P 500. A single global number, not a "
        "per-currency one: it sets the risk regime, and the currencies then "
        "sort themselves by `CurrencyMeta.risk_beta`."
    ),
    series={
        GLOBAL: _fred(
            "VIXCLS",
            "index",
            Frequency.DAILY,
            note="daily close; current",
        ),
    },
)


COMMODITY_INDEX = IndicatorSpec(
    key="commodity_index",
    pillar=PillarName.EXTERNAL,
    unit="index",
    frequency=Frequency.MONTHLY,
    description=(
        "Terms-of-trade proxy for the commodity currencies, plus a global "
        "benchmark. Only the three currencies with a `commodity_link` in "
        "`CurrencyMeta` carry a specific ref; the others take the global index "
        "or nothing."
    ),
    series={
        GLOBAL: _fred(
            "PALLFNFINDEXM",
            "index",
            Frequency.MONTHLY,
            note="IMF all commodity price index, 2016=100; last obs 2026-07",
        ),
        "CAD": _fred(
            "DCOILWTICO",
            "usd_per_barrel",
            Frequency.DAILY,
            note="WTI spot, the standard Canadian dollar terms-of-trade proxy",
        ),
        "AUD": _fred(
            "PIORECRUSDM",
            "index",
            Frequency.MONTHLY,
            note="IMF iron ore price index; last observation 2026-07",
        ),
        "NZD": _manual(
            "commodity_index",
            "index",
            Frequency.IRREGULAR,
            "no dairy price index on FRED, verified by search. The GlobalDairy "
            "Trade auction index is the right series and is published "
            "fortnightly on globaldairytrade.info; enter it by hand. "
            "PFOODINDEXM is a poor but free substitute.",
        ),
    },
)


INDICATORS: Mapping[str, IndicatorSpec] = {
    spec.key: spec
    for spec in (
        POLICY_RATE,
        YIELD_2Y,
        YIELD_10Y,
        CPI_YOY,
        CORE_CPI_YOY,
        GDP_YOY,
        UNEMPLOYMENT_RATE,
        EMPLOYMENT_CHANGE,
        RETAIL_SALES_YOY,
        INDUSTRIAL_PRODUCTION_YOY,
        PMI_MANUFACTURING,
        TRADE_BALANCE,
        CURRENT_ACCOUNT,
        COT_NET_POSITION,
        EQUITY_INDEX,
        VIX,
        COMMODITY_INDEX,
    )
}
"""The registry. Keyed by canonical indicator key; this is what `Pillar.requires`
entries name and what `Observation.indicator` carries."""


def series_for(indicator: str, currency: str) -> SeriesRef | None:
    """Look up the source reference for one indicator and one currency.

    Args:
        indicator: Canonical indicator key, e.g. ``"cpi_yoy"``.
        currency: ISO 4217 code, or ``"GLOBAL"`` for cross-market series.

    Returns:
        The `SeriesRef`, or ``None`` when the registry has no source for this
        pair. ``None`` and a ref with ``verified=False`` mean different things:
        the first is silence, the second is a known gap with a named fallback.

    Raises:
        KeyError: If ``indicator`` is not a registered indicator key. An
            unknown indicator is a programming error, not missing data, so it
            fails loudly rather than returning ``None``.
    """
    spec = INDICATORS[indicator]
    return spec.series.get(currency.upper())


def indicators_for_pillar(pillar: PillarName) -> tuple[str, ...]:
    """Return the indicator keys one pillar consumes, in registry order.

    Args:
        pillar: The pillar to look up.

    Returns:
        Indicator keys, which a `Pillar` implementation can use directly as its
        ``requires`` sequence.
    """
    return tuple(key for key, spec in INDICATORS.items() if spec.pillar is pillar)


def coverage_report() -> Mapping[str, float]:
    """Report the fraction of the G10 each indicator covers with a real source.

    Coverage counts a currency only when its ref is ``verified`` and does not
    come from the manual source, because a manual ref describes work an
    operator has to do rather than data the engine can fetch. An indicator
    holding a ``GLOBAL`` ref covers the whole universe by construction, since
    one VIX print serves all eight currencies.

    Note that coverage says nothing about freshness. Several indicators score
    1.0 here while resting on series that stopped updating in 2024 or 2025; the
    ``note`` on each `SeriesRef` is where that shows up, and the staleness
    penalty in the scoring layer is what acts on it.

    Returns:
        Indicator key to fraction in ``0.0..1.0``.
    """
    report: dict[str, float] = {}
    for key, spec in INDICATORS.items():
        global_ref = spec.series.get(GLOBAL)
        if global_ref is not None and global_ref.verified:
            report[key] = 1.0
            continue
        covered = sum(
            1
            for code in G10
            if (ref := spec.series.get(code)) is not None
            and ref.verified
            and ref.source != SOURCE_MANUAL
        )
        report[key] = covered / len(G10)
    return report
