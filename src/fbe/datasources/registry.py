"""Canonical indicator registry: the map from indicator keys to real series IDs.

This module is the single place where the engine's vocabulary meets the outside
world's. Pillars ask for ``yield_2y`` for ``"CAD"``; the registry says that
means Bank of Canada Valet series ``BD.CDN.2YR.DQ.YLD``, in percent, daily,
as published. Every data source translates into these keys before returning
`Observation`s, so no pillar ever sees a vendor identifier.

Why this file is worth reading carefully
----------------------------------------
A wrong series ID does not raise. It silently scores a currency off the wrong
number, and the error surfaces months later as an unexplained run of losses.
Every ID below was checked against the live source before being written down.
``SeriesRef.verified`` records that check and ``SeriesRef.last_observed``
records the newest observation the source actually held when it was made.

Freshness is a first-class fact here, not a comment
---------------------------------------------------
An identifier that resolves is not the same as an identifier that updates. FRED
in particular is full of series that answer happily and stopped publishing in
2024 or 2025. `coverage_report` is therefore freshness-aware by default: a ref
counts toward coverage only if it was verified, is not manual, and its
``last_observed`` falls inside the indicator's ``max_staleness_days``. The old
identifier-only count is still available as `identifier_coverage`, clearly
named so nobody reaches for it by accident.

``max_staleness_days`` lives on the `IndicatorSpec` because the registry is
where the release calendar is known. A quarterly balance-of-payments figure
cannot be 45 days old and a daily bond yield should never be, and only this
module knows which is which.

Where the data comes from, and why not all from one place
----------------------------------------------------------
FRED is the widest single free source and is still the backbone, but it mirrors
the OECD with a long and uneven delay, and it carries no non-US front-end
yields at all. Two additions fix most of that:

* **The OECD's own SDMX API** carries current data where FRED's mirror of the
  same OECD material is frozen. FRED's CPI complex stops in March or April
  2025; the OECD API serves the same countries through July or August 2026. It
  also runs two months ahead of FRED on policy rates, 10-year yields and share
  price indices. Anywhere this registry used to point at a frozen FRED OECD
  series, it now points at the OECD directly.
* **National central banks and debt offices** publish their own curves, free
  and without a key. That is where the 2-year yields come from: Bank of Canada,
  the ECB Data Portal, Japan's Ministry of Finance, the Bank of England and the
  Reserve Bank of Australia between them cover six of the eight.

What is still missing, stated plainly
--------------------------------------
* **CHF and NZD 2-year yields.** The SNB publishes a Confederation spot curve
  and the endpoint is verified, but the cube stopped at 2025-07-31 while the
  rest of the SNB portal stayed current. The RBNZ and New Zealand Debt
  Management both refuse automated requests outright. Both are manual.
* **PMIs**, all eight. Licensed by S&P Global and ISM, on no free API.
* **Current account**, all eight. The FRED family stopped at 2024Q4 and no free
  replacement was found. The staleness allowance below is set to what a
  quarterly balance-of-payments release honestly justifies, which means the
  frozen series correctly fails it rather than being waved through.
* **Industrial production** for CHF, AUD and NZD, and the euro-area aggregate.
* **Euro-area aggregates** for several indicators, where the registry
  substitutes German national series and says so in the note.

Verification date for everything below: 2026-09-09.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from fbe.types import Frequency, PillarName
from fbe.universe import G10

__all__ = [
    "GLOBAL",
    "INDICATORS",
    "IndicatorSpec",
    "SeriesRef",
    "SOURCE_BOC",
    "SOURCE_BOE",
    "SOURCE_CFTC",
    "SOURCE_ECB",
    "SOURCE_FRED",
    "SOURCE_MANUAL",
    "SOURCE_MOF_JP",
    "SOURCE_OECD",
    "SOURCE_RBA",
    "SOURCE_SNB",
    "SOURCE_STOOQ",
    "TRANSFORMS",
    "VERIFIED_ON",
    "coverage_report",
    "identifier_coverage",
    "indicators_for_pillar",
    "series_for",
    "stale_refs",
]


VERIFIED_ON = date(2026, 9, 9)
"""When every identifier below was last checked against its live source. The
``last_observed`` dates are as at this date, so freshness computed against a
much later ``asof`` is measuring the age of this file as much as the data."""

GLOBAL = "GLOBAL"
"""Pseudo-currency for cross-market series such as VIX. Matches the convention
`Observation.currency` documents: a series that describes the whole market, not
one economy, is filed under ``"GLOBAL"`` rather than duplicated eight times."""

SOURCE_FRED = "fred"
SOURCE_OECD = "oecd"
SOURCE_CFTC = "cftc"
SOURCE_STOOQ = "stooq"
SOURCE_MANUAL = "manual"
SOURCE_BOC = "boc"
SOURCE_ECB = "ecb"
SOURCE_MOF_JP = "mof_jp"
SOURCE_BOE = "boe"
SOURCE_RBA = "rba"
SOURCE_SNB = "snb"

CURVE_SOURCES: frozenset[str] = frozenset(
    {SOURCE_BOC, SOURCE_ECB, SOURCE_MOF_JP, SOURCE_BOE, SOURCE_RBA, SOURCE_SNB}
)
"""The central bank and debt office sources, all served by
`fbe.datasources.curves`. Grouped because they share one thing that matters:
each publishes only its own country, so none of them can ever be a fallback for
another, and losing one is losing a currency rather than degrading a series."""

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
the OECD's ``GY`` transformation is already a growth rate, and only the
registry knows which.
"""


@dataclass(frozen=True, slots=True)
class SeriesRef:
    """One source's identifier for one indicator for one currency.

    Attributes:
        source: Short source key matching a `DataSource.name`.
        series_id: The source's own identifier. FRED uses a series ID, the CFTC
            a six-digit contract market code, the OECD a ``flow/key`` pair, the
            curve sources whatever their own API takes. `fbe.datasources.curves`
            and `fbe.datasources.oecd` document how each is composed.
        unit: Unit of the published value, echoed onto every `Observation`.
        frequency: How often the source publishes this particular series. This
            is authoritative and may differ from the parent `IndicatorSpec`,
            since the same indicator arrives monthly in one country and
            quarterly in another.
        transform: One of `TRANSFORMS`, describing what the source must do to
            the published number to produce the canonical indicator.
        verified: True when this identifier was confirmed against the live
            source on `VERIFIED_ON`. False marks a ref that could not be
            checked, which in this registry means either that no free source
            exists or that the source refused an automated request.
        last_observed: Newest observation the source held on `VERIFIED_ON`.
            ``None`` means freshness could not be established, which
            `coverage_report` treats as not fresh. This is the field that stops
            a frozen series from counting as coverage.
        note: Free text: what the series actually measures, and any caveat.

    """

    source: str
    series_id: str
    unit: str
    frequency: Frequency
    transform: str = "level"
    verified: bool = True
    last_observed: date | None = None
    note: str = ""

    @property
    def fetchable(self) -> bool:
        """True when a machine can retrieve this without an operator typing it."""
        return self.verified and self.source != SOURCE_MANUAL

    def stale_on(self, asof: date, max_staleness_days: int) -> bool:
        """Say whether this ref's newest observation is too old to use.

        Args:
            asof: The date to age against.
            max_staleness_days: The indicator's allowance.

        Returns:
            True when the ref is stale, including when ``last_observed`` is
            unknown. Unknown freshness counts as stale on purpose: an
            unverifiable number should not quietly earn a currency a score.

        """
        if self.last_observed is None:
            return True
        return (asof - self.last_observed).days > max_staleness_days


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
        max_staleness_days: How old this indicator's newest observation may be
            and still count. Set per indicator rather than globally because the
            release calendar differs by an order of magnitude across this
            table: a 2-year yield stale by a week means the feed broke, while a
            quarterly balance-of-payments figure is routinely five months old
            on the day it is most current. This overrides
            `ScoringConfig.max_staleness_days` for this indicator.

            Each value is derived from the publication cadence, never from what
            the data happens to need: it is the age of the newest print on the
            day before the next one is due, which is the period length, plus
            the statistics office's lag, plus one more period. A quarterly
            series stamped at the start of its quarter therefore earns about
            270 days and a lagging monthly series about 180. Setting an
            allowance higher than the cadence justifies, so that a frozen
            series passes, converts a visible gap into an invisible one and is
            the single easiest way to make this whole registry lie.
        description: What the number means and why the pillar wants it.
        series: Per-currency refs. A currency absent from this mapping has no
            source at all for this indicator, which is different from having a
            manual one.

    """

    key: str
    pillar: PillarName
    unit: str
    frequency: Frequency
    max_staleness_days: int
    description: str
    series: Mapping[str, SeriesRef] = field(default_factory=dict)


def _ref(
    source: str,
    series_id: str,
    unit: str,
    frequency: Frequency,
    last_observed: date | None,
    transform: str = "level",
    note: str = "",
    verified: bool = True,
) -> SeriesRef:
    """Build a `SeriesRef`. Exists only to keep the tables below readable."""
    return SeriesRef(
        source=source,
        series_id=series_id,
        unit=unit,
        frequency=frequency,
        transform=transform,
        verified=verified,
        last_observed=last_observed,
        note=note,
    )


def _manual(
    series_id: str,
    unit: str,
    frequency: Frequency,
    note: str,
) -> SeriesRef:
    """Build a manual `SeriesRef`.

    Manual refs are always unverified with no ``last_observed``. Neither flag
    comments on the operator's typing: together they record that no free
    machine-readable source was found, which is the fact a coverage report
    needs to surface.
    """
    return SeriesRef(
        source=SOURCE_MANUAL,
        series_id=series_id,
        unit=unit,
        frequency=frequency,
        transform="level",
        verified=False,
        last_observed=None,
        note=note,
    )


def _cftc(series_id: str, last_observed: date, note: str = "") -> SeriesRef:
    """Build a CFTC `SeriesRef` for a currency futures contract market code."""
    return SeriesRef(
        source=SOURCE_CFTC,
        series_id=series_id,
        unit="contracts",
        frequency=Frequency.WEEKLY,
        transform="net_position",
        verified=True,
        last_observed=last_observed,
        note=note,
    )


# Recurring notes, written once so the tables stay scannable and so a change of
# fact is a change in one place.
_EA_AGGREGATE_DEAD = (
    "euro-area aggregate stopped updating; German national series used as the "
    "euro-area proxy"
)
_PMI_LICENSED = (
    "PMIs are licensed by S&P Global and ISM and are on no free API; operator "
    "enters the headline print by hand"
)
_OECD_FRESHER = (
    "taken from the OECD API rather than FRED's mirror of the same OECD "
    "material, which runs two months behind"
)


POLICY_RATE = IndicatorSpec(
    key="policy_rate",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.DAILY,
    max_staleness_days=75,
    description=(
        "The central bank's target rate, or the overnight rate that tracks it. "
        "The level matters less than where it sits relative to the rest of the "
        "G10, which is the whole premise of a relative-value framework."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "DFEDTARU",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 9),
            note="fed funds target range upper limit, published daily",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "ECBDFR",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 9),
            note="ECB deposit facility rate, the effective policy rate",
        ),
        "GBP": _ref(
            SOURCE_BOE,
            "IUDBEDR",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 1),
            note=(
                "Bank Rate itself from the Bank of England database, which "
                "replaces the earlier SONIA proxy"
            ),
        ),
        "JPY": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/JPN.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"uncollateralised overnight call rate; {_OECD_FRESHER}",
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CHE.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=(
                "SARON-area overnight rate. Closes a real gap: FRED's "
                "IRSTCI01CHM156N stopped at 2024-03."
            ),
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CAN.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"overnight money market rate; {_OECD_FRESHER}",
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/AUS.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"interbank overnight cash rate; {_OECD_FRESHER}",
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/NZL.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=(
                "overnight interbank rate. Closes a real gap: FRED's "
                "IRSTCI01NZM156N stopped at 2024-12."
            ),
        ),
    },
)


YIELD_2Y = IndicatorSpec(
    key="yield_2y",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.DAILY,
    max_staleness_days=10,
    description=(
        "Two-year government bond yield, the market's own forecast of where "
        "policy goes next. The 2y differential is the strongest single "
        "fundamental driver of a G10 pair over a multi-week horizon, and the "
        "monetary pillar derives most of its sub-weight from it, so a missing "
        "leg here costs a currency the heaviest pillar in the model."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "DGS2",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="Treasury constant maturity",
        ),
        "EUR": _ref(
            SOURCE_ECB,
            "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note=(
                "euro-area AAA government spot curve, 2-year point, Svensson "
                "fit. AAA issuers only, so this is close to the Bund and does "
                "not carry periphery spread."
            ),
        ),
        "GBP": _ref(
            SOURCE_BOE,
            "GLC_NOMINAL_SPOT_SHORT/2.0",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note=(
                "UK nominal spot curve, 2-year point, from the yield curve "
                "archive. Read from sheet '3. spot, short end', the column "
                "whose header maturity is 2.0 years. Needs a spreadsheet "
                "reader, unlike every other source here."
            ),
        ),
        "JPY": _ref(
            SOURCE_MOF_JP,
            "2Y",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note=(
                "JGB par yield, 2-year column of the Ministry of Finance CSV. "
                "The current-month file and the full history are separate "
                "downloads; both are needed."
            ),
        ),
        "CHF": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "the SNB publishes a Confederation spot curve and the endpoint is "
            "verified (cube 'rendoblid', dimension '2J'), but it stopped at "
            "2025-07-31 while the rest of the SNB portal stayed current. No "
            "free replacement found. Enter by hand or accept that the Swiss "
            "franc runs the monetary pillar without a front end.",
        ),
        "CAD": _ref(
            SOURCE_BOC,
            "BD.CDN.2YR.DQ.YLD",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="Government of Canada 2-year benchmark bond yield",
        ),
        "AUD": _ref(
            SOURCE_RBA,
            "FCMYGBAG2D",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 2),
            note=(
                "Australian Government 2-year bond, interpolated, from RBA "
                "statistical table F2"
            ),
        ),
        "NZD": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "the RBNZ publishes 2-year government bond yields in table B2, but "
            "rbnz.govt.nz, nzdmo.govt.nz and debtmanagement.treasury.govt.nz "
            "all refuse automated requests with HTTP 403. Not verified, not "
            "wired in. This may be an environment block rather than a policy "
            "one, so it is worth retrying from another network before "
            "accepting the manual route.",
        ),
    },
)


YIELD_10Y = IndicatorSpec(
    key="yield_10y",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.MONTHLY,
    max_staleness_days=75,
    description=(
        "Ten-year benchmark government bond yield. Slower than the 2y and less "
        "directly tied to policy, but it has clean, current coverage across "
        "all eight, so it carries the monetary pillar for the two currencies "
        "whose front end is missing."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "DGS10",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="Treasury constant maturity, daily",
        ),
        "EUR": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/DEU.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"10y Bund, the euro-area benchmark; {_OECD_FRESHER}",
        ),
        "GBP": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/GBR.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "JPY": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/JPN.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CHE.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CAN.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/AUS.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/NZL.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
    },
)


CPI_YOY = IndicatorSpec(
    key="cpi_yoy",
    pillar=PillarName.INFLATION,
    unit="percent",
    frequency=Frequency.MONTHLY,
    max_staleness_days=200,
    description=(
        "Headline consumer price inflation, year on year. The inflation pillar "
        "scores the gap to each central bank's target rather than the raw "
        "print, so `CurrencyMeta.inflation_target` is the other half of this "
        "input. Coverage here was two of eight until the OECD's own API "
        "replaced FRED's frozen mirror."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "CPIAUCSL",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="yoy",
            note="CPI-U all items, seasonally adjusted index, from the BLS",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "CP0000EZ19M086NEST",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="yoy",
            note="Eurostat HICP all items, euro area 19, a true bloc aggregate",
        ),
        "GBP": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/GBR.M.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="national CPI, growth over one year. FRED's mirror froze at 2025-03.",
        ),
        "JPY": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/JPN.M.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note=(
                "Japan sits in the COICOP 2018 dataflow, not the 1999 one. "
                "FRED's mirror froze at 2021-06, a five-year hole."
            ),
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/CHE.M.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note="COICOP 2018 dataflow. FRED's mirror froze at 2025-04.",
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/CAN.M.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="national CPI. FRED's mirror froze at 2025-03.",
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/AUS.Q.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="quarterly by publication, not by choice; 2026Q2",
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/NZL.Q.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="quarterly; 2026Q2. FRED's mirror froze at 2023Q3.",
        ),
    },
)


CORE_CPI_YOY = IndicatorSpec(
    key="core_cpi_yoy",
    pillar=PillarName.INFLATION,
    unit="percent",
    frequency=Frequency.MONTHLY,
    max_staleness_days=200,
    description=(
        "Consumer prices excluding food and energy, year on year. Central banks "
        "react to this more than to the headline, so it leads policy and "
        "therefore leads the currency."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "CPILFESL",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="yoy",
            note="CPI-U less food and energy, seasonally adjusted index",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "00XEFDEZ19M086NEST",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="yoy",
            note="Eurostat HICP excluding energy, food, alcohol and tobacco",
        ),
        "GBP": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/GBR.M.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="all items less food and energy",
        ),
        "JPY": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/JPN.M.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="COICOP 2018 dataflow",
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG"
            "/CHE.M.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=(
                "the only Swiss core series found anywhere free: it is absent "
                "from both general price dataflows and lives in the dedicated "
                "core flow"
            ),
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/CAN.M.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="COICOP 2018 dataflow; the 1999 flow has no Canadian core",
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/AUS.Q.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="2026Q2. Not the RBA's trimmed mean, which is its preferred cut.",
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/NZL.Q.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="2026Q2. Not the RBNZ sectoral factor model estimate.",
        ),
    },
)


GDP_YOY = IndicatorSpec(
    key="gdp_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.QUARTERLY,
    max_staleness_days=270,
    description=(
        "Real GDP growth, year on year. Slow and heavily revised, so it anchors "
        "the growth pillar rather than driving it. Full G10 coverage, which is "
        "rare enough in this registry to be worth stating."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "GDPC1",
            "billions_chained_usd",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="real GDP, SAAR chained 2017 dollars; 2026Q2",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "CLVMNACSCAB1GQEA19",
            "millions_chained_eur",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="Eurostat real GDP, euro area 19, a true bloc aggregate; 2026Q2",
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "NGDPRSAXDCGBQ",
            "millions_chained_gbp",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="2026Q2",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "JPNRGDPEXP",
            "billions_chained_jpy",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="real GDP by expenditure; 2026Q2",
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "CLVMNACSCAB1GQCH",
            "millions_chained_chf",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="2026Q2",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "NGDPRSAXDCCAQ",
            "millions_chained_cad",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="2026Q2",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "NGDPRSAXDCAUQ",
            "millions_chained_aud",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="2026Q2",
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "NZLGDPRQPSMEI",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            note=(
                "already published as a year-on-year growth rate, so no "
                "transform; 2026Q1"
            ),
        ),
    },
)


UNEMPLOYMENT_RATE = IndicatorSpec(
    key="unemployment_rate",
    pillar=PillarName.EMPLOYMENT,
    unit="percent",
    frequency=Frequency.MONTHLY,
    max_staleness_days=270,
    description=(
        "Harmonised unemployment rate. Compared cross-sectionally against the "
        "rest of the G10 and against its own recent trend, since the level that "
        "counts as full employment differs by country."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "UNRATE",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note="BLS headline U-3, roughly one month behind",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "LRHUTTTTDEM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(
                f"German harmonised rate. {_EA_AGGREGATE_DEAD}: "
                "LRHUTTTTEZM156S stopped at 2023-01."
            ),
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "LRHUTTTTGBM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 4, 1),
            note="",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "LRHUTTTTJPM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "LRUN64TTCHQ156S",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            note=(
                "quarterly ILO rate, aged 15-64; Switzerland publishes no "
                "monthly harmonised rate on FRED. 2026Q1."
            ),
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "LRHUTTTTCAM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "LRHUTTTTAUM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "LRHUTTTTNZQ156S",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="quarterly by publication; 2026Q2",
        ),
    },
)


EMPLOYMENT_CHANGE = IndicatorSpec(
    key="employment_change",
    pillar=PillarName.EMPLOYMENT,
    unit="persons",
    frequency=Frequency.MONTHLY,
    max_staleness_days=270,
    description=(
        "Change in the number of people employed. The flow, not the stock: a "
        "falling unemployment rate driven by people leaving the labour force is "
        "a different signal from one driven by hiring, and this indicator is "
        "what separates them."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "PAYEMS",
            "thousands_of_persons",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            transform="diff",
            note="total nonfarm payrolls; the differenced level is the NFP headline",
        ),
        "EUR": _manual(
            "employment_change",
            "persons",
            Frequency.QUARTERLY,
            "no live euro-area or German employment level on FRED "
            "(LFEMTTTTEZQ647S stopped at 2022-10); take the Eurostat quarterly "
            "employment release by hand",
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "LFEMTTTTGBQ647S",
            "persons",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            transform="diff",
            note="2026Q1",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "LFEMTTTTJPM647S",
            "persons",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            transform="diff",
            note="",
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "LFEMTTTTCHQ647S",
            "persons",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            transform="diff",
            note="2026Q1",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "LFEMTTTTCAM647S",
            "persons",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="diff",
            note="",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "LFEMTTTTAUM647S",
            "persons",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            transform="diff",
            note="",
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "LFEMTTTTNZQ647S",
            "persons",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="diff",
            note="2026Q2",
        ),
    },
)


RETAIL_SALES_YOY = IndicatorSpec(
    key="retail_sales_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.MONTHLY,
    max_staleness_days=270,
    description=(
        "Retail trade volume, year on year. The fastest read on household "
        "demand, and the growth pillar's main monthly input given that GDP "
        "arrives quarterly and late."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "USASLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(
                "OECD retail volume growth, chosen over the fresher US-only "
                "RSAFS so the eight legs are measured the same way"
            ),
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "DEUSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=f"{_EA_AGGREGATE_DEAD}: EA19SLRTTO01GYSAM stopped at 2023-10.",
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "GBRSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "JPNSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note="",
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "CHESLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note="",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "CANSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 4, 1),
            note="",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "SLRTTO01AUQ659S",
            "percent",
            Frequency.QUARTERLY,
            date(2025, 4, 1),
            note=(
                "DISCONTINUED at 2025Q2. Australia has no live retail series "
                "on FRED and none was found on the OECD API either."
            ),
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "SLRTTO01NZQ659S",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            note="2026Q1",
        ),
    },
)


INDUSTRIAL_PRODUCTION_YOY = IndicatorSpec(
    key="industrial_production_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.MONTHLY,
    max_staleness_days=180,
    description=(
        "Industrial production, year on year. Coverage here is the worst of the "
        "growth inputs: four of eight are live. Weight it accordingly, or the "
        "growth pillar ends up scoring the countries that happen to publish "
        "rather than the countries that happen to be growing."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "USAPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(
                "OECD basis for cross-country comparability; INDPRO is the "
                "fresher US-only alternative"
            ),
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "DEUPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2023, 12, 1),
            note=(
                f"DISCONTINUED at 2023-12. {_EA_AGGREGATE_DEAD}, and the "
                "German proxy has now stopped too."
            ),
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "GBRPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note="",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "JPNPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note="",
        ),
        "CHF": _manual(
            "industrial_production_yoy",
            "percent",
            Frequency.QUARTERLY,
            "no Swiss industrial production series on FRED in any live form",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "CANPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 4, 1),
            note="",
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
    max_staleness_days=45,
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
    max_staleness_days=150,
    description=(
        "Merchandise trade balance in US dollars, seasonally adjusted. Already "
        "currency-converted by the source, so the eight legs are directly "
        "comparable without an FX step. Full, current G10 coverage."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "XTNTVA01USM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(
                "OECD basis for comparability; BOPGSTB is the fresher US-only "
                "goods and services balance"
            ),
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "XTNTVA01DEM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(
                f"{_EA_AGGREGATE_DEAD}: XTNTVA01EZM667S stopped at 2022-12. "
                "Germany runs a structural surplus larger than the bloc's, so "
                "this proxy flatters the euro."
            ),
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "XTNTVA01GBM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "XTNTVA01JPM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "XTNTVA01CHM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "XTNTVA01CAM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "XTNTVA01AUM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "XTNTVA01NZM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note="",
        ),
    },
)


CURRENT_ACCOUNT = IndicatorSpec(
    key="current_account",
    pillar=PillarName.EXTERNAL,
    unit="percent_of_gdp",
    frequency=Frequency.QUARTERLY,
    max_staleness_days=210,
    description=(
        "Current account balance as a share of GDP. A structural measure of "
        "whether a currency is financed by the world or financing it."
    ),
    series={
        code: _ref(
            SOURCE_FRED,
            series_id,
            "percent_of_gdp",
            Frequency.QUARTERLY,
            date(2024, 10, 1),
            note=(
                "DISCONTINUED at 2024Q4, as is every leg of this family. No "
                "free replacement was found: the OECD API's balance of "
                "payments dataflows cover trade in services and merchandise, "
                "not the quarterly current account balance."
            ),
        )
        for code, series_id in (
            ("USD", "USAB6BLTT02STSAQ"),
            ("EUR", "DEUB6BLTT02STSAQ"),
            ("GBP", "GBRB6BLTT02STSAQ"),
            ("JPY", "JPNB6BLTT02STSAQ"),
            ("CHF", "CHEB6BLTT02STSAQ"),
            ("CAD", "CANB6BLTT02STSAQ"),
            ("AUD", "AUSB6BLTT02STSAQ"),
            ("NZD", "NZLB6BLTT02STSAQ"),
        )
    },
)
"""The 210-day allowance is what a quarterly balance-of-payments release
honestly justifies: the quarter has to end, the statistics office needs about
two months, and the next quarter is then already half over. It is deliberately
not set high enough to let the frozen 2024Q4 data through. Setting it to 700
would make the indicator report as covered while feeding the model numbers two
years old, which is the exact failure this registry exists to prevent. As it
stands the indicator correctly reports zero coverage until a live source is
found, and the external pillar leans on `trade_balance`, which is current for
all eight."""


COT_NET_POSITION = IndicatorSpec(
    key="cot_net_position",
    pillar=PillarName.POSITIONING,
    unit="contracts",
    frequency=Frequency.WEEKLY,
    max_staleness_days=21,
    description=(
        "Net speculative position in CME currency futures from the CFTC "
        "Commitments of Traders report. A crowded position is a reason to fade "
        "a fundamental view, not to add to it, so this pillar usually works "
        "against the others by design."
    ),
    series={
        "USD": _cftc(
            "098662",
            date(2026, 9, 1),
            "USD Index on ICE, in the Legacy report (6dca-aqww), not TFF. The "
            "primary dollar read is the sign-flipped complement of the other "
            "seven; this contract is a small, thinly held cross-check.",
        ),
        "EUR": _cftc("099741", date(2026, 9, 1), "EURO FX, CME, TFF gpe5-46if"),
        "GBP": _cftc("096742", date(2026, 9, 1), "BRITISH POUND, CME"),
        "JPY": _cftc("097741", date(2026, 9, 1), "JAPANESE YEN, CME"),
        "CHF": _cftc("092741", date(2026, 9, 1), "SWISS FRANC, CME"),
        "CAD": _cftc("090741", date(2026, 9, 1), "CANADIAN DOLLAR, CME"),
        "AUD": _cftc("232741", date(2026, 9, 1), "AUSTRALIAN DOLLAR, CME"),
        "NZD": _cftc("112741", date(2026, 9, 1), "NZ DOLLAR, CME"),
    },
)


EQUITY_INDEX = IndicatorSpec(
    key="equity_index",
    pillar=PillarName.RISK,
    unit="index",
    frequency=Frequency.DAILY,
    max_staleness_days=75,
    description=(
        "Benchmark equity index for each economy. Feeds the risk pillar in two "
        "ways: as a proxy for the local growth and earnings picture, and, in "
        "concert with `CurrencyMeta.risk_beta`, as a read on whether the market "
        "is in risk-on or risk-off."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "SP500",
            "index",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="daily close; FRED holds a rolling ten-year window only",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "NIKKEI225",
            "index",
            Frequency.DAILY,
            date(2026, 9, 9),
            note="daily close",
        ),
        "EUR": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/DEU.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=(
                f"OECD share price index, monthly average. {_OECD_FRESHER}. "
                "Monthly is slow for a risk pillar: prefer the Stooq daily "
                "feed where it can be made to work, and keep this as the "
                "dependable fallback."
            ),
        ),
        "GBP": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/GBR.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CHE.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CAN.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/AUS.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/NZL.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
    },
)


VIX = IndicatorSpec(
    key="vix",
    pillar=PillarName.RISK,
    unit="index",
    frequency=Frequency.DAILY,
    max_staleness_days=7,
    description=(
        "CBOE implied volatility on the S&P 500. A single global number, not a "
        "per-currency one: it sets the risk regime, and the currencies then "
        "sort themselves by `CurrencyMeta.risk_beta`."
    ),
    series={
        GLOBAL: _ref(
            SOURCE_FRED,
            "VIXCLS",
            "index",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="daily close",
        ),
    },
)


COMMODITY_INDEX = IndicatorSpec(
    key="commodity_index",
    pillar=PillarName.EXTERNAL,
    unit="index",
    frequency=Frequency.MONTHLY,
    max_staleness_days=90,
    description=(
        "Terms-of-trade proxy for the commodity currencies, plus a global "
        "benchmark. Only the three currencies with a `commodity_link` in "
        "`CurrencyMeta` carry a specific ref; the others take the global index "
        "or nothing."
    ),
    series={
        GLOBAL: _ref(
            SOURCE_FRED,
            "PALLFNFINDEXM",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="IMF all commodity price index, 2016=100",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "DCOILWTICO",
            "usd_per_barrel",
            Frequency.DAILY,
            date(2026, 9, 1),
            note="WTI spot, the standard Canadian dollar terms-of-trade proxy",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "PIORECRUSDM",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="IMF iron ore price index",
        ),
        "NZD": _manual(
            "commodity_index",
            "index",
            Frequency.IRREGULAR,
            "no dairy price index on FRED, verified by search. The "
            "GlobalDairyTrade auction index is the right series and is "
            "published fortnightly on globaldairytrade.info. PFOODINDEXM is a "
            "poor but free substitute.",
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
        indicator: Canonical indicator key, e.g. ``"yield_2y"``.
        currency: ISO 4217 code, or ``"GLOBAL"`` for cross-market series.

    Returns:
        The `SeriesRef`, or ``None`` when the registry has no source for this
        pair. ``None`` and a manual ref mean different things: the first is
        silence, the second is a known gap with a named fallback.

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


def coverage_report(asof: date | None = None) -> Mapping[str, float]:
    """Report the fraction of the G10 each indicator covers with usable data.

    A currency counts only when its ref is verified, is not manual, and its
    ``last_observed`` falls inside the indicator's ``max_staleness_days``. All
    three conditions matter, and the third is the one that was missing before:
    an identifier that resolves but stopped publishing in 2024 is not coverage,
    it is a number that will pass every type check and score a currency wrongly.

    An indicator holding a ``GLOBAL`` ref covers the whole universe by
    construction, since one VIX print serves all eight currencies.

    Args:
        asof: Date to age the registry against. Defaults to `VERIFIED_ON`,
            which is the honest default: the ``last_observed`` dates were
            recorded then, so ageing against a much later date measures how
            long since this file was checked as much as how stale the data is.
            Pass a real run date to see the position on that day, and re-verify
            the registry when the two drift far apart.

    Returns:
        Indicator key to fraction in ``0.0..1.0``.

    """
    when = asof or VERIFIED_ON
    report: dict[str, float] = {}
    for key, spec in INDICATORS.items():
        limit = spec.max_staleness_days
        global_ref = spec.series.get(GLOBAL)
        if global_ref is not None:
            usable = global_ref.fetchable and not global_ref.stale_on(when, limit)
            report[key] = 1.0 if usable else 0.0
            continue
        covered = sum(
            1
            for code in G10
            if (ref := spec.series.get(code)) is not None
            and ref.fetchable
            and not ref.stale_on(when, limit)
        )
        report[key] = covered / len(G10)
    return report


def identifier_coverage() -> Mapping[str, float]:
    """Report how many G10 legs have a fetchable identifier, ignoring freshness.

    Useful for one thing only: telling a wiring problem apart from a data
    problem. If `coverage_report` says 0.0 and this says 1.0, every identifier
    is right and the source has stopped publishing. If both say 0.0, the
    registry has no source at all.

    Do not use this to decide whether a pillar can score. That is
    `coverage_report`'s job, and the difference between the two functions is
    exactly the class of bug this registry is built to avoid.

    Returns:
        Indicator key to fraction in ``0.0..1.0``.

    """
    report: dict[str, float] = {}
    for key, spec in INDICATORS.items():
        global_ref = spec.series.get(GLOBAL)
        if global_ref is not None:
            report[key] = 1.0 if global_ref.fetchable else 0.0
            continue
        covered = sum(
            1
            for code in G10
            if (ref := spec.series.get(code)) is not None and ref.fetchable
        )
        report[key] = covered / len(G10)
    return report


def stale_refs(asof: date | None = None) -> Mapping[str, tuple[str, ...]]:
    """List the currencies whose ref for each indicator is unusable.

    The complement of `coverage_report`, and the more actionable of the two:
    this is the list an operator works through, and the list a report shows so
    a reader can see which legs of a score were extrapolated.

    Args:
        asof: Date to age against. Defaults to `VERIFIED_ON`.

    Returns:
        Indicator key to the currencies with no usable ref. Indicators with
        full coverage are omitted, so an empty mapping means the registry is
        entirely healthy.

    """
    when = asof or VERIFIED_ON
    out: dict[str, tuple[str, ...]] = {}
    for key, spec in INDICATORS.items():
        if GLOBAL in spec.series:
            continue
        limit = spec.max_staleness_days
        bad = tuple(
            code
            for code in G10
            if (ref := spec.series.get(code)) is None
            or not ref.fetchable
            or ref.stale_on(when, limit)
        )
        if bad:
            out[key] = bad
    return out
